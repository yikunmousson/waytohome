"""四条候选走廊的静态地图缩略图（高德静态图 API / v3/staticmap）。

为什么用「静态图」而不是在前端放地图 SDK：
    这一页是每天自己看的看板，不是导航产品。缩略图只需要一眼看清
    「四条线在哪儿分叉、各自绕去哪边」。静态 PNG 最省事，而且
    前端不放任何 Key —— Key 放在前端 JS 里会被抓走。

为什么四条线画在一张图上：
    高德静态图 paths 参数上限正好是 4 条折线，和我们候选走廊的数量一致。
    四条线共用一个 bbox、一个 zoom，分叉关系才看得出来；
    拆成四张图各自动缩放，反而看不出谁比谁更偏西。

几何从哪来：
    采集阶段只在推荐走廊上挂 polyline（否则 data.json 会膨胀到几 MB），
    所以这里按需抓一次四条走廊的几何，缓存到 data/geometry/。
    路不会动，所以缓存没有过期时间，只在 --refresh 时重抓。

合规：
    只用高德（白名单内）。Key 从环境变量 / .env 读，只出现在本脚本发出的
    服务端请求里，不写进 web/ 下的任何文件。

用法：
    python3 src/thumb_map.py                      # 两个方向都出图
    python3 src/thumb_map.py --direction outbound
    python3 src/thumb_map.py --refresh            # 忽略缓存，重抓几何
    python3 src/thumb_map.py --if-stale           # 几何没变且图已在就跳过（定时任务用）
    python3 src/thumb_map.py --url-only           # 只打印 URL，不下载（调试用）
"""

import argparse
import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from amap import AmapClient, AmapError                                  # noqa: E402
from collect import collect_strategy, iter_corridors, load_config, load_env_file  # noqa: E402
from segment import build_path_analysis                                 # noqa: E402
from geo import simplify                                                # noqa: E402

TZ = timezone(timedelta(hours=8))
GEOM_DIR = ROOT / "data" / "geometry"
STATICMAP = "https://restapi.amap.com/v3/staticmap"

# 与 web/index.html 的 --c0..--c3 保持一致：改一处必须改另一处，否则看板图例和地图对不上。
PALETTE = ["#1F5FA8", "#D9A036", "#D9604F", "#7A6FA8"]
MAX_PTS = 56          # 每条折线最多保留多少个点（URL 长度和辨识度的折中）
MAX_SIDE = 1024       # 静态图 size 参数上限

DIR_LABEL = {"outbound": "去程", "return": "返程"}


# ---------------- 几何抓取与缓存 ----------------

def fetch_geometry(client, cfg: dict, trip: dict, corridor: dict, strategy: int) -> dict:
    """抓一条走廊的完整几何。失败抛 AmapError。"""
    data = client.driving_v3(trip["origin"], trip["destination"],
                             strategy=strategy, waypoints=corridor["waypoints"] or None)
    paths = ((data.get("route") or {}).get("paths")) or []
    if not paths:
        raise AmapError("高德未返回路线")
    p = paths[0]
    analysis = build_path_analysis(p, cfg["analysis"]["segment_count"])
    if not analysis:
        raise AmapError("路径几何为空")
    raw_km = _num(p.get("distance")) / 1000.0
    return {
        "polyline": analysis["polyline"],          # 已抽稀到 ~700 点以内
        "total_km": analysis["total_km"],          # 我们自己量出来的里程
        "distance_km": round(raw_km, 1) or analysis["total_km"],   # 高德报的里程
        "roads": analysis["roads"],
        "n_points": len(analysis["polyline"]),
    }


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def ensure_geometry(client, cfg: dict, direction: str, trip: dict,
                    refresh: bool = False, verbose: bool = True) -> tuple:
    """保证该方向四条走廊的几何都在缓存里。

    返回 (几何字典, 本轮是否真的抓过网络请求)。
    第二个值给 --if-stale 用：几何没变说明路线没动，图也就不用重新下载，
    免得每 15 分钟的定时任务白耗一次静态图配额。
    """
    GEOM_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = GEOM_DIR / ("%s.json" % direction)
    cache = {}
    if cache_file.exists() and not refresh:
        try:
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
    out = dict(cache)
    strategy = collect_strategy(cfg)
    changed = False

    for c in iter_corridors(trip):
        cid = c["id"]
        hit = out.get(cid)
        cache_matches = (
            hit and hit.get("polyline")
            and int(hit.get("route_version", 1)) == int(c.get("route_version", 1))
            and (hit.get("waypoints") or []) == (c.get("waypoints") or [])
        )
        if cache_matches and not refresh:
            if verbose:
                print("   · %-12s 用缓存 %.1f km / %d 点"
                      % (c["short"], _num(hit.get("distance_km")), len(hit["polyline"])))
            continue
        try:
            g = fetch_geometry(client, cfg, trip, c, strategy)
        except AmapError as exc:
            print("   ✗ %-12s 抓几何失败：%s" % (c["short"], exc))
            continue
        g.update({
            "id": cid, "short": c["short"], "name": c["name"],
            "recommended": bool(c["recommended"]), "waypoints": c["waypoints"],
            "route_version": c["route_version"],
            "fetched_at": datetime.now(TZ).isoformat(timespec="seconds"),
        })
        out[cid] = g
        changed = True
        if verbose:
            print("   ✓ %-12s %.1f km / %d 点" % (c["short"], g["distance_km"], g["n_points"]))

    if changed:
        cache_file.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")),
                              encoding="utf-8")
        if verbose:
            print("   ↳ 几何已缓存到 %s" % cache_file.relative_to(ROOT))
    return out, changed


