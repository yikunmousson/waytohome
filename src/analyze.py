"""把攒下来的采样，变成「走哪条、什么时候出发」的答案。

两条主线：

1) 每条走廊各自建一套「常态基线」
   高德不给历史路况，基线只能自己攒。刚开始只有几条采样，纯靠实测会得到
   一条毫无意义的曲线，所以用「先验 + 实测」的收缩估计（shrinkage）：

       因子 = (n × 实测因子 + K × 先验因子) / (n + K)

     n = 该小时已有的采样数，K = 先验的等效样本量（PRIOR_K）
     第一天：几乎全是先验，曲线依然有形状，能用来做决策
     第两周：n 远大于 K，曲线基本由你自己的数据说话
   每条结论都会标注依据：实测为主 / 实测+经验 / 经验推算。

2) 走廊之间横向比
   同一时刻，4 条走廊各自要多久？谁最快？差多少？这是「走哪条」的直接答案。
   出发窗口也一样：不是比一个时刻，而是逐段推进推算出全程耗时再比。

用法：
    python3 src/analyze.py
"""

import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from collect import iter_corridors, iter_trips           # noqa: E402

TZ = timezone(timedelta(hours=8))
DIRS = ["outbound", "return"]
WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# ---------------------------------------------------------------------------
# 经验先验：珠三角长途出行的「拥堵日形状」（各小时相对最畅通时段的倍数）
# 形态来自普遍规律：凌晨 3–5 点最空，早高峰 8–10 点、晚高峰 17–19 点各一个峰。
# 这不是拍脑袋的常数，而是把它当成「等效 K 条样本」，实测一多就会被压过去。
# ---------------------------------------------------------------------------
PRIOR_K = 6.0

PRIOR_FACTORS = {
    "workday": [1.38, 1.25, 1.07, 1.00, 1.00, 1.02, 1.08, 1.25, 1.43, 1.48, 1.43, 1.31,
                1.22, 1.18, 1.18, 1.20, 1.27, 1.39, 1.45, 1.41, 1.31, 1.22, 1.18, 1.27],
    "weekend": [1.35, 1.22, 1.05, 1.00, 1.00, 1.00, 1.04, 1.10, 1.22, 1.32, 1.34, 1.28,
                1.22, 1.18, 1.18, 1.22, 1.28, 1.34, 1.38, 1.32, 1.24, 1.18, 1.14, 1.20],
}
# 假期相对周末的额外放大（往年国庆形态）
HOLIDAY_PRIOR = 1.14

# ---------------------------------------------------------------------------
# 先验振幅的「沿线衰减」—— 这是本模型最关键的一处修正。
#
# 上面那条曲线描述的是「一段城市道路」的日形状：晚高峰能比凌晨慢 39%。
# 但深圳→衡阳是 603 km 的长途，其中约 80% 是粤北山区高速，
# 时段对它的影响远没有那么剧烈 —— 山里的路不会因为到了 18 点就慢 39%。
#
# 如果照搬城市振幅，会推出「凌晨 2 点只要 5.1 小时」这种结论
# （603 km / 5.1 h = 118 km/h 均速，含隧道群，物理上不可能）。
# 出来的建议看着漂亮，实际会把人带沟里。
#
# 所以：把振幅按里程位置衰减 ——
#   靠近起终点（城市/枢纽，约 URBAN_KM 公里内）保留完整振幅
#   中间纯高速段只保留 RURAL_AMP 的比例
# 振幅本身（哪个小时更堵）仍是先验，衰减比例是这条路线自己的性质。
# ---------------------------------------------------------------------------
URBAN_KM = 60.0          # 城市影响沿线的衰减尺度（公里）
RURAL_AMP = 0.22         # 纯高速段保留的先验振幅比例
MAX_AVG_SPEED = 115.0    # 物理护栏：全程平均车速不可能超过这个值（km/h）

WINDOW_HOURS = list(range(24))
BASIS_CODE = {"经验推算": 0, "实测+经验": 1, "实测为主": 2}
BASIS_NAME = {v: k for k, v in BASIS_CODE.items()}


def num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def norm_day_type(t: str) -> str:
    return "workday" if t in ("makeup", None, "") else t


def prior_factor(day_type: str, hour: int) -> float:
    day_type = norm_day_type(day_type)
    if day_type == "holiday":
        return round(PRIOR_FACTORS["weekend"][hour] * HOLIDAY_PRIOR, 3)
    return PRIOR_FACTORS.get(day_type, PRIOR_FACTORS["workday"])[hour]


