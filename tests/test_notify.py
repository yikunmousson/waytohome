import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import notify

START = datetime(2026, 9, 30, 12, tzinfo=notify.TZ)


def config():
    trip = {"route_version": 2, "corridors": [
        {"id": "main", "short": "主线", "recommended": True},
        {"id": "other", "short": "备选", "recommended": False},
    ]}
    return {"trip": trip, "return_trip": copy.deepcopy(trip),
            "analysis": {"max_current_age_minutes": 45},
            "notifications": {"enabled": True, "eta_change_minutes": 30,
                              "alternative_saving_minutes": 30, "failure_rounds": 3,
                              "cooldown_minutes": 60,
                              "active_start": "2026-09-25T00:00:00+08:00",
                              "active_end": "2026-10-08T00:00:00+08:00"}}


def record(offset=0, main=400, other=410, **fields):
    rec = {"ts": (START + timedelta(minutes=offset)).isoformat(),
           "direction": "outbound", "source": "amap", "route_version": 2,
           "corridors": [{"id": cid, "ok": mins is not None, "duration_min": mins,
                          "distance_km": 600, "roads": ["测试高速"]}
                         for cid, mins in (("main", main), ("other", other))]}
    rec.update(fields)
    return rec


def evaluate(records, state=None, offset=None):
    now = (START + timedelta(minutes=offset)) if offset is not None else notify.timestamp(records[-1]["ts"])
    return notify.evaluate(records, config(), "outbound", state or {}, now)


class DecisionTests(unittest.TestCase):
    def test_initial_baseline_quiet_and_small_changes_accumulate(self):
        first, state = evaluate([record()])
        self.assertIsNone(first)
        for mins in (10, 20):
            message, state = evaluate([record(mins, 400 + mins, 500)], state)
            self.assertIsNone(message)
        message, state = evaluate([record(30, 435, 500)], state)
        self.assertIn("增加35分钟", message["body"])
        self.assertEqual(state["anchor"]["duration_min"], 435)

    def test_cooldown_preserves_pending_change_and_repeated_sample_is_quiet(self):
        _, state = evaluate([record()])
        message, state = evaluate([record(30, 435, 500)], state)
        message, state = evaluate([record(45, 470, 500)], state)
        self.assertIsNone(message)
        self.assertEqual(state["anchor"]["duration_min"], 435)
        message, state = evaluate([record(90, 470, 500)], state)
        self.assertIn("增加35分钟", message["body"])
        self.assertIsNone(evaluate([record(90, 470, 500)], state)[0])

    def test_duration_improves(self):
        _, state = evaluate([record(0, 470, 500)])
        message, _ = evaluate([record(15, 400, 500)], state)
        self.assertIn("减少70分钟", message["body"])

    def test_alternative_sent_once_until_condition_clears(self):
        message, state = evaluate([record(0, 450, 400)])
        self.assertIn("备选当前预计6小时40分", message["body"])
        self.assertIsNone(evaluate([record(65, 450, 400)], state)[0])
        message, state = evaluate([record(70, 450, 440)], state)
        self.assertIsNone(message)
        message, _ = evaluate([record(75, 450, 400)], state)
        self.assertIsNotNone(message)

    def test_alternative_requires_valid_recommended_route(self):
        self.assertIsNone(evaluate([record(0, None, 300)])[0])

    def test_simulated_old_route_and_future_samples_are_ignored(self):
        samples = [record(source="fixture"), record(source="simulated"),
                   record(route_version=1), record(10)]
        message, state = evaluate(samples, offset=0)
        self.assertIsNone(message)
        self.assertNotIn("anchor", state)

    def test_stale_data_only_triggers_health_alert_once_and_recovers(self):
        message, state = evaluate([record(0, 450, 300)], offset=46)
        self.assertIn("采集异常", message["title"])
        self.assertNotIn("备选", message["body"])
        self.assertIsNone(evaluate([record()], state, offset=150)[0])
        message, state = evaluate([record(160)], state)
        self.assertIn("采集恢复", message["title"])
        self.assertFalse(state["incident"])

    def test_failures_count_distinct_collection_rounds(self):
        failed = record(0, None, None)
        self.assertIsNone(evaluate([failed] * 4)[0])
        records = [record(i, None, None) for i in (0, 15, 30)]
        message, state = evaluate(records)
        self.assertIn("连续3轮", message["body"])
        self.assertIsNone(evaluate(records + [record(120, None, None)], state)[0])
        message, _ = evaluate(records + [record(135)], state)
        self.assertIn("恢复", message["title"])

    def test_latest_failure_does_not_reuse_old_traffic_data(self):
        _, state = evaluate([record(0, 400, 410)])
        samples = [record(15, 500, 300), record(30, None, None)]
        self.assertIsNone(evaluate(samples, state)[0])

    def test_route_change_and_old_anchor_reset_reference(self):
        _, state = evaluate([record()])
        changed = record(15, 500, 520)
        changed["corridors"][0]["roads"] = ["另一条高速"]
        self.assertIsNone(evaluate([changed], state)[0])
        changed = record(15, 500, 520)
        changed["corridors"][0]["distance_km"] = 680
        self.assertIsNone(evaluate([changed], state)[0])
        self.assertIsNone(evaluate([record(1500, 500, 520)], state)[0])
        new_state = dict(state, route_version=1)
        self.assertIsNone(evaluate([record(15, 500, 520)], new_state)[0])

    def test_large_route_warning_suppresses_alternative(self):
        rec = record(0, 500, 300, route_signature_checked=True, route_signature_similarity=0.2)
        self.assertIsNone(evaluate([rec])[0])

    def test_active_window_has_exclusive_end(self):
        settings = config()["notifications"]
        self.assertFalse(notify.active(settings, START.replace(day=24)))
        self.assertTrue(notify.active(settings, START.replace(day=25, hour=0)))
        self.assertFalse(notify.active(settings, datetime(2026, 10, 8, tzinfo=notify.TZ)))
        self.assertFalse(notify.active(dict(settings, enabled=False), START))