def ordered_geometry(geoms: dict, trip: dict) -> list:
    """按 config 里的走廊顺序排好，并补上配色索引（与看板 PALETTE 对齐）。"""
    rows = []
    for i, c in enumerate(iter_corridors(trip)):
        g = geoms.get(c["id"])
        if not g or not g.get("polyline"):
            continue
        rows.append({
            "id": c["id"], "short": c["short"], "recommended": bool(c["recommended"]),
            "idx": i, "polyline": g["polyline"], "distance_km": g.get("distance_km"),
        })
    return rows


def thumb_paths(out_dir: Path, direction: str, route_version: int) -> tuple:
    """缩略图文件名里带路线版本号。

    为什么要把版本号写进文件名（而不是只写进 json）：
        改了途经点之后，旧版本的图还躺在 web/ 里。如果文件名不变，前端拿到的是
        一张「旧路线」的图配「新路线」的数据 —— 看着对，其实是错的。
        （浏览器缓存也会让旧图更难清掉。）
        把版本编进文件名之后：文件名对不上就是 404 → 前端自然走到「还没生成」的提示，
        不会出现新旧混搭。这也顺便解决了缓存：换版本＝换文件名。
    """
    base = "corridors-thumb-%s-v%d" % (direction, route_version)
    return out_dir / (base + ".png"), out_dir / (base + ".json")


def prune_old_thumbs(out_dir: Path, direction: str, keep_version: int, verbose: bool = True) -> list:
    """删掉同方向其它版本的图（含早期不带版本号的命名）。"""
    removed = []
    for p in sorted(out_dir.glob("corridors-thumb-%s*.png" % direction)) + \
             sorted(out_dir.glob("corridors-thumb-%s*.json" % direction)):
        m = re.match(r"^corridors-thumb-%s(?:-v(\d+))?\.(png|json)$" % re.escape(direction), p.name)
        if not m:
            continue
        if m.group(1) is not None and int(m.group(1)) == keep_version:
            continue
        p.unlink()
        removed.append(p.name)
    if removed and verbose:
        print("   ⌫ 清掉旧版本图：%s" % "、".join(removed))
    return removed


# ---------------- 抽稀与 URL 拼接 ----------------

