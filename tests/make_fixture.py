"""生成一份仿真的高德 v3 驾车路径规划响应，用于离线验证整条流水线。

几何是真实的（来自 data/route-geometry.json），只有「耗时 / 路况」是模拟的。
这样可以先把采集 → 解析 → 分段 → 看板整条链路跑通，等真实 Key 到位再换成真数据。

用法：
    python3 tests/make_fixture.py            # 生成 tests/fixture_amap.json
    python3 tests/make_fixture.py --hour 22  # 模拟晚上 22 点出发
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from geo import haversine_m  # noqa: E402

# 各高速的设计基准速度（km/h）
ROAD_BASE_SPEED = {
    "南光高速": 90, "龙大高速": 90, "莞佛高速": 95, "珠三角环线高速": 100,
    "大广高速": 110, "广连高速": 115, "临连高速": 100, "许广高速": 112,
    "沙河西路": 45, "南三环西延段": 80, "蔡伦大道": 55,
}

# 小时 -> 全局拥堵系数（1.0 = 畅通）。基于往年国庆形态的经验值，仅用于仿真。
HOUR_FACTOR = {
    0: 1.35, 1: 1.22, 2: 1.05, 3: 0.98, 4: 0.98, 5: 1.00, 6: 1.06, 7: 1.22,
    8: 1.40, 9: 1.45, 10: 1.40, 11: 1.28, 12: 1.20, 13: 1.16, 14: 1.16,
    15: 1.18, 16: 1.24, 17: 1.36, 18: 1.42, 19: 1.38, 20: 1.28, 21: 1.20,
    22: 1.16, 23: 1.24,
}

# 珠三角出城段（0–160 km）在早晚高峰额外吃惩罚
URBAN_PENALTY_KM = 160.0


def speed_for(road: str, km: float, hour: int) -> float:
    base = ROAD_BASE_SPEED.get(road, 95)
    f = HOUR_FACTOR[hour]
    if km < URBAN_PENALTY_KM:
        f *= 1.18
    return max(18.0, base / f)


def iter_ranges(points, cum, roads, total_km, max_len=32.0):
    """按顺序把路线切给各路段，产出 (road, start_km, end_km, 点串)。

    注意：必须按点索引顺序推进，而不是「按里程区间挑点」——
    后者会在每段边界丢掉一个点间距，几十段下来里程能少算 5%。
    """
    n = len(points)
    i = 0
    for r in roads:
        a = r["start_km"]
        b = min(r["end_km"], total_km)
        cur = a
        while cur < b - 0.5:
            nxt = min(cur + max_len, b)
            start = i
            j = i
            while j < n and cum[j] <= nxt + 1e-9:
                j += 1
            end = min(n - 1, j)          # 多含一个跨过 nxt 的点，保证与下一段无缝衔接
            seg = points[start:end + 1]
            if len(seg) >= 2:
                yield r["road"], cur, nxt, seg
                i = end                  # 下一段从同一点起步
            cur = nxt


def build_path(geom, hour, time_scale=1.0):
    pts, cum = geom["points"], geom["cum_km"]
    total = cum[-1]

    steps, tmcs = [], []
    total_time_s = 0.0
    total_dist_m = 0.0

    for road, a_km, b_km, seg in iter_ranges(pts, cum, geom["roads"], total):
        dist = sum(haversine_m(seg[i - 1][0], seg[i - 1][1], seg[i][0], seg[i][1])
                   for i in range(1, len(seg)))
        mid_km = (a_km + b_km) / 2
        spd = speed_for(road, mid_km, hour)
        dur = dist / (spd / 3.6) * time_scale
        total_time_s += dur
        total_dist_m += dist

        steps.append({
            "instruction": "沿%s行驶" % road,
            "orientation": "北",
            "road": road,
            "distance": str(int(dist)),
            "duration": str(int(dur)),
            "polyline": ";".join("%.6f,%.6f" % (p[0], p[1]) for p in seg),
            "action": "直行",
            "assistant_action": [],
        })

        # 每个 step 再切成 2 段不同路况，模拟 tmcs 色带
        half = max(1, len(seg) // 2)
        for part, sub in (("a", seg[:half + 1]), ("b", seg[half:])):
            if len(sub) < 2:
                continue
            d = sum(haversine_m(sub[i - 1][0], sub[i - 1][1], sub[i][0], sub[i][1])
                    for i in range(1, len(sub)))
            sp = spd * (0.82 if part == "a" else 1.12)
            if sp >= 75:
                status = "畅通"
            elif sp >= 50:
                status = "缓行"
            elif sp >= 32:
                status = "拥堵"
            else:
                status = "严重拥堵"
            tmcs.append({
                "status": status,
                "distance": str(int(d)),
                "polyline": ";".join("%.6f,%.6f" % (p[0], p[1]) for p in sub),
            })

    return {
        "distance": str(int(total_dist_m)),
        "duration": str(int(total_time_s)),
        "tolls": "288" if time_scale < 1.05 else "305",
        "toll_distance": str(int(total_dist_m * 0.94)),
        "traffic_lights": "14",
        "restriction": "0",
        "steps": steps,
        "tmcs": tmcs,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hour", type=int, default=9, help="模拟几点出发（0-23）")
    ap.add_argument("--out", default=str(ROOT / "tests" / "fixture_amap.json"))
    args = ap.parse_args()

    geom = json.loads((ROOT / "data" / "route-geometry.json").read_text(encoding="utf-8"))

    primary = build_path(geom, args.hour)
    # 备选路线：稍远一点、稍慢一点（模拟高德返回的第二条方案）
    alt = build_path(geom, args.hour, time_scale=1.08)

    payload = {
        "status": "1",
        "info": "OK",
        "infocode": "10000",
        "count": "2",
        "route": {
            "origin": "113.954325,22.580845",
            "destination": "112.571859,26.894350",
            "taxi_cost": "0",
            "paths": [primary, alt],
        },
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print("✓ 生成 %s" % args.out)
    print("  方案1 %s km / %s 分钟   steps=%d tmcs=%d"
          % (int(primary["distance"]) // 1000, int(primary["duration"]) // 60,
             len(primary["steps"]), len(primary["tmcs"])))


if __name__ == "__main__":
    main()
