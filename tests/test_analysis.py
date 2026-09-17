import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.modules.setdefault("requests", types.ModuleType("requests"))

import analyze  # noqa: E402


TZ = timezone(timedelta(hours=8))


def config():
    trip = {
        "name": "测试",
        "origin_name": "甲",
        "destination_name": "乙",
        "route_version": 2,
        "corridors": [{
            "id": "main", "name": "主线", "short": "主线",
            "recommended": True, "waypoints": [],
        }],
    }
    return {
        "trip": trip,
        "return_trip": dict(trip),
        "collect": {"strategy": 0},
        "holiday": {
            "holiday_days": ["2026-10-01"],
            "makeup_workdays": [],
            "outbound_candidates": ["2026-09-30"],
            "return_candidates": ["2026-09-30", "2026-10-01"],
            "return_arrival_candidates": ["2026-10-01"],
            "free_start": "2026-10-01T00:00:00+08:00",
            "free_end": "2026-10-08T00:00:00+08:00",
        },
        "analysis": {
            "segment_count": 1,
            "baseline_window_days": 14,
            "max_current_age_minutes": 45,
            "best_windows_per_corridor": 6,
            "include_simulated": False,
        },
    }


def record(ts, ok=True, version=2, duration=120.0):
    item = {
        "id": "main", "ok": ok, "recommended": True,
        "route_version": version,
    }
    if ok:
        item.update({
            "duration_min": duration,
            "distance_km": 200.0,
            "tolls": 100.0,
            "bins": [{
                "index": 1, "distance_km": 200.0, "duration_min": duration,
                "start_km": 0.0, "end_km": 200.0, "road": "测试高速",
            }],
            "roads": ["测试高速"],
        })
    else:
        item["error"] = "mock failure"
    dt = datetime.fromisoformat(ts)
    return {
        "ts": ts, "date": ts[:10], "direction": "return",
        "source": "amap", "route_version": version,
        "day_type": "holiday" if dt.date().isoformat() == "2026-10-01" else "workday",
        "corridors": [item],
    }


class AnalysisTests(unittest.TestCase):
    def test_old_route_version_is_excluded(self):
        self.assertFalse(analyze.usable(record("2026-09-30T12:00:00+08:00", version=1), config()))
        self.assertTrue(analyze.usable(record("2026-09-30T12:00:00+08:00", version=2), config()))

    def test_basis_is_not_called_measured_while_prior_dominates(self):
        baseline = {
            "detail": {"workday": {8: {"n": 3, "obs": 2.0, "raw_prior": 1.0}}},
            "prior_k": 6, "avg_amp": 1.0,
        }
        self.assertEqual(analyze.factor_at(baseline, "workday", 8)[1], "实测+经验")
        baseline["detail"]["workday"][8]["n"] = 18
        self.assertEqual(analyze.factor_at(baseline, "workday", 8)[1], "实测为主")

    def test_return_windows_are_filtered_by_arrival_date(self):
        cfg = config()
        recs = [record("2026-09-30T12:00:00+08:00")]
        baseline = analyze.build_baseline(recs, cfg, "main")
        projected = analyze.project_corridor(
            recs, baseline, cfg, "return", "main",
            datetime(2026, 9, 30, 12, tzinfo=TZ))
        self.assertIsNone(projected["cells"][0]["m"][0])
        self.assertIsNotNone(projected["cells"][0]["m"][23])
        self.assertTrue(all(w["arrive"][:10] == "2026-10-01" for w in projected["best"]))

    def test_stale_success_does_not_enter_current_ranking(self):
        cfg = config()
        sampled = "2026-09-30T12:00:00+08:00"
        result = analyze.analyze_direction(
            [record(sampled)], cfg, "return",
            datetime(2026, 9, 30, 13, tzinfo=TZ))
        self.assertEqual(result.get("now_table", []), [])
        self.assertFalse(result["corridors"][0]["now"]["ok"])
        self.assertTrue(result["corridors"][0]["now"]["stale"])

    def test_latest_failure_replaces_older_success(self):
        cfg = config()
        recs = [
            record("2026-09-30T12:00:00+08:00"),
            record("2026-09-30T12:15:00+08:00", ok=False),
        ]
        result = analyze.analyze_direction(
            recs, cfg, "return",
            datetime(2026, 9, 30, 12, 20, tzinfo=TZ))
        now = result["corridors"][0]["now"]
        self.assertFalse(now["ok"])
        self.assertEqual(now["sampled_at"], "2026-09-30T12:15:00+08:00")
        self.assertEqual(now["error"], "mock failure")


if __name__ == "__main__":
    unittest.main()
