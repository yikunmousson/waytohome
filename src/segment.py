"""把高德返回的路径，归一化成「固定 N 段」的可比结构。

为什么一定要自己切段？
    高德每次返回的 steps 粒度并不固定（有时几十个碎段），分段边界天天在变，
    直接对比毫无意义。所以统一按里程等分切成 N 段 ——
    保证今天的第 7 段和明天的第 7 段，指的就是路上同一段路。

每段产出三个可比指标：
    speed_kmh          平均车速
    efficiency         按里程加权的通行效率（1.0 = 全程畅通）
    congestion_ratio   相对耗时倍数 = 1 / efficiency（2.0 表示这段要花双倍时间）
"""

import bisect
import math

from geo import cumulative_km, parse_polyline

# 路况状态 -> 相对通行效率（以畅通为 1.0）。数值是经验系数，只用于横向比较。
STATUS_EFFICIENCY = {
    "畅通": 1.00,
    "缓行": 0.55,
    "拥堵": 0.30,
    "严重拥堵": 0.15,
    "未知": None,
}
STATUS_ORDER = ["畅通", "缓行", "拥堵", "严重拥堵", "未知"]


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _step_view(st: dict) -> dict:
    """抹平 v3 / v5 的字段差异。"""
    cost = st.get("cost") or {}
    return {
        "road": (st.get("road") or st.get("road_name") or "").strip(),
        "distance_m": _num(st.get("distance") or st.get("step_distance")),
        "duration_s": _num(st.get("duration") or cost.get("duration")),
        "polyline": st.get("polyline") or "",
    }


def _tmc_view(t: dict) -> dict:
    return {
        "status": (t.get("status") or t.get("tmc_status") or "未知").strip(),
        "distance_m": _num(t.get("distance") or t.get("tmc_distance")),
        "polyline": t.get("polyline") or t.get("tmc_polyline") or "",
    }


def _collect_tmcs(path: dict) -> list:
    """v3 的 tmcs 挂在 path 上，v5 的 tmcs 嵌在每个 step 里 —— 两种都收。"""
    out = [_tmc_view(t) for t in (path.get("tmcs") or [])]
    for st in path.get("steps") or []:
        for t in st.get("tmcs") or []:
            out.append(_tmc_view(t))
    return [t for t in out if t["polyline"] and t["distance_m"] > 0]


def _nearest_km(pts: list, cum: list, target: list, stride: int = 8) -> float:
    """把 tmc 的起点投到路线上，返回它对应的里程（公里）。

    经度方向按 cos(lat) 压缩，否则高纬度处会往东西方向偏。
    """
    if not pts:
        return 0.0
    k = math.cos(math.radians(target[1]))
    n = len(pts)
    best_i, best_d = 0, float("inf")

    def d2(i):
        dx = (pts[i][0] - target[0]) * k
        dy = pts[i][1] - target[1]
        return dx * dx + dy * dy

    for i in range(0, n, stride):
        v = d2(i)
        if v < best_d:
            best_d, best_i = v, i
    for i in range(max(0, best_i - stride), min(n, best_i + stride + 1)):
        v = d2(i)
        if v < best_d:
            best_d, best_i = v, i
    return cum[best_i]


def _add_span(bins: list, km0: float, km1: float, distance_m: float, key: str, field: str) -> None:
    """把 [km0, km1] 这一段按里程比例摊到各 bin 的 field[key] 上。"""
    if km1 <= km0:
        return
    per_km = distance_m / (km1 - km0)
    for b in bins:
        lo = max(km0, b["start_km"])
        hi = min(km1, b["end_km"])
        if hi <= lo:
            continue
        share = per_km * (hi - lo)
        b[field][key] = b[field].get(key, 0.0) + share


