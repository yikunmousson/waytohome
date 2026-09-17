"""造一段仿真历史，用于在真实数据攒够之前预览「看板有历史的样子」。

几何是真实的（data/route-geometry.json），只有「耗时 / 路况」是按经验曲线模拟的。
每条记录都带 source="simulated"，analyze.py 默认会把它排除在基线之外 ——
想看带历史的演示效果，要在 config.yaml 里显式打开：

    analysis:
      include_simulated: true

用法：
    python3 tests/backfill_demo.py                 # 补最近 14 天（4 条走廊）
    python3 tests/backfill_demo.py --days 21
    python3 tests/backfill_demo.py --clean         # 清掉所有仿真/夹具数据
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import make_fixture as fx                                  # noqa: E402
from collect import WEEKDAY_CN, bucket_of, day_type_of, iter_corridors   # noqa: E402
from segment import build_path_analysis, path_summary      # noqa: E402

TZ = timezone(timedelta(hours=8))

# 兜底：假如 raw 里一条真实记录都没有，就用实测的比例常数
FALLBACK_SCALE = {"leguang": 1.000, "xuguang": 1.023, "jinggangao": 1.142, "erguang": 1.191}


def line_is_real(ln: str) -> bool:
    """只有 source == "amap" 的记录才是真实采集，其余（仿真/夹具）都不能留。"""
    ln = ln.strip()
    if not ln:
        return False
    try:
        rec = json.loads(ln)
    except Exception:
        return False
    return (rec.get("source") or "") == "amap"


def corridor_scales(raw_dir: Path, direction: str, corridors: list) -> dict:
    """从最近一条真实采样里读出各走廊相对推荐走廊的耗时比例。

    这样仿真出来的「走廊差距」跟现实一致，而不是拍脑袋的常数。
    """
    latest = None
    for f in sorted(raw_dir.glob("*.jsonl"), reverse=True):
        for ln in reversed(f.read_text(encoding="utf-8").splitlines()):
            if not ln.strip():
                continue
            try:
                rec = json.loads(ln)
            except Exception:
                continue
            if rec.get("direction") != direction or (rec.get("source") or "") != "amap":
                continue
            if rec.get("corridors"):
                latest = rec
                break
        if latest:
            break

    scales = {}
    if latest:
        by_id = {c["id"]: c for c in latest["corridors"] if c.get("ok") and c.get("duration_min")}
        rec_id = next((c["id"] for c in corridors if c["recommended"]), None)
        base = by_id.get(rec_id, {}).get("duration_min")
        if base:
            for c in corridors:
                d = by_id.get(c["id"], {}).get("duration_min")
                scales[c["id"]] = round(d / base, 4) if d else FALLBACK_SCALE.get(c["id"], 1.0)
    for c in corridors:
        scales.setdefault(c["id"], FALLBACK_SCALE.get(c["id"], 1.0))
    return scales


def peakness(hour: int) -> float:
    """0 = 最空的凌晨，1 = 最堵的早高峰。"""
    lo, hi = 0.98, 1.45
    return max(0.0, min(1.0, (fx.HOUR_FACTOR[hour] - lo) / (hi - lo)))


def main() -> None:
    import yaml
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--step", type=int, default=2, help="每几小时一个采样点")
    ap.add_argument("--clean", action="store_true", help="删除所有仿真/夹具数据后退出")
    args = ap.parse_args()

    raw_dir = ROOT / "data" / "raw"
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))

    if args.clean:
        n = 0
        for f in raw_dir.glob("*.jsonl"):
            kept = []
            for ln in f.read_text(encoding="utf-8").splitlines():
                if not line_is_real(ln):
                    n += 1
                    continue
                kept.append(ln)
            if kept:
                f.write_text("\n".join(kept) + "\n", encoding="utf-8")
            else:
                f.unlink()
        print("✓ 清除 %d 条非真实记录（仿真 + 夹具）" % n)
        return

    geom = json.loads((ROOT / "data" / "route-geometry.json").read_text(encoding="utf-8"))
    now = datetime.now(TZ)
    start = now.date() - timedelta(days=args.days - 1)
    seg_n = cfg["analysis"]["segment_count"]

    trips = {"outbound": cfg["trip"], "return": cfg.get("return_trip")}
    scales = {d: corridor_scales(raw_dir, d, iter_corridors(trips[d])) for d in trips}

    by_date = {}
    made = 0
    for k in range(args.days):
        day = start + timedelta(days=k)
        for hour in range(0, 24, args.step):
            dt = datetime(day.year, day.month, day.day, hour, 0, tzinfo=TZ)
            if dt > now:
                continue
            for direction, trip in trips.items():
                corridors = iter_corridors(trip)
                rec_id = next((c["id"] for c in corridors if c["recommended"]), corridors[0]["id"])
                items = []
                # 返程整体略慢（回程潮汐）
                dir_scale = 1.0 if direction == "outbound" else 1.04
                for c in corridors:
                    # 拥堵时段的走廊差距被放大：凌晨几条路差不多，早高峰备选明显更慢
                    gap = scales[direction][c["id"]] - 1.0
                    eff = 1.0 + gap * (0.6 + 0.8 * peakness(hour))
                    payload = fx.build_path(geom, hour, time_scale=eff * dir_scale)
                    summary = path_summary(payload)
                    analysis = build_path_analysis(payload, seg_n)
                    if not analysis:
                        continue
                    item = {
                        "id": c["id"], "name": c["name"], "short": c["short"],
                        "recommended": c["recommended"], "waypoints": c["waypoints"], "ok": True,
                        "arrive_at": (dt + timedelta(minutes=summary["duration_min"])).isoformat(timespec="minutes"),
                        **summary,
                        "total_km": analysis["total_km"],
                        "roads": analysis["roads"],
                        "bins": analysis["bins"],
                    }
                    if summary["duration_min"] > 0:
                        item["speed_kmh"] = round(
                            summary["distance_km"] / (summary["duration_min"] / 60), 1)
                    if c["id"] == rec_id:
                        item["polyline"] = analysis["polyline"]
                        item["cum_km"] = analysis["cum_km"]
                    items.append(item)

                ok = [i for i in items if i["ok"]]
                rec = {
                    "ts": dt.isoformat(timespec="seconds"),
                    "date": dt.strftime("%Y-%m-%d"),
                    "time": dt.strftime("%H:%M"),
                    "weekday": dt.weekday(),
                    "weekday_cn": WEEKDAY_CN[dt.weekday()],
                    "day_type": day_type_of(dt, cfg),
                    "bucket": bucket_of(dt, cfg["analysis"]["baseline_bucket_minutes"]),
                    "direction": direction,
                    "strategy": 0,
                    "corridors": items,
                    "fastest": (min(ok, key=lambda r: r["duration_min"])["id"] if ok else None),
                    "source": "simulated",
                    "api_calls": 0,
                    "elapsed_s": 0,
                }
                by_date.setdefault(rec["date"], {"outbound": [], "return": []})
                by_date[rec["date"]][direction].append(rec)
                made += 1

    raw_dir.mkdir(parents=True, exist_ok=True)
    for ds, dirs in by_date.items():
        path = raw_dir / ("%s.jsonl" % ds)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        lines = []
        for direction in ("outbound", "return"):
            lines.extend(json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in dirs[direction])
        path.write_text(existing + "\n".join(lines) + "\n", encoding="utf-8")

    print("✓ 生成 %d 条仿真记录（每条含 4 条走廊），覆盖 %d 天（%s → %s）"
          % (made, len(by_date), start, now.date()))
    for d in trips:
        print("  %-9s 走廊耗时比例：%s" % (d, scales[d]))
    print("  仿真数据默认不进基线。要看带历史的看板效果，在 config.yaml 里设：")
    print("      analysis:\n        include_simulated: true")
    print("  清除方式：python3 tests/backfill_demo.py --clean")


if __name__ == "__main__":
    main()