def position_amp(pos: float, total_km: float) -> float:
    """线路位置 pos∈[0,1] 处，先验振幅应保留多少。

    两端（城市/枢纽）→ 1.0；中间（纯高速）→ RURAL_AMP。
    """
    import math
    if total_km <= 0:
        return 1.0
    u = min(0.30, URBAN_KM / total_km)          # 60 km 换算成占全程的比例
    if u <= 0:
        return RURAL_AMP
    w = max(math.exp(-((pos / u) ** 2)), math.exp(-(((1.0 - pos) / u) ** 2)))
    return RURAL_AMP + (1.0 - RURAL_AMP) * w


def damp_prior(pf: float, amp: float) -> float:
    """把先验倍数按振幅压缩：1.39 × amp=0.33 → 1.13。"""
    return 1.0 + (pf - 1.0) * amp


def route_profile(bins: list):
    """由路线几何算出 (全程平均振幅, 逐段振幅)。

    逐段振幅给「逐段推进」用 —— 出发点在深圳时，深圳那几段按城市振幅算；
    跑到粤北山里时，那几段按 RURAL_AMP 算。这样才对得上真实体验。
    """
    dists = [max(0.0, num(b.get("distance_km"))) for b in bins or []]
    total = sum(dists)
    if total <= 0:
        return 1.0, [1.0 for _ in dists]
    amps, cum = [], 0.0
    for d in dists:
        cum += d
        pos = (cum - d / 2.0) / total if d > 0 else (cum / total)
        amps.append(position_amp(pos, total))
    avg = sum(a * d for a, d in zip(amps, dists)) / total
    return round(avg, 3), amps


def day_type_of(dt: datetime, cfg: dict) -> str:
    hol = cfg["holiday"]
    ds = dt.strftime("%Y-%m-%d")
    if ds in (hol.get("makeup_workdays") or []):
        return "workday"
    if ds in (hol.get("holiday_days") or []):
        return "holiday"
    if dt.weekday() >= 5:
        return "weekend"
    return "workday"


# ---------------- 读取与迁移 ----------------

def migrate_record(rec: dict, cfg: dict, direction: str) -> dict:
    """把「单路线」时代的老记录，升级成「多走廊」结构。

    仅保留兼容读取能力。路线版本变更后，这类旧记录会由 usable() 排除，
    不会与新途经点产生的数据混在同一条基线里。
    """
    if rec.get("corridors"):
        return rec
    routes = rec.get("routes") or []
    if not routes:
        return rec

    trip = cfg["trip"] if direction == "outbound" else cfg.get("return_trip") or {}
    cors = iter_corridors(trip) if trip else []
    if not cors:
        return rec
    rec_id = next((c["id"] for c in cors if c["recommended"]), cors[0]["id"])
    info = next(c for c in cors if c["id"] == rec_id)

    best = min(routes, key=lambda r: num(r.get("duration_min"), 1e9))
    item = {
        "id": rec_id, "name": info["name"], "short": info["short"],
        "recommended": True, "waypoints": info["waypoints"], "ok": True,
        "duration_min": best.get("duration_min"), "distance_km": best.get("distance_km"),
        "tolls": best.get("tolls"), "toll_distance_km": best.get("toll_distance_km"),
        "traffic_lights": best.get("traffic_lights"), "arrive_at": best.get("arrive_at"),
        "total_km": best.get("total_km"), "roads": best.get("roads"),
        "bins": best.get("bins"),
    }
    if best.get("polyline"):
        item["polyline"] = best["polyline"]
        item["cum_km"] = best.get("cum_km")

    out = dict(rec)
    out.pop("routes", None)
    out["corridors"] = [item]
    out["fastest"] = rec_id
    out["legacy"] = True
    return out