def decimate(points: list, max_pts: int = MAX_PTS) -> list:
    """Douglas-Peucker 自适应抽稀：容差不够就逐步放大，直到点数达标。

    静态图走的是 GET，整条 URL 全塞在 query 里，点太多会被服务端拒。
    60 个点画一条 600 km 的线，视觉上已经完全够用。
    """
    pts = [[float(p[0]), float(p[1])] for p in points]
    if len(pts) <= max_pts:
        return pts
    tol = 0.003
    for _ in range(14):
        s = simplify(pts, tol)
        if len(s) <= max_pts:
            return s
        tol *= 1.45
    stride = max(1, len(pts) // (max_pts - 1))
    s = pts[::stride]
    if s[-1] != pts[-1]:
        s.append(pts[-1])
    return s


def bbox_of(point_lists: list) -> tuple:
    xs = [p[0] for pl in point_lists for p in pl]
    ys = [p[1] for pl in point_lists for p in pl]
    return min(xs), min(ys), max(xs), max(ys)


def pick_size(bbox: tuple, max_side: int = MAX_SIDE, min_ar: float = 0.5) -> tuple:
    """按 bbox 的等距长宽比反推图片尺寸，尽量少留空白。

    经度要按 cos(纬度) 压缩，否则在中纬度会把东西方向算宽。

    min_ar（最小宽高比）是必须的：这四条走廊整体是南北向的细长条，
    纯按 bbox 比例出图会得到 407×1024 —— 横向只剩 32 px 余量，
    而起点深圳正好压在 bbox 的右下角，地名标签会被画布边缘裁掉。
    把画布撑到 1:2 之后，左右余量和上下拉平（各约 85 px），标签就完整了。
    （高德的 zoom 是整数，没法靠微调 bbox 来加内边距。）
    """
    lon0, lat0, lon1, lat1 = bbox
    latm = (lat0 + lat1) / 2.0
    w_km = max(1e-6, (lon1 - lon0) * 111.32 * math.cos(math.radians(latm)))
    h_km = max(1e-6, (lat1 - lat0) * 110.9)
    ar = max(w_km / h_km, min_ar)
    if ar >= 1.0:
        w, h = max_side, int(round(max_side / ar))
    else:
        h, w = max_side, int(round(max_side * ar))
    return max(120, min(max_side, w)), max(120, min(max_side, h))


def _amap_color(hex_color: str) -> str:
    return "0x" + hex_color.lstrip("#").upper()


def clean_label(s: str, max_len: int = 15) -> str:
    """清洗地名，让它能通过静态图的 labels 校验。

    踩过的坑：labels 的 content 里只要带空格（编码成 %20）就整条请求
    报 INVALID_PARAMS —— 实测「深圳·西丽地铁站」可以，「深圳 · 西丽地铁站」不行。
    所以这里把空白全部抹掉；顺便把长度收进 15 字上限（超了也会报错）。
    """
    t = "".join(ch for ch in (s or "") if not ch.isspace())
    return t[:max_len]


def build_url(key: str, rows: list, trip: dict, size: tuple,
              with_labels: bool = True, traffic: bool = False) -> tuple:
    """拼出静态图 URL。返回 (url, 尺寸, 各线保留点数)。"""
    # 静态图后画的压在上面：备选先画，推荐走廊留在最上层。
    draw = sorted(rows, key=lambda r: (r["recommended"], r["idx"]))

    segs, counts = [], {}
    for r in draw:
        pts = decimate(r["polyline"])
        counts[r["id"]] = len(pts)
        coords = ";".join("%.5f,%.5f" % (p[0], p[1]) for p in pts)
        weight = 6 if r["recommended"] else 4
        # fillcolor / fillTransparency 必须留空 —— 一旦给了填充色，折线会闭合成多边形。
        style = "%d,%s,%s,, " % (weight, _amap_color(PALETTE[r["idx"] % len(PALETTE)]),
                                 "0.95" if r["recommended"] else "0.85")
        segs.append(style.strip() + ":" + coords)
    paths = "|".join(segs)

    # 起点绿、终点黑；label 只接受单个中文字
    markers = "mid,0x1D9E75,起:%s|mid,0x1D1D1F,终:%s" % (trip["origin"], trip["destination"])

    params = [("size", "%d*%d" % size), ("paths", paths), ("markers", markers)]
    if with_labels:
        labels = "|".join(
            "%s,0,1,13,0xFFFFFF,0x1D1D1F:%s" % (clean_label(trip[k + "_name"]), trip[k])
            for k in ("origin", "destination")
        )
        params.append(("labels", labels))
    if traffic:
        params.append(("traffic", "1"))
    params.append(("key", key))

    url = STATICMAP + "?" + "&".join(
        "%s=%s" % (k, quote(v, safe=",:;|*")) for k, v in params)
    return url, counts


# ---------------- 出图 ----------------

def render_direction(client, cfg: dict, key: str, direction: str, trip: dict,
                     out_dir: Path, refresh: bool = False, url_only: bool = False,
                     with_labels: bool = True, traffic: bool = False,
                     if_stale: bool = False) -> Path:
    print("[%s] %s → %s"
          % (DIR_LABEL.get(direction, direction), trip["origin_name"], trip["destination_name"]))
    out_dir.mkdir(parents=True, exist_ok=True)
    version = int(trip.get("route_version", 1))
    out, meta_path = thumb_paths(out_dir, direction, version)

    geoms, fetched = ensure_geometry(client, cfg, direction, trip, refresh=refresh)

    # 定时任务用 --if-stale：路线没动过、图也已经在，就不用再问高德要一次同样的图。
    if if_stale and not fetched and not refresh and out.exists():
        print("   几何无变化且图已存在，跳过下载（--if-stale）")
        prune_old_thumbs(out_dir, direction, version)
        return out

    rows = ordered_geometry(geoms, trip)
    if not rows:
        raise AmapError("没有任何一条走廊有几何，先检查 Key / 途经点")

    pts_all = [r["polyline"] for r in rows]
    origin = [float(x) for x in trip["origin"].split(",")]
    dest = [float(x) for x in trip["destination"].split(",")]
    bbox = bbox_of(pts_all + [[origin], [dest]])
    size = pick_size(bbox)
    url, counts = build_url(key, rows, trip, size, with_labels=with_labels, traffic=traffic)

    print("   走廊 %d 条，抽稀后点数：%s"
          % (len(rows), "、".join("%s %d" % (r["short"], counts[r["id"]]) for r in rows)))
    print("   底图尺寸 %d×%d，bbox %.3f,%.3f – %.3f,%.3f"
          % (size[0], size[1], bbox[0], bbox[1], bbox[2], bbox[3]))
    print("   URL 长度 %d 字符" % len(url))

    if url_only:
        print("   " + url)
        return out

    import requests
    resp = requests.get(url, timeout=30)
    if resp.status_code != 200:
        raise AmapError("静态图下载失败 HTTP %d" % resp.status_code)
    ctype = resp.headers.get("Content-Type", "")
    if "image" not in ctype:
        # 高德出错时返回的是 JSON/文本，直接把它打出来，比"存了个坏图"好排查
        raise AmapError("静态图返回的不是图片（%s）：%s" % (ctype, resp.text[:200]))
    out.write_bytes(resp.content)
    print("   ✓ 已保存 %s（%.0f KB）" % (out.relative_to(ROOT), len(resp.content) / 1024))

    # 顺手把走廊元信息落一份，方便以后接交互式地图时不用再解析 config
    meta_path.write_text(json.dumps({
        "direction": direction, "updated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "route_version": version,
        "size": list(size), "bbox": [round(v, 5) for v in bbox],
        "corridors": [{"id": r["id"], "short": r["short"], "recommended": r["recommended"],
                       "color": PALETTE[r["idx"] % len(PALETTE)],
                       "distance_km": r["distance_km"], "points": counts[r["id"]]}
                      for r in rows],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    prune_old_thumbs(out_dir, direction, version)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="四条候选走廊的静态地图缩略图")
    ap.add_argument("--direction", choices=["outbound", "return", "both"], default="both")
    ap.add_argument("--refresh", action="store_true", help="忽略几何缓存，重新抓")
    ap.add_argument("--url-only", action="store_true", help="只打印 URL，不下载（调试用）")
    ap.add_argument("--no-labels", action="store_true", help="不加地名标签，只留起终标记")
    ap.add_argument("--traffic", action="store_true", help="底图叠加实时路况（会盖住折线，慎用）")
    ap.add_argument("--if-stale", action="store_true",
                    help="几何没变化且图已存在时直接跳过（定时任务用，省静态图配额）")
    ap.add_argument("--out-dir", default=str(ROOT / "web"))
    args = ap.parse_args()

    load_env_file()
    cfg = load_config()
    import os
    key = os.environ.get("AMAP_KEY", "").strip()
    try:
        client = AmapClient(key, cfg["collect"]["timeout_seconds"], cfg["collect"]["retries"])
    except AmapError as exc:
        print("✗ %s" % exc)
        sys.exit(2)

    dirs = ["outbound", "return"] if args.direction == "both" else [args.direction]
    trips = {"outbound": cfg["trip"], "return": cfg.get("return_trip")}
    out_dir = Path(args.out_dir)

    made = 0
    for dr in dirs:
        trip = trips.get(dr)
        if not trip:
            print("[%s] 配置里没有这个方向，跳过" % dr)
            continue
        try:
            render_direction(client, cfg, key, dr, trip, out_dir,
                             refresh=args.refresh, url_only=args.url_only,
                             with_labels=not args.no_labels, traffic=args.traffic,
                             if_stale=args.if_stale)
        except Exception as exc:
            print("   ✗ %s 出图失败：%s" % (dr, exc))
            continue
        made += 1
        print("")
    if not made:
        sys.exit(1)


if __name__ == "__main__":
    main()
