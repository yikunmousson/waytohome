import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from serve import DataSource


class ServeTests(unittest.TestCase):
    def test_refresh_cache_and_offline_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'web').mkdir()
            local = {'outbound': {'updated_at': '2026-09-29T12:00:00+08:00'}}
            remote = {'outbound': {'updated_at': '2026-09-30T12:00:00+08:00'}}
            (root / 'web/data.json').write_text(json.dumps(local))
            source = DataSource('owner/repo', root)
            with patch('serve.subprocess.run', return_value=SimpleNamespace(stdout=json.dumps(remote))) as run:
                self.assertEqual(json.loads(source.read())['outbound'], remote['outbound'])
                source.read()
                self.assertEqual(run.call_count, 1)
            with patch('serve.subprocess.run', side_effect=OSError('offline')):
                restored = json.loads(DataSource('owner/repo', root).read())
                self.assertEqual(restored['outbound'], remote['outbound'])
                self.assertTrue(restored['sync_status']['warning'])
            self.assertEqual(json.loads((root / 'web/data.json').read_text()), local)