def load_records(raw_dir: Path, cfg: dict) -> dict:
    out = {d: [] for d in DIRS}
    if not raw_dir.exists():
        return out
    for f in sorted(raw_dir.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            d = rec.get("direction", "outbound")
            if d not in out:
                continue
            out[d].append(migrate_record(rec, cfg, d))
    for d in out:
        out[d].sort(key=lambda r: r["ts"])
    return out


def corridor_of(rec: dict, cid: str) -> dict:
    for c in rec.get("corridors") or []:
        if c.get("id") == cid and c.get("ok"):
            return c
    return {}


def corridor_attempt(rec: dict, cid: str) -> dict:
    """取某轮的走廊结果，包括 ok=False 的失败结果。"""
    for c in rec.get("corridors") or []:
        if c.get("id") == cid:
            return c
    return {}


def rec_latest(records: list, cid: str) -> dict:
    for rec in reversed(records):
        c = corridor_of(rec, cid)
        if c:
            return {"rec": rec, "corridor": c}
    return {}


def usable(rec: dict, cfg: dict) -> bool:
    """这条记录能不能参与建模。

    只有 source == "amap" 才是真实采集。仿真/夹具数据一旦混进基线，
    「比平常更堵」就变成自己跟自己比，结论全废。所以默认一律排除，
    想看带历史的演示效果再显式打开 analysis.include_simulated。
    """
    src = rec.get("source") or "amap"
    source_ok = (src == "amap" or
                 bool((cfg.get("analysis") or {}).get("include_simulated", False)))
    return source_ok and route_version_matches(rec, cfg)


def route_version_matches(rec: dict, cfg: dict) -> bool:
    direction = rec.get("direction", "outbound")
    trip = cfg["trip"] if direction == "outbound" else cfg.get("return_trip") or {}
    expected_version = int(trip.get("route_version", 1))
    actual_version = int(rec.get("route_version", 1))
    return actual_version == expected_version


def within_baseline_window(records: list, cfg: dict) -> list:
    """只保留最近配置窗口内的样本，避免数月前的数据永久污染基线。"""
    if not records:
        return []
    days = int((cfg.get("analysis") or {}).get("baseline_window_days", 14))
    newest = max(datetime.fromisoformat(r["ts"]) for r in records)
    cutoff = newest - timedelta(days=max(1, days))
    return [r for r in records if datetime.fromisoformat(r["ts"]) >= cutoff]


# ---------------- 单走廊基线 ----------------

def build_baseline(records: list, cfg: dict, cid: str) -> dict:
    seg_hours = {}      # day_type -> hour -> [duration_min]
    anchors = []        # 用先验形状反推出来的「畅通耗时」候选值

    # 先拿到这条走廊的几何剖面，才知道先验振幅该压多少
    latest = rec_latest(records, cid)
    bins = latest["corridor"].get("bins") if latest else None
    avg_amp, seg_amps = route_profile(bins or [])
    total_km = sum(num(b.get("distance_km")) for b in (bins or [])) or 0.0

    for rec in records:
        c = corridor_of(rec, cid)
        if not c:
            continue
        dt = datetime.fromisoformat(rec["ts"])
        t = norm_day_type(rec.get("day_type", "workday"))
        dur = num(c.get("duration_min"))
        if dur <= 0:
            continue
        seg_hours.setdefault(t, {}).setdefault(dt.hour, []).append(dur)
        pf = prior_factor(t, dt.hour)
        if pf > 0:
            anchors.append(dur / damp_prior(pf, avg_amp))

    obs_median = {t: {h: statistics.median(v) for h, v in hh.items()}
                  for t, hh in seg_hours.items()}
    obs_n = {t: {h: len(v) for h, v in hh.items()} for t, hh in seg_hours.items()}

    # 畅通基准：实测最小值 与 先验反推值 按覆盖度加权混合
    anchor_ff = statistics.median(anchors) if anchors else None
    all_obs = [v for hh in obs_median.values() for v in hh.values()]
    ff_obs = min(all_obs) if all_obs else None
    covered = set()
    for hh in obs_median.values():
        covered |= set(hh.keys())
    weight = min(1.0, len(covered) / 12.0)      # 覆盖满 12 个不同小时才算「实测可信」
    if anchor_ff and ff_obs:
        freeflow = weight * ff_obs + (1 - weight) * anchor_ff
    else:
        freeflow = ff_obs or anchor_ff

    # 物理护栏：全程均速不可能超过 MAX_AVG_SPEED
    floor_min = (total_km / MAX_AVG_SPEED * 60.0) if total_km > 0 else None
    if freeflow and floor_min:
        freeflow = max(freeflow, floor_min)

    # 时段因子：在每个小时上把先验和实测做收缩估计（先验已按振幅压缩）
    factors, detail = {}, {}
    for t in ("workday", "weekend", "holiday"):
        factors[t], detail[t] = {}, {}
        for h in range(24):
            raw_pf = prior_factor(t, h)
            pf = damp_prior(raw_pf, avg_amp)
            med = (obs_median.get(t) or {}).get(h)
            n = (obs_n.get(t) or {}).get(h, 0)
            if med and freeflow:
                fo = med / freeflow
                f = (n * fo + PRIOR_K * pf) / (n + PRIOR_K)
            else:
                fo, f = None, pf
            factors[t][h] = round(f, 3)
            detail[t][h] = {"obs": round(fo, 3) if fo else None, "n": n,
                            "prior": round(pf, 3), "raw_prior": raw_pf,
                            "value": round(f, 3)}

    return {
        "freeflow_min": round(freeflow, 1) if freeflow else None,
        "factors": factors,
        "detail": detail,
        "samples": obs_n,
        "maturity": round(weight, 2),
        "prior_k": PRIOR_K,
        "covered_hours": len(covered),
        "avg_amp": avg_amp,
        "seg_amps": seg_amps,
        "total_km": round(total_km, 1),
        "rural_amp": RURAL_AMP,
        "urban_km": URBAN_KM,
    }


def factor_at(baseline: dict, day_type: str, hour: int, amp: float = None) -> tuple:
    """取某小时的综合因子。返回 (factor, 依据标签)。

    amp 传入时会用该位置的振幅重算先验再收缩 —— 逐段推进就是这么干的：
    同一小时，深圳那几段和粤北山里那几段，压缩比例不一样。
    """
    day_type = norm_day_type(day_type)
    amp_use = baseline.get("avg_amp", 1.0) if amp is None else amp
    det = (baseline.get("detail") or {}).get(day_type) or {}
    d = det.get(hour) or det.get(str(hour))
    raw = (d or {}).get("raw_prior") or prior_factor(day_type, hour)
    pf = damp_prior(raw, amp_use)
    n = (d or {}).get("n", 0)
    obs = (d or {}).get("obs")
    k = baseline.get("prior_k", PRIOR_K)
    if obs is not None and n > 0:
        value = round((n * obs + k * pf) / (n + k), 3)
        # n >= 3K 时实测权重达到 75%，此前不能把结果简称为“实测”。
        label = "实测为主" if n >= 3 * k else "实测+经验"
        return value, label
    f = (baseline.get("factors") or {}).get(day_type) or {}
    fallback = f.get(hour) or f.get(str(hour))
    if fallback is not None and amp is None:
        return fallback, "经验推算"
    return round(pf, 3), "经验推算"


# ---------------- 分段对比（单走廊） ----------------

def segment_compare(records: list, cfg: dict, cid: str, now: datetime) -> dict:
    """把最新快照与相同日期类型、相近小时的历史样本比较。"""
    seg_n = cfg["analysis"]["segment_count"]
    recent, base_acc = {}, {}
    latest = rec_latest(records, cid)
    if not latest:
        return []
    latest_rec = latest["rec"]
    latest_dt = datetime.fromisoformat(latest_rec["ts"])
    latest_type = norm_day_type(latest_rec.get("day_type") or day_type_of(latest_dt, cfg))

    # “近期”只取最新一次快照，避免把过去 24 小时不同时段混成一个中位数。
    for b in latest["corridor"].get("bins") or []:
        i = b.get("index")
        if i is not None and b.get("distance_km"):
            recent.setdefault(i, []).append(
                num(b["duration_min"]) / num(b["distance_km"]))

    for rec in records:
        if rec is latest_rec:
            continue
        c = corridor_of(rec, cid)
        if not c:
            continue
        dt = datetime.fromisoformat(rec["ts"])
        rec_type = norm_day_type(rec.get("day_type") or day_type_of(dt, cfg))
        hour_gap = abs(dt.hour - latest_dt.hour)
        hour_gap = min(hour_gap, 24 - hour_gap)
        if rec_type != latest_type or hour_gap > 1:
            continue
        for b in c.get("bins") or []:
            i = b.get("index")
            if i is None or not b.get("distance_km"):
                continue
            # 用「分钟/公里」做可比量，消除每段长度差异
            base_acc.setdefault(i, []).append(
                num(b["duration_min"]) / num(b["distance_km"]))

    out = []
    for i in range(1, seg_n + 1):
        r_vals, b_vals = recent.get(i) or [], base_acc.get(i) or []
        r_med = statistics.median(r_vals) if r_vals else None
        b_med = statistics.median(b_vals) if b_vals else None
        out.append({
            "index": i,
            "recent_min_per_km": round(r_med, 4) if r_med else None,
            "base_min_per_km": round(b_med, 4) if b_med else None,
            "delta_pct": (round((r_med - b_med) / b_med * 100, 1) if r_med and b_med else None),
            "has_baseline": bool(b_vals),
            "recent_samples": len(r_vals),
            "base_samples": len(b_vals),
        })

    if latest:
        for seg, b in zip(out, latest["corridor"].get("bins") or []):
            seg["road"] = b.get("road", "")
            seg["start_km"] = b.get("start_km")
            seg["end_km"] = b.get("end_km")
            seg["status_mix"] = b.get("status_mix") or {}
            seg["speed_kmh"] = b.get("speed_kmh")
            seg["distance_km"] = b.get("distance_km")
            seg["efficiency"] = b.get("efficiency")
    return out


# ---------------- 出发窗口推算（单走廊） ----------------

def project_corridor(records: list, baseline: dict, cfg: dict, direction: str,
                     cid: str, now: datetime) -> dict:
    """对每个候选日期 × 每个小时，推算这条走廊的全程耗时。

    逐段推进：每一段用「车实际走到那一段的那个小时」的综合因子放大。
    600 公里要跑 8 小时，拿出发时刻的拥堵一刀切是完全错的。
    """
    empty = {"cells": [], "best": [], "freeflow_min": baseline.get("freeflow_min")}
    latest = rec_latest(records, cid)
    if not latest:
        return empty
    bins = latest["corridor"].get("bins") or []
    if not bins:
        return empty

    now_type = norm_day_type(latest["rec"].get("day_type", "workday"))
    now_factor, _ = factor_at(baseline, now_type, now.hour)

    # 逐段的先验振幅：深圳那几段按城市算，跑到粤北山里就压下去
    dists = [max(0.0, num(b.get("distance_km"))) for b in bins]
    total_km = sum(dists)
    cum, seg_amps = 0.0, []
    for dkm in dists:
        cum += dkm
        pos = ((cum - dkm / 2.0) / total_km) if total_km > 0 and dkm > 0 else (cum / total_km if total_km else 0.5)
        seg_amps.append(position_amp(pos, total_km))

    # 把最新一次采样还原成「畅通耗时」：除掉采样时刻的因子
    freeflow_bins = []
    for b, dkm, amp in zip(bins, dists, seg_amps):
        d = num(b["duration_min"])
        if d <= 0 or dkm <= 0:
            continue
        freeflow_bins.append({"km": dkm, "min": d / max(now_factor, 0.01), "amp": amp})
    if not freeflow_bins:
        return empty

    # 校准到与基线一致的畅通基准，避免看板上两个口径互相打架
    base_free = baseline.get("freeflow_min")
    raw_total = sum(b["min"] for b in freeflow_bins)
    if base_free and raw_total > 0:
        k = base_free / raw_total
        for b in freeflow_bins:
            b["min"] *= k

    freeflow_total = sum(b["min"] for b in freeflow_bins)
    candidates = (cfg["holiday"]["outbound_candidates"] if direction == "outbound"
                  else cfg["holiday"]["return_candidates"])
    arrival_targets = set(cfg["holiday"].get("return_arrival_candidates") or [])
    free_start = datetime.fromisoformat(cfg["holiday"]["free_start"])
    free_end = datetime.fromisoformat(cfg["holiday"]["free_end"])

    cells, best = [], []
    for date_i, ds in enumerate(candidates):
        day = datetime.fromisoformat(ds + "T00:00:00+08:00")
        dt_type = day_type_of(day, cfg)
        row_m, row_f, row_b = [], [], []
        for hour in WINDOW_HOURS:
            depart = day + timedelta(hours=hour)
            elapsed, bases = 0.0, set()
            for b in freeflow_bins:
                segment_time = depart + timedelta(minutes=elapsed)
                h = int(segment_time.hour)
                # 跨过午夜后要按新的日期类型计算，不能沿用出发日。
                segment_type = day_type_of(segment_time, cfg)
                f, basis = factor_at(baseline, segment_type, h, b.get("amp"))
                bases.add(basis)
                elapsed += b["min"] * f
            arrive = depart + timedelta(minutes=elapsed)
            allowed_arrival = (direction != "return" or not arrival_targets or
                               arrive.strftime("%Y-%m-%d") in arrival_targets)
            if not allowed_arrival:
                row_m.append(None)
                row_f.append(0)
                row_b.append(BASIS_CODE["经验推算"])
                continue
            free_toll = bool(free_start <= arrive < free_end)
            basis_name = ("实测为主" if bases == {"实测为主"} else
                          "经验推算" if bases == {"经验推算"} else "实测+经验")
            row_m.append(round(elapsed, 1))
            row_f.append(1 if free_toll else 0)
            row_b.append(BASIS_CODE[basis_name])
            best.append({
                "corridor": cid,
                "date": ds, "date_i": date_i, "hour": hour,
                "weekday_cn": WEEKDAY_CN[day.weekday()], "day_type": dt_type,
                "est_min": round(elapsed, 1), "est_h": round(elapsed / 60, 2),
                "vs_freeflow": round(elapsed / freeflow_total, 2) if freeflow_total else None,
                "depart": depart.isoformat(timespec="minutes"),
                "arrive": arrive.isoformat(timespec="minutes"),
                "free_toll": free_toll,
                "overnight": bool(arrive.date() != depart.date()),
                "basis": basis_name,
            })
        cells.append({"m": row_m, "f": row_f, "b": row_b})

    n_keep = int(cfg["analysis"].get("best_windows_per_corridor", 6))
    best.sort(key=lambda w: w["est_min"])
    return {
        "cells": cells,
        "best": best[:n_keep],
        "freeflow_min": baseline.get("freeflow_min"),
    }


# ---------------- 紧凑化 ----------------

def compact_bins(bins: list) -> list:
    """分段数据瘦身，但保留 status_mix —— 色带要按里程占比堆叠显示。

    只取占比最高的那个状态是不够的：实测出现过一段里 48% 畅通、20% 缓行、
    15% 拥堵、17% 严重拥堵，取「占比最高」会被画成纯绿色，把一半的堵藏起来。
    """
    out = []
    for b in bins or []:
        mix = b.get("status_mix") or {}
        out.append({
            "i": b.get("index"),
            "road": b.get("road", ""),
            "dist": b.get("distance_km"),
            "km0": b.get("start_km"),
            "km1": b.get("end_km"),
            "spd": b.get("speed_kmh"),
            "eff": b.get("efficiency"),
            "min": b.get("duration_min"),
            "mix": {k: round(v, 3) for k, v in mix.items() if v >= 0.005},
        })
    return out


def compact_segments(segs: list) -> list:
    out = []
    for s in segs:
        item = {
            "i": s.get("index"),
            "road": s.get("road", ""),
            "delta": s.get("delta_pct"),
            "recent": s.get("recent_min_per_km"),
            "base": s.get("base_min_per_km"),
            "spd": s.get("speed_kmh"),
            "km0": s.get("start_km"),
            "km1": s.get("end_km"),
        }
        if s.get("status_mix"):
            item["mix"] = s["status_mix"]
        out.append(item)
    return out


# ---------------- 汇总一个方向 ----------------

def analyze_direction(records: list, cfg: dict, direction: str, now: datetime) -> dict:
    trip = cfg["trip"] if direction == "outbound" else cfg.get("return_trip") or {}
    corridors = iter_corridors(trip)
    rec_id = next((c["id"] for c in corridors if c["recommended"]), corridors[0]["id"])

    out = {
        "trip": {
            "name": trip.get("name", ""),
            "origin_name": trip.get("origin_name", ""),
            "destination_name": trip.get("destination_name", ""),
            "route_version": int(trip.get("route_version", 1)),
        },
        "strategy": int(cfg["collect"].get("strategy", 0)),
        "updated_at": None,
        "dates": [],
        "corridors": [],
    }

    cands = (cfg["holiday"]["outbound_candidates"] if direction == "outbound"
             else cfg["holiday"]["return_candidates"])
    for ds in cands:
        day = datetime.fromisoformat(ds + "T00:00:00+08:00")
        out["dates"].append({"date": ds, "weekday_cn": WEEKDAY_CN[day.weekday()],
                             "day_type": day_type_of(day, cfg)})

    all_best, nows = [], []
    # 只有可入模的记录（默认真实采集）才进基线 / 推算 / 分段对比
    version_recs = [r for r in records if route_version_matches(r, cfg)]
    current_recs = [r for r in version_recs if (r.get("source") or "amap") == "amap"]
    model_recs = within_baseline_window([r for r in version_recs if usable(r, cfg)], cfg)
    out["updated_at"] = current_recs[-1]["ts"] if current_recs else None
    if not model_recs:
        print("        ⚠ %s 没有任何可入模的真实采样，只能靠先验" % direction)
    elif len(model_recs) < len(records):
        print("        ⚠ %s 有 %d 条非真实记录已被排除在基线之外（看板会提示清理）"
              % (direction, len(records) - len(model_recs)))

    for idx, info in enumerate(corridors):
        cid = info["id"]
        recs = [r for r in model_recs if corridor_of(r, cid)]
        baseline = build_baseline(model_recs, cfg, cid)
        proj = project_corridor(model_recs, baseline, cfg, direction, cid, now)
        segs = segment_compare(model_recs, cfg, cid, now)
        latest = rec_latest(model_recs, cid)
        latest_attempt = {}
        for rec in reversed(current_recs):
            attempted = corridor_attempt(rec, cid)
            if attempted:
                latest_attempt = {"rec": rec, "corridor": attempted}
                break

        # 首采基准：该走廊最早一次入模采样，用来做「今天比首采快/慢」的参照
        first = next((corridor_of(r, cid) for r in model_recs if corridor_of(r, cid)), {})
        attempt_rec = latest_attempt.get("rec") or {}
        attempt_c = latest_attempt.get("corridor") or {}
        sampled_at = attempt_rec.get("ts")
        age_min = ((now - datetime.fromisoformat(sampled_at)).total_seconds() / 60
                   if sampled_at else None)
        max_age = int((cfg.get("analysis") or {}).get("max_current_age_minutes", 45))
        fresh = bool(attempt_c.get("ok") and age_min is not None and
                     -5 <= age_min <= max_age)
        now_c = attempt_c if fresh else {}
        current_error = attempt_c.get("error")
        if attempt_c.get("ok") and not fresh:
            current_error = "数据已超过 %d 分钟，已停止参与当前排名" % max_age
        if not fresh:
            segs = []

        entry = {
            "id": cid,
            "name": info["name"],
            "short": info["short"],
            "note": info["note"],
            "recommended": bool(info["recommended"]),
            "waypoints": info["waypoints"],
            "idx": idx,
            "window_rows": len(proj["cells"]),
            "cells": proj["cells"],
            "best": proj["best"],
            "freeflow_min": proj["freeflow_min"],
            "baseline": {
                "freeflow_min": baseline["freeflow_min"],
                "factors": baseline["factors"],
                "samples": baseline["samples"],
                "maturity": baseline["maturity"],
                "covered_hours": baseline["covered_hours"],
                "prior_k": baseline["prior_k"],
                "avg_amp": baseline["avg_amp"],
                "rural_amp": baseline["rural_amp"],
                "urban_km": baseline["urban_km"],
                "total_km": baseline["total_km"],
            },
            "coverage": {
                "records": len(recs),
                "days": len({r["date"] for r in recs}),
                "hours_covered": baseline["covered_hours"],
                "maturity": baseline["maturity"],
                "real": len(recs),          # 能进模型的都是真实采集
                "simulated": 0,             # 非真实记录已在入口被排除
                "first": recs[0]["ts"] if recs else None,
                "last": recs[-1]["ts"] if recs else None,
            },
            "now": {
                "ok": fresh,
                "stale": bool(attempt_c.get("ok") and not fresh),
                "sampled_at": sampled_at,
                "age_minutes": round(age_min, 1) if age_min is not None else None,
                "duration_min": now_c.get("duration_min"),
                "distance_km": now_c.get("distance_km"),
                "tolls": now_c.get("tolls"),
                "traffic_lights": now_c.get("traffic_lights"),
                "speed_kmh": now_c.get("speed_kmh"),
                "arrive_at": now_c.get("arrive_at"),
                "roads": now_c.get("roads") or [],
                "error": current_error,
            },
            "reference": {
                "duration_min": first.get("duration_min"),
                "distance_km": first.get("distance_km"),
                "tolls": first.get("tolls"),
            },
            "bins": compact_bins(now_c.get("bins")),
            "segments": compact_segments(segs),
            "trend": [{
                "ts": r["ts"],
                "hour": datetime.fromisoformat(r["ts"]).hour,
                "duration_min": num(corridor_of(r, cid).get("duration_min")),
                "distance_km": num(corridor_of(r, cid).get("distance_km")),
                "day_type": r.get("day_type"),
                "source": r.get("source") or "amap",
            } for r in recs],
            "polyline": now_c.get("polyline"),
            "cum_km": now_c.get("cum_km"),
        }
        out["corridors"].append(entry)
        all_best.extend(proj["best"])
        if fresh and now_c.get("duration_min"):
            nows.append({"id": cid, "short": info["short"], "recommended": bool(info["recommended"]),
                         "duration_min": now_c["duration_min"], "distance_km": now_c.get("distance_km"),
                         "tolls": now_c.get("tolls")})

    # ---- 此刻横向比 ----
    if nows:
        fastest = min(nows, key=lambda x: x["duration_min"])
        for n in nows:
            n["gap_min"] = round(n["duration_min"] - fastest["duration_min"], 1)
            n["gap_km"] = round((n.get("distance_km") or 0) - (fastest.get("distance_km") or 0), 1)
        nows.sort(key=lambda x: x["duration_min"])
        for i, n in enumerate(nows):
            n["rank"] = i + 1
        out["now_table"] = nows
        out["fastest_now"] = fastest["id"]
        out["now_spread_min"] = round(
            max(x["duration_min"] for x in nows) - fastest["duration_min"], 1)

        # 各走廊条目里补上排名与差距，看板直接读
        rank = {n["id"]: n for n in nows}
        for c in out["corridors"]:
            r = rank.get(c["id"])
            c["rank"] = r["rank"] if r else None
            c["gap_min"] = r["gap_min"] if r else None
            c["gap_km"] = r["gap_km"] if r else None

    # ---- 全走廊最佳窗口榜（回答「哪天几点走哪条」）----
    all_best.sort(key=lambda w: w["est_min"])
    short_of = {c["id"]: c["short"] for c in out["corridors"]}
    for w in all_best:
        w["corridor_short"] = short_of.get(w["corridor"], w["corridor"])
    out["best_overall"] = all_best[:12]

    # ---- 覆盖度（总体）----
    real = current_recs
    out["coverage"] = {
        "records": len(version_recs),
        "real": len(real),
        "simulated": len(version_recs) - len(real),
        "excluded_old_route": len(records) - len(version_recs),
        "first": version_recs[0]["ts"] if version_recs else None,
        "last": version_recs[-1]["ts"] if version_recs else None,
        "days": len({r["date"] for r in version_recs}),
        "hours_covered": max([c["coverage"]["hours_covered"] for c in out["corridors"]] or [0]),
        "maturity": max([c["coverage"]["maturity"] for c in out["corridors"]] or [0]),
    }
    out["arrival_constraint"] = (
        cfg["holiday"].get("return_arrival_candidates") if direction == "return" else None)
    latest_current = current_recs[-1] if current_recs else {}
    out["route_warning"] = bool(
        latest_current.get("route_signature_checked")
        and num(latest_current.get("route_signature_similarity"), 1.0) < 0.6)
    out["model_note"] = (
        "逐段推进推算：每段耗时用「车走到该段的那个小时」的时段因子放大，"
        "而不是拿出发时刻一刀切。每条走廊各自建一套基线，避免不同通道互相污染。"
        "先验振幅按里程位置衰减：起终点 60 km 内按城市（振幅 100%），中间纯高速只保留 " +
        str(round(RURAL_AMP * 100)) + "%，"
        "否则会推出「凌晨 2 点 5 小时跑完 600 km」这种物理上不可能的结论。"
    )
    out["recommended_id"] = rec_id
    return out


def main() -> None:
    import yaml
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    now = datetime.now(TZ)
    raw_dir = ROOT / "data" / "raw"
    out_path = ROOT / cfg["output"]["latest_file"]

    records = load_records(raw_dir, cfg)

    merged = {}
    if out_path.exists():
        try:
            merged = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:
            merged = {}

    total = 0
    for direction, trip in iter_trips(cfg):
        recs = records.get(direction) or []
        result = analyze_direction(recs, cfg, direction, now)
        merged.setdefault(direction, {}).clear()
        merged[direction].update(result)
        total += result["coverage"]["records"]

        cov = result["coverage"]
        print("  [%s] %d 条采样 / %d 天 / 成熟度 %.0f%%"
              % (direction, cov["records"], cov["days"], cov["maturity"] * 100))
        for c in result["corridors"]:
            n = c["now"]
            if n["ok"]:
                print("        %s %-12s %6.1f km %5.2f h ¥%-4.0f  比最快 +%s 分 %s"
                      % ("★" if c["recommended"] else " ", c["short"],
                         n["distance_km"], n["duration_min"] / 60, n["tolls"],
                         c["gap_min"], ("[%d]" % c["rank"]) if c["rank"] else ""))
            else:
                print("        %s %-12s 本轮无数据（%s）"
                      % ("★" if c["recommended"] else " ", c["short"], n.get("error") or "—"))
        if result.get("best_overall"):
            b = result["best_overall"][0]
            print("        最佳窗口：%s %s %02d:00 走 %s，约 %.1f 小时%s（依据 %s）"
                  % (b["date"], b["weekday_cn"], b["hour"], b["corridor_short"],
                     b["est_h"], "，免费" if b["free_toll"] else "", b["basis"]))

    merged["generated_at"] = now.isoformat(timespec="seconds")
    merged["total_records"] = total
    merged["segment_count"] = cfg["analysis"]["segment_count"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(merged, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8")
    print("✓ 写出 %s（%.1f KB）" % (out_path.relative_to(ROOT), out_path.stat().st_size / 1024))


if __name__ == "__main__":
    main()
