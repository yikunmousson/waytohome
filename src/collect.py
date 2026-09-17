"""单次采集：调高德，把「去程 + 返程」每一条候选走廊此刻的路况存下来。

一次运行 = 每个方向一条 JSONL 记录，追加到 data/raw/YYYY-MM-DD.jsonl。
每条记录里装着该方向全部候选走廊（1 推荐 + 3 备选）各自的快照。

    一轮调用数 = 走廊数 × 方向数（默认 4 × 2 = 8 次）
    15 分钟一档 → 768 次/天，免费额度 5000 次/天，余量充足。

长时间跑下去，这些记录就是「历史基线」—— 高德不给历史，只能自己攒。

用法：
    AMAP_KEY=xxx python3 src/collect.py                    # 跑一次（去程+返程）
    AMAP_KEY=xxx python3 src/collect.py --direction outbound
    AMAP_KEY=xxx python3 src/collect.py --loop             # 按配置频率常驻（本机用）
    python3 src/collect.py --fixture tests/fixture_amap.json --dry-run   # 离线验证
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from amap import AmapClient, AmapError                     # noqa: E402
from segment import build_path_analysis, path_summary      # noqa: E402

TZ = timezone(timedelta(hours=8))
WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
DIR_LABEL = {"outbound": "去程", "return": "返程"}


# ---------------- 客户端 ----------------

class FixtureClient:
    """离线夹具客户端：不发网络请求，直接回放本地保存的高德响应。

    用于在真实 Key 到位前验证整条流水线，也方便复现某一次异常数据。
    每条走廊会拿到同一个响应，所以离线跑出来的走廊耗时是一样的 —— 这是预期的。
    """

    source = "fixture"

    def __init__(self, path: str):
        self.payload = json.loads(Path(path).read_text(encoding="utf-8"))
        self.call_count = 0

    def driving_v3(self, origin, destination, strategy=None, waypoints=None, extensions="all",
                   cartype=None, province=None, number=None):
        self.call_count += 1
        return self.payload


# ---------------- 配置与环境 ----------------

def load_env_file() -> None:
    """读 .env（如果存在），不覆盖已有的环境变量。"""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config() -> dict:
    import yaml
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def iter_trips(cfg: dict):
    """产出 (direction, trip_cfg)。返程没配就只跑去程。"""
    yield "outbound", cfg["trip"]
    if cfg.get("return_trip"):
        yield "return", cfg["return_trip"]


def iter_corridors(trip: dict) -> list:
    """取该方向的走廊清单。

    兼容两种写法：
      - 新写法：trip.corridors 是列表，每条含 id/name/waypoints
      - 老写法：trip.waypoints 单条路线 —— 自动包装成一条名为「默认」的走廊
    """
    cors = trip.get("corridors")
    if cors:
        out = []
        for i, c in enumerate(cors):
            out.append({
                "id": c.get("id") or ("corridor-%d" % (i + 1)),
                "name": c.get("name") or c.get("short") or ("走廊 %d" % (i + 1)),
                "short": c.get("short") or c.get("name") or ("走廊 %d" % (i + 1)),
                "recommended": bool(c.get("recommended")),
                "note": c.get("note") or "",
                "waypoints": c.get("waypoints") or [],
                "route_version": int(trip.get("route_version", 1)),
            })
        return out
    return [{
        "id": "default", "name": "默认路线", "short": "默认",
        "recommended": True, "note": "",
        "waypoints": trip.get("waypoints") or [],
        "route_version": int(trip.get("route_version", 1)),
    }]


def collect_strategy(cfg: dict) -> int:
    """统一策略。老配置写过 strategies: [...] 列表，取第一个即可。"""
    c = cfg["collect"]
    if "strategy" in c:
        return int(c["strategy"])
    st = c.get("strategies") or [0]
    return int(st[0])


def day_type_of(d: datetime, cfg: dict) -> str:
    hol = cfg["holiday"]
    ds = d.strftime("%Y-%m-%d")
    if ds in (hol.get("makeup_workdays") or []):
        return "makeup"          # 补班日，按工作日看
    if ds in (hol.get("holiday_days") or []):
        return "holiday"
    if d.weekday() >= 5:
        return "weekend"
    return "workday"


def bucket_of(d: datetime, minutes: int) -> str:
    m = (d.minute // minutes) * minutes
    return "%02d:%02d" % (d.hour, m)


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


# ---------------- 采集 ----------------

def collect_corridor(client, cfg: dict, trip: dict, corridor: dict, now: datetime,
                     seg_n: int, strategy: int, keep_geometry: bool) -> dict:
    """采一条走廊。任何失败都不抛出，而是记成 ok=False，不拖垮整轮。"""
    origin, destination = trip["origin"], trip["destination"]
    item = {
        "id": corridor["id"],
        "name": corridor["name"],
        "short": corridor["short"],
        "recommended": corridor["recommended"],
        "waypoints": corridor["waypoints"],
        "route_version": corridor["route_version"],
        "ok": False,
    }
    try:
        data = client.driving_v3(origin, destination, strategy=strategy,
                                 waypoints=corridor["waypoints"] or None,
                                 cartype=trip.get("cartype"),
                                 province=trip.get("plate_province"),
                                 number=trip.get("plate_number"))
    except AmapError as exc:
        item["error"] = str(exc)
        return item
    except Exception as exc:                                  # 网络/解析异常也不致命
        item["error"] = "%s: %s" % (type(exc).__name__, exc)
        return item

    paths = ((data.get("route") or {}).get("paths")) or []
    if not paths:
        item["error"] = "高德未返回路线"
        return item

    # 每条走廊取高德给出的第一条（= 该走廊在速度优先下的最优解）
    analysis = build_path_analysis(paths[0], seg_n)
    if not analysis:
        item["error"] = "路径几何为空，无法分段"
        return item

    summary = path_summary(paths[0])
    item.update({
        "ok": True,
        "duration_min": summary["duration_min"],
        "distance_km": summary["distance_km"],
        "tolls": summary["tolls"],
        "toll_distance_km": summary["toll_distance_km"],
        "traffic_lights": summary["traffic_lights"],
        "arrive_at": (now + timedelta(minutes=summary["duration_min"])).isoformat(timespec="minutes"),
        "total_km": analysis["total_km"],
        "roads": analysis["roads"],
        "bins": analysis["bins"],
    })
    if summary["duration_min"] > 0:
        item["speed_kmh"] = round(summary["distance_km"] / (summary["duration_min"] / 60), 1)
    # 几何只挂推荐走廊，避免 4 条 × 700 个点的体积失控
    if keep_geometry:
        item["polyline"] = analysis["polyline"]
        item["cum_km"] = analysis["cum_km"]
    return item


def collect_direction(client, cfg: dict, direction: str, trip: dict) -> dict:
    now = datetime.now(TZ)
    seg_n = cfg["analysis"]["segment_count"]
    strategy = collect_strategy(cfg)
    corridors = iter_corridors(trip)
    rec_geom = next((c["id"] for c in corridors if c["recommended"]), corridors[0]["id"])
    t0 = time.time()

    print("  【%s】%s → %s（%d 条走廊）"
          % (DIR_LABEL[direction], trip["origin_name"], trip["destination_name"], len(corridors)))

    items = []
    for c in corridors:
        it = collect_corridor(client, cfg, trip, c, now, seg_n, strategy,
                              keep_geometry=(c["id"] == rec_geom))
        items.append(it)
        if it["ok"]:
            print("    %s %-12s %6.1f km  %5.2f h  ¥%-4.0f  到达 %s"
                  % ("★" if c["recommended"] else " ", it["short"], it["distance_km"],
                     it["duration_min"] / 60, it["tolls"], it["arrive_at"][11:16]))
        else:
            print("    ✗ %-12s 采集失败：%s" % (it["short"], it.get("error", "")))

    ok_items = [i for i in items if i["ok"]]
    fastest = min(ok_items, key=lambda r: r["duration_min"])["id"] if ok_items else None
    if fastest:
        f = next(i for i in ok_items if i["id"] == fastest)
        print("    → 本轮最快：%s（%.2f h）" % (f["short"], f["duration_min"] / 60))

    return {
        "ts": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "weekday": now.weekday(),
        "weekday_cn": WEEKDAY_CN[now.weekday()],
        "day_type": day_type_of(now, cfg),
        "bucket": bucket_of(now, cfg["analysis"]["baseline_bucket_minutes"]),
        "direction": direction,
        "route_version": int(trip.get("route_version", 1)),
        "source": getattr(client, "source", "amap"),   # amap / fixture —— 基线只认 amap
        "strategy": strategy,
        "corridors": items,
        "fastest": fastest,
        "api_calls": client.call_count,
        "elapsed_s": round(time.time() - t0, 2),
    }


def corridor_signature(rec: dict, cid: str = None) -> list:
    """取某条走廊的道路序列。默认取推荐走廊 —— 它是我们最关心的那条。"""
    cors = rec.get("corridors") or []
    if not cors:                                   # 老格式兜底
        cors = rec.get("routes") or []
    if not cors:
        return []
    if cid:
        for c in cors:
            if c.get("id") == cid:
                return c.get("roads") or []
    for c in cors:
        if c.get("recommended") or c.get("is_primary"):
            return c.get("roads") or []
    return cors[0].get("roads") or []


def signature_similarity(a: list, b: list) -> float:
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb)


def previous_signature(raw_dir: Path, today: str, direction: str,
                       route_version: int = 1) -> list:
    """取最近一条同方向历史的路线签名，用来检测高德是否换了路线。"""
    for f in reversed(sorted(raw_dir.glob("*.jsonl"))):
        if f.stem > today:
            continue
        lines = [ln for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
        for ln in reversed(lines):
            try:
                rec = json.loads(ln)
            except Exception:
                continue
            if rec.get("direction") != direction:
                continue
            if int(rec.get("route_version", 1)) != int(route_version):
                continue
            sig = corridor_signature(rec)
            if sig:
                return sig
    return []


def append_record(record: dict, raw_dir: Path) -> Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    out = raw_dir / ("%s.jsonl" % record["date"])
    with out.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    return out


# ---------------- 单轮执行 ----------------

def run_once(client, cfg: dict, raw_dir: Path, only: str = "", dry_run: bool = False) -> dict:
    print("[%s] 采集 %s" % (datetime.now(TZ).strftime("%H:%M:%S"),
                            cfg["trip"].get("name", "")))
    records = {}
    for direction, trip in iter_trips(cfg):
        if only and direction != only:
            continue
        rec = collect_direction(client, cfg, direction, trip)
        if not any(c["ok"] for c in rec["corridors"]):
            print("    ✗ 所有走廊都失败，仍记录本轮失败，避免旧数据冒充实时")

        prev_sig = previous_signature(
            raw_dir, rec["date"], direction, rec.get("route_version", 1))
        sim = signature_similarity(prev_sig, corridor_signature(rec))
        rec["route_signature_similarity"] = round(sim, 3)
        rec["route_signature_checked"] = bool(prev_sig)
        if prev_sig and sim < 0.6:
            print("    ⚠ 推荐走廊与上次差异较大（相似度 %.2f），可能被绕行了，建议检查途经点" % sim)
        records[direction] = rec

    if dry_run:
        print("  (dry-run，未落盘)")
        return records

    for rec in records.values():
        p = append_record(rec, raw_dir)
    if records:
        print("  ✓ 追加写入 %s" % (ROOT / "data" / "raw" / ("%s.jsonl" % datetime.now(TZ).strftime("%Y-%m-%d"))).relative_to(ROOT))
        print("  （看板数据由 analyze.py 生成，采集阶段只负责攒原始记录）")
    return records


def latest_sample_time(raw_dir: Path) -> datetime:
    """返回最近一次落盘时间；没有记录时返回 None。"""
    for f in reversed(sorted(raw_dir.glob("*.jsonl"))):
        lines = [ln for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
        for line in reversed(lines):
            try:
                return datetime.fromisoformat(json.loads(line)["ts"])
            except Exception:
                continue
    return None


def collection_due(cfg: dict, raw_dir: Path, now: datetime = None) -> tuple:
    """GitHub 每 15 分钟唤醒一次，但是否调用高德由配置中的两档间隔决定。"""
    now = now or datetime.now(TZ)
    dense = (parse_iso(cfg["collect"]["dense_start"]) <= now
             < parse_iso(cfg["collect"]["dense_end"]))
    interval = int(cfg["collect"]["interval_dense_minutes"] if dense
                   else cfg["collect"]["interval_normal_minutes"])
    latest = latest_sample_time(raw_dir)
    if latest is None:
        return True, interval
    return (now - latest).total_seconds() >= max(1, interval) * 60 - 30, interval


def main() -> None:
    ap = argparse.ArgumentParser(description="国庆路线路况采集（多走廊）")
    ap.add_argument("--loop", action="store_true", help="按配置的频率持续采集")
    ap.add_argument("--dry-run", action="store_true", help="只打印不落盘")
    ap.add_argument("--direction", choices=["outbound", "return"], default="", help="只采某个方向")
    ap.add_argument("--fixture", help="用本地夹具响应代替真实请求（离线验证用）")
    ap.add_argument("--if-due", action="store_true",
                    help="仅在配置的采集间隔已到时执行（供 GitHub Actions 使用）")
    args = ap.parse_args()

    load_env_file()
    cfg = load_config()

    if args.fixture:
        client = FixtureClient(args.fixture)
    else:
        key = os.environ.get("AMAP_KEY", "").strip()
        try:
            client = AmapClient(key, cfg["collect"]["timeout_seconds"], cfg["collect"]["retries"])
        except AmapError as exc:
            print("✗ %s" % exc)
            sys.exit(2)

    raw_dir = ROOT / "data" / "raw"

    if args.if_due and not args.fixture:
        due, interval = collection_due(cfg, raw_dir)
        if not due:
            print("距离上次采样未满 %d 分钟，本轮不调用高德。" % interval)
            return

    if not args.loop:
        recs = run_once(client, cfg, raw_dir, args.direction, args.dry_run)
        sys.exit(0 if recs else 1)

    print("持续采集中，Ctrl-C 停止。")
    while True:
        try:
            run_once(client, cfg, raw_dir, args.direction, args.dry_run)
        except Exception as exc:
            print("  ✗ 本轮出错：%s" % exc)
        now = datetime.now(TZ)
        dense = (parse_iso(cfg["collect"]["dense_start"]) <= now
                 < parse_iso(cfg["collect"]["dense_end"]))
        iv = (cfg["collect"]["interval_dense_minutes"] if dense
              else cfg["collect"]["interval_normal_minutes"])
        print("  下一轮 %d 分钟后（%s）\n" % (iv, "密集档" if dense else "平常档"))
        time.sleep(iv * 60)


if __name__ == "__main__":
    main()
