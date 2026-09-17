"""一次性引导：从 OSRM(OSM) 路线结果生成离线路线骨架。

用途：在高德 Key 还没到位、或采集脚本还没跑起来之前，先让看板有东西可画。
真跑起来之后，看板会用高德每日返回的真实 polyline 覆盖这份骨架。

用法：
    curl -s -A "waytohome/1.0" -o /tmp/osrm_geom.json \
      "https://router.project-osrm.org/route/v1/driving/113.9494373,22.5838445;112.5666171,26.8979280?overview=full&geometries=geojson"
    python3 src/bootstrap_geometry.py /tmp/osrm_geom.json
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geo import cumulative_km, haversine_m, simplify, slice_by_km, wgs84_to_gcj02

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "route-geometry.json"
SEGMENT_COUNT = 20


def main(src_path: str, steps_file: str = "") -> None:
    raw = json.loads(Path(src_path).read_text(encoding="utf-8"))
    route = raw["routes"][0]

    # OSRM 的 GeoJSON 是 [lng, lat]，WGS84
    wgs = [[float(p[0]), float(p[1])] for p in route["geometry"]["coordinates"]]

    # 1) 抽稀（先抽稀再转坐标，省算力；容差按纬度做经度缩放补偿）
    simple_wgs = simplify(wgs, tolerance_deg=0.0004)

    # 2) 转 GCJ02 —— 高德坐标系
    gcj = [list(wgs84_to_gcj02(lng, lat)) for lng, lat in simple_wgs]

    # 3) 累计里程 + 等分切片
    cum = cumulative_km(gcj)
    segments = slice_by_km(gcj, cum, SEGMENT_COUNT)

    # 道路名与里程来自同一走廊的 steps 结果，用于给断面打标注。
    # 只读本地文件，不在脚本里发网络请求，避免半路被中断导致整个引导失败。
    #
    # 关键：把连续同名 step 合并，并把「无名」step 并进相邻路段，
    # 保证 road 区间从 0 连续覆盖到终点 —— 否则分段里程会漏掉一段，
    # 后面按公里切断面就会错位。
    step_names = []
    steps_path = Path(steps_file) if steps_file else None
    if steps_path and steps_path.exists():
        d = json.loads(steps_path.read_text(encoding="utf-8"))
        acc = 0.0
        for st in d["routes"][0]["legs"][0]["steps"]:
            km = st["distance"] / 1000.0
            nm = (st.get("name") or st.get("ref") or "").strip()
            if not step_names:
                step_names.append({"road": nm or "城市道路",
                                   "start_km": 0.0, "end_km": round(acc + km, 1)})
            elif nm and nm != step_names[-1]["road"]:
                step_names.append({"road": nm,
                                   "start_km": round(acc, 1), "end_km": round(acc + km, 1)})
            else:
                # 同名、或无名 -> 延长上一段
                step_names[-1]["end_km"] = round(acc + km, 1)
            acc += km
        # 收尾对其到几何总里程，抹掉最后一点零头
        if step_names:
            step_names[-1]["end_km"] = None   # 由调用方按实际总里程补齐
    else:
        print("[warn] 未提供 steps 文件，断面不做道路名标注")

    # 用几何实际总里程补齐最后一段，保证道路区间铺满全程
    if step_names and step_names[-1]["end_km"] is None:
        step_names[-1]["end_km"] = round(cum[-1], 1)

    def label_for(mid_km: float) -> str:
        for s in step_names:
            if s["start_km"] <= mid_km <= s["end_km"]:
                return s["road"]
        return ""

    for seg in segments:
        seg["road"] = label_for(seg["mid_km"])

    out = {
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "source": "OSRM / OpenStreetMap（WGS84→GCJ02 转换）—— 仅作离线兜底骨架",
        "origin_name": "深圳 · 西丽地铁站",
        "destination_name": "衡阳 · 市人民政府",
        "distance_km": round(sum(
            haversine_m(gcj[i - 1][0], gcj[i - 1][1], gcj[i][0], gcj[i][1]) / 1000.0
            for i in range(1, len(gcj))
        ), 1),
        "segment_count": SEGMENT_COUNT,
        "points": [[round(x, 6), round(y, 6)] for x, y in gcj],
        "cum_km": [round(c, 3) for c in cum],
        "segments": segments,
        "roads": step_names,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"✓ 写出 {OUT}")
    print(f"  抽稀后点数 {len(gcj)}（原始 {len(wgs)}）")
    print(f"  OSRM 里程 {out['distance_km']} km，断面 {SEGMENT_COUNT} 段")
    for s in segments:
        print(f"    #{s['index']:>2}  {s['start_km']:>6.1f}–{s['end_km']:>6.1f} km  {s['road']}")


if __name__ == "__main__":
    main(
        sys.argv[1] if len(sys.argv) > 1 else "/tmp/osrm_geom.json",
        sys.argv[2] if len(sys.argv) > 2 else "/tmp/osrm_steps.json",
    )
