import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import watch_collect


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 30, tzinfo=timezone.utc)

    def data(self, age):
        return {d: {'corridors': [{'last_sample': {
            'sampled_at': (self.now - timedelta(minutes=age)).isoformat()}}]}
                for d in ('outbound', 'return')}

    def test_fresh_data_needs_no_dispatch(self):
        self.assertEqual(watch_collect.decide(self.data(10), [], self.now)['status'], 'fresh')

    def test_stale_or_missing_direction_needs_dispatch(self):
        self.assertEqual(watch_collect.decide(self.data(16), [], self.now)['status'], 'due')
        data = self.data(1)
        del data['return']
        self.assertEqual(watch_collect.decide(data, [], self.now)['status'], 'due')

    def test_existing_run_and_cooldown_prevent_duplicates(self):
        run = {'status': 'queued', 'databaseId': 123, 'createdAt': self.now.isoformat()}
        self.assertEqual(watch_collect.decide(self.data(90), [run], self.now)['status'], 'busy')
        run['status'] = 'completed'
        self.assertEqual(watch_collect.decide(self.data(90), [run], self.now)['status'], 'cooldown')

    def test_cutoff_blocks_even_network_reads(self):
        with patch.object(watch_collect, 'project_active', return_value=False), patch.object(watch_collect, 'gh') as gh:
            self.assertEqual(watch_collect.check(True)['status'], 'ended')
            gh.assert_not_called()
