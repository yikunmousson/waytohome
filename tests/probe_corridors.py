"""探测脚本：为 4 条候选走廊找出「钉在路上」的途经点。

为什么要这么做：
    高德的途经点参数只吃坐标。如果把一个城市的市中心当途经点，
    它会带你进城再出城，里程和耗时全废。必须用**服务区/停车区**这类
    就落在高速公路正线上的 POI，才能真正把路线钉在某条走廊上。

用法：
    python3 tests/probe_corridors.py poi      # 只搜服务区，看看有什么
    python3 tests/probe_corridors.py route    # 用候选途经点试算，比较 4 条走廊
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from amap import AmapClient                     # noqa: E402
from collect import load_env_file               # noqa: E402
from segment import path_summary                # noqa: E402

ORIGIN = "113.954325,22.580845"
DEST = "112.571859,26.894350"

# 每条走廊上可能存在的服务区关键词（用「高速名+服务区」去搜）
POI_QUERIES = [
    ("G4 京港澳 · 韶关段", "京港澳高速服务区", "韶关市"),
    ("G4 京港澳 · 韶关段2", "曲江服务区", "韶关市"),
    ("G4 京港澳 · 云岩", "云岩服务区", "韶关市"),
    ("G0421 许广 · 清远段", "许广高速服务区", "清远市"),
    ("G0421 许广 · 阳山", "阳山服务区", "清远市"),
    ("G0421 许广 · 连州", "连州服务区", "清远市"),
    ("G0423 广连 · 清远段", "广连高速服务区", "清远市"),
    ("G0423 广连 · 从化", "从化服务区", "广州市"),
    ("G0422 武深 · 河源段", "武深高速服务区", "河源市"),
    ("G0422 武深 · 连平", "连平服务区", "河源市"),
    ("G55 二广 · 肇庆段", "二广高速服务区", "肇庆市"),
    ("G55 二广 · 怀集", "怀集服务区", "肇庆市"),
    ("G55 二广 · 永州段", "二广高速服务区", "永州市"),
]


def do_poi(client):
    for label, kw, city in POI_QUERIES:
        try:
            data = client.place_text(kw, city=city, citylimit=True)
        except Exception as exc:
            print("%-26s ✗ %s" % (label, exc))
            continue
        pois = data.get("pois") or []
        print("=== %-24s [%s]" % (label, kw))
        if not pois:
            print("     （无结果）")
        for p in pois[:4]:
            print("     %-28s %s   %s" % (p.get("name", "")[:28], p.get("location", ""),
                                          (p.get("type") or "")[-30:]))
        print()
    print("高德调用次数：%d" % client.call_count)


# 候选走廊：每条用**真实服务区坐标**把它钉在高速正线上，再让高德算。
# 坐标来自 `probe_corridors.py poi` 的实测结果（格式 "经度,纬度"）。
CORRIDOR_TRIES = [
    ("东线 · 京港澳 G4（曲江+云岩服务区钉住）",
     ["113.597754,24.638799", "113.109652,25.085049"]),
    ("西线 · 许广 G0421（阳山南+连州服务区钉住）",
     ["112.698975,24.233710", "112.532536,24.976269"]),
    ("西线 · 广连 G0423（龙山服务区+黎溪北停车区钉住）",
     ["113.335848,23.787596", "113.272252,23.956502"]),
    ("中线 · 武深 G0422（隆街停车区+连平服务区钉住）",
     ["114.323203,24.281724", "114.565601,24.446082"]),
    ("远西 · 二广 G55（怀集服务区钉住）",
     ["112.230956,23.978423"]),
]


# 单点隔离探测：一次只钉一个服务区，看清它把路线带到哪、里程多少。
# 多钉子容易互相带偏（实测出现过 758km 的大绕行），所以先隔离变量。
SINGLE_POINTS = [
    ("曲江服务区（G4 韶关曲江）", "113.597754,24.638799"),
    ("云岩服务区（G4 韶关乳源）", "113.109652,25.085049"),
    ("阳山南服务区（G0421 清远阳山）", "112.698975,24.233710"),
    ("连州服务区（G0421 清远连州）", "112.532536,24.976269"),
    ("龙山服务区（广连 清远英德）", "113.335848,23.787596"),
    ("黎溪北停车区（广连 清远英德）", "113.272252,23.956502"),
    ("怀集服务区（G55 肇庆怀集）", "112.230956,23.978423"),
    ("连平服务区（G0422 河源连平）", "114.565601,24.446082"),
]


def do_single(client):
    print("=== 各策略下的高德官方候选（不钉途经点）===")
    for stg in (0, 5):
        try:
            data = client.driving_v3(ORIGIN, DEST, strategy=stg)
        except Exception as exc:
            print("  策略%s ✗ %s" % (stg, exc))
            continue
        paths = ((data.get("route") or {}).get("paths")) or []
        print("  策略%s 返回 %d 条：" % (stg, len(paths)))
        for i, path in enumerate(paths):
            s = path_summary(path)
            roads = road_names(path)
            print("    [%d] %.1f km  %.2f h  ¥%.0f  %s" % (
                i, s["distance_km"], s["duration_min"] / 60, s["tolls"],
                " → ".join(roads[6:14]) or "—"))
    print()
    print("=== 单服务区隔离探测 ===")
    for label, wp in SINGLE_POINTS:
        try:
            data = client.driving_v3(ORIGIN, DEST, strategy=0, waypoints=[wp])
        except Exception as exc:
            print("%-34s ✗ %s" % (label, exc))
            continue
        paths = ((data.get("route") or {}).get("paths")) or []
        if not paths:
            print("%-34s （无路线）" % label)
            continue
        s = path_summary(paths[0])
        roads = road_names(paths[0])
        print("%-34s %6.1f km %5.2f h ¥%-4.0f  %s" % (
            label, s["distance_km"], s["duration_min"] / 60, s["tolls"],
            " → ".join(roads[8:16]) or "—"))
    print()
    print("高德调用次数：%d" % client.call_count)


def road_names(path):
    roads = []
    for st in path.get("steps") or []:
        r = (st.get("road") or "").strip()
        if r and (not roads or roads[-1] != r):
            roads.append(r)
    return roads


FINAL_WPS = [
    ("广连 G0423", ["113.272252,23.956502"]),                 # 黎溪北停车区
    ("许广 G0421", ["112.698975,24.233710"]),                 # 阳山南服务区
    ("京港澳 G4", ["113.597754,24.638799"]),                  # 曲江服务区
    ("二广 G55", ["112.230956,23.978423"]),                   # 怀集服务区
]

RETURN_ORIGIN = DEST
RETURN_DEST = ORIGIN


def do_final(client):
    print("### 去程默认路线（完整走向）")
    dump_full(client, ORIGIN, DEST, None)
    print("\n### 返程默认路线（完整走向）")
    dump_full(client, RETURN_ORIGIN, RETURN_DEST, None)

    print("\n### 去程 · 4 条候选走廊")
    for label, wps in FINAL_WPS:
        show_line(client, label, ORIGIN, DEST, wps)
    print("\n### 返程 · 4 条候选走廊")
    for label, wps in FINAL_WPS:
        show_line(client, label, RETURN_ORIGIN, RETURN_DEST, wps)
    print("\n高德调用次数：%d" % client.call_count)


def dump_full(client, origin, dest, wps):
    try:
        data = client.driving_v3(origin, dest, strategy=0, waypoints=wps)
    except Exception as exc:
        print("   ✗ %s" % exc)
        return
    paths = ((data.get("route") or {}).get("paths")) or []
    if not paths:
        print("   （无路线）")
        return
    p = paths[0]
    s = path_summary(p)
    print("   %.1f km  %.2f h  ¥%.0f" % (s["distance_km"], s["duration_min"] / 60, s["tolls"]))
    print("   " + " → ".join(road_names(p)))


def show_line(client, label, origin, dest, wps):
    try:
        data = client.driving_v3(origin, dest, strategy=0, waypoints=wps)
    except Exception as exc:
        print("   %-12s ✗ %s" % (label, exc))
        return
    paths = ((data.get("route") or {}).get("paths")) or []
    if not paths:
        print("   %-12s （无路线）" % label)
        return
    s = path_summary(paths[0])
    print("   %-12s %6.1f km  %5.2f h  ¥%-4.0f   %s" % (
        label, s["distance_km"], s["duration_min"] / 60, s["tolls"],
        " → ".join(road_names(paths[0])[5:13]) or "—"))


def do_route(client):
    print("=== 默认（不设途经点）===")
    show(client, [])
    print()
    for label, wps in CORRIDOR_TRIES:
        print("=== %s ===" % label)
        show(client, wps)
        print()
    print("高德调用次数：%d" % client.call_count)


def show(client, wps):
    try:
        data = client.driving_v3(ORIGIN, DEST, strategy=0, waypoints=wps or None)
    except Exception as exc:
        print("     ✗ %s" % exc)
        return
    for path in ((data.get("route") or {}).get("paths")) or []:
        s = path_summary(path)
        roads = []
        for st in path.get("steps") or []:
            r = (st.get("road") or "").strip()
            if r and (not roads or roads[-1] != r):
                roads.append(r)
        print("     %.1f km  %.2f h  ¥%.0f" % (s["distance_km"], s["duration_min"] / 60, s["tolls"]))
        print("     " + " → ".join(roads[:22]) + (" …" if len(roads) > 22 else ""))


def main():
    load_env_file()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "poi"
    client = AmapClient(os.environ.get("AMAP_KEY", "").strip(), timeout=20, retries=2)
    if cmd == "poi":
        do_poi(client)
    elif cmd == "single":
        do_single(client)
    elif cmd == "final":
        do_final(client)
    else:
        do_route(client)


if __name__ == "__main__":
    main()