class DeliveryTests(unittest.TestCase):
    def test_post_keeps_key_out_of_url_and_supports_copied_test_path(self):
        opener = MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.status = 200
        response.read.return_value = b'{"code":200}'
        with patch.object(notify.request, "build_opener", return_value=opener) as factory:
            notify.send_bark("https://api.day.app/secretDeviceKey/test/body",
                             {"title": "标题", "body": "正文"}, "https://example.com/route/")
        req = opener.open.call_args.args[0]
        self.assertEqual(req.full_url, "https://api.day.app/push")
        payload = json.loads(req.data)
        self.assertEqual(payload["device_key"], "secretDeviceKey")
        self.assertEqual(payload["url"], "https://example.com/route/")
        self.assertEqual(payload["level"], "active")
        self.assertIsInstance(factory.call_args.args[0], notify.NoRedirect)

    def test_api_rejection_is_failure_even_with_http_200(self):
        opener = MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.status = 200
        response.read.return_value = b'{"code":400,"message":"secretDeviceKey"}'
        with patch.object(notify.request, "build_opener", return_value=opener):
            with self.assertRaises(notify.NotificationError) as raised:
                notify.send_bark("https://api.day.app/secretDeviceKey", {"body": "test"})
        self.assertNotIn("secretDeviceKey", str(raised.exception))

    def test_transport_errors_and_bad_urls_never_print_secrets(self):
        opener = MagicMock()
        opener.open.side_effect = URLError("secretDeviceKey")
        with patch.object(notify.request, "build_opener", return_value=opener):
            with self.assertRaises(notify.NotificationError) as raised:
                notify.send_bark("https://api.day.app/secretDeviceKey", {"body": "test"})
        self.assertNotIn("secretDeviceKey", str(raised.exception))
        for url in ("http://api.day.app/key", "https://api.day.app/", "https://api.day.app/key?secret=1"):
            with self.assertRaises(notify.NotificationError):
                notify.send_bark(url, {"body": "test"})

    def test_failed_direction_does_not_erase_other_direction_state(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            samples = [record(0, 450, 400), record(0, 450, 400, direction="return")]
            calls = []

            def sender(key, message, dashboard):
                calls.append(message)
                if message["title"].startswith("返程"):
                    raise notify.NotificationError("测试失败")

            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = notify.run(config(), samples, path, "key", "", START, sender=sender)
            self.assertEqual(result, 1)
            state = notify.read_state(path)
            self.assertIn("last_sent_at", state["directions"]["outbound"])
            self.assertNotIn("return", state["directions"])
            calls.clear()
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                notify.run(config(), samples, path, "key", "", START, sender=sender)
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0]["title"].startswith("返程"))

    def test_dry_run_and_missing_key_never_send_or_write(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            sender = MagicMock()
            for dry_run in (True, False):
                with redirect_stdout(io.StringIO()):
                    notify.run(config(), [record(0, 450, 400)], path, "", "", START,
                               dry_run=dry_run, sender=sender)
                self.assertFalse(path.exists())
                sender.assert_not_called()

    def test_corrupt_state_is_not_silently_reset(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            for data in ("broken", "[]", '{"schema":2,"directions":{}}'):
                path.write_text(data)
                with self.assertRaises(notify.NotificationError):
                    notify.read_state(path)


if __name__ == "__main__":
    unittest.main()