def build_path_analysis(path: dict, segment_count: int) -> dict:
    """path = route.paths[i]。返回 {total_km, bins[], roads[], polyline, cum_km}"""

    # ---- 1. 拼出整条路线几何，并记录每个 step 落在哪一段点集上 ----
    pts, spans = [], []
    for raw in path.get("steps") or []:
        st = _step_view(raw)
        p = parse_polyline(st["polyline"])
        if not p:
            continue
        if pts and pts[-1] == p[0]:
            p = p[1:]
        if not p:
            continue
        start = len(pts)
        pts.extend(p)
        spans.append((start, len(pts) - 1, st))

    if len(pts) < 2:
        return {}

    cum = cumulative_km(pts)
    total_km = cum[-1]
    if total_km <= 0:
        return {}

    step_km = total_km / segment_count
    bins = []
    for i in range(segment_count):
        bins.append({
            "index": i + 1,
            "start_km": round(i * step_km, 3),
            "end_km": round(min((i + 1) * step_km, total_km), 3),
            "distance_m": 0.0,
            "duration_s": 0.0,
            "road_m": {},
            "status_m": {},
        })

    # ---- 2. 把每个 step 的里程与耗时，按几何重叠比例摊到各 bin ----
    for s_i, e_i, st in spans:
        a, b = cum[s_i], cum[e_i]
        if b - a <= 1e-9:
            continue
        road = st["road"] or "未命名路段"
        _add_span(bins, a, b, st["distance_m"], road, "road_m")
        frac_m = st["distance_m"]
        for bin_ in bins:
            lo = max(a, bin_["start_km"])
            hi = min(b, bin_["end_km"])
            if hi <= lo:
                continue
            bin_["distance_m"] += frac_m * (hi - lo) / (b - a)
            bin_["duration_s"] += st["duration_s"] * (hi - lo) / (b - a)

    # ---- 3. 把 tmcs 路况色带摊到各 bin ----
    for t in _collect_tmcs(path):
        p = parse_polyline(t["polyline"])
        if not p:
            continue
        km0 = _nearest_km(pts, cum, p[0])
        # tmc 起点偶尔落在路线尾部之外，夹一下
        km0 = max(0.0, min(km0, total_km))
        km1 = min(km0 + t["distance_m"] / 1000.0, total_km)
        _add_span(bins, km0, km1, t["distance_m"], t["status"], "status_m")

    # ---- 4. 结算每段的可比指标 ----
    for b in bins:
        b["mid_km"] = round((b["start_km"] + b["end_km"]) / 2, 2)
        b["distance_km"] = round(b["distance_m"] / 1000, 2)
        b["duration_min"] = round(b["duration_s"] / 60, 1)
        b["speed_kmh"] = (round(b["distance_m"] / b["duration_s"] * 3.6, 1)
                          if b["duration_s"] > 0 else None)

        tot = sum(b["status_m"].values())
        b["status_mix"] = ({k: round(v / tot, 3) for k, v in b["status_m"].items()}
                           if tot > 0 else {})

        eff, weight = 0.0, 0.0
        for k, v in b["status_m"].items():
            f = STATUS_EFFICIENCY.get(k)
            if f is None:
                continue
            eff += f * v
            weight += v
        b["efficiency"] = round(eff / weight, 3) if weight > 0 else None
        b["congestion_ratio"] = round(1.0 / b["efficiency"], 2) if b["efficiency"] else None

        b["road"] = max(b["road_m"], key=b["road_m"].get) if b["road_m"] else ""
        del b["road_m"]
        del b["status_m"]

    # ---- 5. 道路序列（用于检测高德今天是不是偷偷换了路线）----
    roads = []
    for _, _, st in spans:
        r = st["road"]
        if r and (not roads or roads[-1] != r):
            roads.append(r)

    # ---- 6. 抽稀后的路线几何（给看板画线用）----
    stride = max(1, len(pts) // 700)
    shape_pts = pts[::stride]
    shape_cum = cum[::stride]
    if (shape_pts[-1] != pts[-1]):
        shape_pts.append(pts[-1])
        shape_cum.append(cum[-1])

    return {
        "total_km": round(total_km, 1),
        "bins": bins,
        "roads": roads,
        "polyline": [[round(x, 5), round(y, 5)] for x, y in shape_pts],
        "cum_km": [round(c, 2) for c in shape_cum],
    }


def path_summary(path: dict) -> dict:
    """路径级别的汇总字段（v3 / v5 字段差异一并抹平）。"""
    cost = path.get("cost") or {}
    return {
        "distance_km": round(_num(path.get("distance")) / 1000.0, 1),
        "duration_min": round(_num(path.get("duration") or cost.get("duration")) / 60.0, 1),
        "tolls": _num(path.get("tolls") if path.get("tolls") is not None else cost.get("tolls")),
        "toll_distance_km": round(
            _num(path.get("toll_distance") or cost.get("toll_distance")) / 1000.0, 1),
        "traffic_lights": int(_num(path.get("traffic_lights") or cost.get("traffic_lights"))),
        "restriction": _num(path.get("restriction")),
    }
