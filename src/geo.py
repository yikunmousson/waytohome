"""坐标与几何工具（纯标准库，无第三方依赖）。

国内地图三套坐标系：
  WGS84  国际标准，GPS/OSM 用
  GCJ02  火星坐标，高德/腾讯用
  BD09   百度用

高德接口只吃 GCJ02。任何来自 OSM/GPS 的坐标，进高德之前必须先转换，
否则整条线路会整体漂移 300~500 米，分段对比全部失真。
"""

import math

_A = 6378245.0                 # 克拉索夫斯基椭球长半轴
_EE = 0.00669342162296594323   # 偏心率平方


def _out_of_china(lng: float, lat: float) -> bool:
    return not (73.66 < lng < 135.05 and 3.86 < lat < 53.55)


def _transform_lat(lng: float, lat: float) -> float:
    ret = -100.0 + 2.0 * lng + 3.0 * lat + 0.2 * lat * lat + 0.1 * lng * lat
    ret += 0.2 * math.sqrt(abs(lng))
    ret += (20.0 * math.sin(6.0 * lng * math.pi) + 20.0 * math.sin(2.0 * lng * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(lat * math.pi) + 40.0 * math.sin(lat / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(lat / 12.0 * math.pi) + 320 * math.sin(lat * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lng(lng: float, lat: float) -> float:
    ret = 300.0 + lng + 2.0 * lat + 0.1 * lng * lng + 0.1 * lng * lat
    ret += 0.1 * math.sqrt(abs(lng))
    ret += (20.0 * math.sin(6.0 * lng * math.pi) + 20.0 * math.sin(2.0 * lng * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(lng * math.pi) + 40.0 * math.sin(lng / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(lng / 12.0 * math.pi) + 300.0 * math.sin(lng / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lng: float, lat: float) -> tuple:
    """GPS/OSM 坐标 -> 高德坐标。"""
    if _out_of_china(lng, lat):
        return lng, lat
    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    rad_lat = lat / 180.0 * math.pi
    magic = math.sin(rad_lat)
    magic = 1 - _EE * magic * magic
    sqrt_magic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrt_magic) * math.pi)
    dlng = (dlng * 180.0) / (_A / sqrt_magic * math.cos(rad_lat) * math.pi)
    return round(lng + dlng, 6), round(lat + dlat, 6)


def gcj02_to_wgs84(lng: float, lat: float) -> tuple:
    """高德坐标 -> GPS/OSM 坐标（迭代逼近，精度约 1e-6 度 ~ 0.1 米）。"""
    if _out_of_china(lng, lat):
        return lng, lat
    wlng, wlat = lng, lat
    for _ in range(5):
        glng, glat = wgs84_to_gcj02(wlng, wlat)
        wlng += lng - glng
        wlat += lat - glat
    return round(wlng, 6), round(wlat, 6)


def haversine_m(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    """两点球面距离，单位米。"""
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def parse_polyline(s: str) -> list:
    """高德 polyline 字段 "lng,lat;lng,lat;..." -> [[lng, lat], ...]。"""
    pts = []
    if not s:
        return pts
    for chunk in s.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(",")
        if len(parts) < 2:
            continue
        try:
            pts.append([float(parts[0]), float(parts[1])])
        except ValueError:
            continue
    return pts


def cumulative_km(points: list) -> list:
    """返回每个点距起点的累计公里数，长度与 points 相同。"""
    out = [0.0]
    for i in range(1, len(points)):
        a, b = points[i - 1], points[i]
        out.append(out[-1] + haversine_m(a[0], a[1], b[0], b[1]) / 1000.0)
    return out


def _seg_distance(p, a, b) -> float:
    """点 p 到线段 ab 的垂直距离（平面近似，单位与输入一致）。"""
    px, py = p
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def simplify(points: list, tolerance_deg: float = 0.0004) -> list:
    """Douglas-Peucker 抽稀，保留首尾点。"""
    if len(points) < 3:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        worst, worst_d = -1, tolerance_deg
        for k in range(i + 1, j):
            d = _seg_distance(points[k], points[i], points[j])
            if d > worst_d:
                worst, worst_d = k, d
        if worst != -1:
            keep[worst] = True
            stack.append((i, worst))
            stack.append((worst, j))
    return [p for p, k in zip(points, keep) if k]


def slice_by_km(points: list, cum: list, n: int) -> list:
    """把线切成 n 段等长区间，返回每段的起终里程与代表点。

    用于看板上的「分段路况色带」——即使不清楚具体路段名称，
    按里程等分也能保证每天比较的是同一段路。
    """
    total = cum[-1] if cum else 0.0
    if total <= 0 or n <= 0:
        return []
    step = total / n
    segments = []
    for i in range(n):
        s, e = i * step, (i + 1) * step
        idx = 0
        for k, c in enumerate(cum):
            if c >= s:
                idx = k
                break
        segments.append({
            "index": i + 1,
            "start_km": round(s, 2),
            "end_km": round(e, 2),
            "mid_km": round((s + e) / 2, 2),
            "point": points[idx],
        })
    return segments
