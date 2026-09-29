"""Loopback dashboard with authenticated, read-only GitHub data refresh."""
import argparse
import json
import subprocess
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent


def sample_time(data):
    return max((data.get(d, {}).get('updated_at') or '' for d in ('outbound', 'return')))


class DataSource:
    def __init__(self, repo, root=ROOT):
        self.repo, self.root = repo, root
        self.lock = threading.Lock()
        self.checked = None
        self.warning = ''

    def read(self):
        with self.lock:
            cache = self.root / '.preview-cache' / 'data.json'
            if self.checked is None or time.monotonic() - self.checked >= 60:
                try:
                    result = subprocess.run(
                        ['gh', 'api', f'repos/{self.repo}/contents/web/data.json?ref=main',
                         '-H', 'Accept: application/vnd.github.raw+json'],
                        capture_output=True, check=True, timeout=15)
                    data = json.loads(result.stdout)
                    if not sample_time(data):
                        raise ValueError('Missing sample timestamp')
                    cache.parent.mkdir(exist_ok=True)
                    temporary = cache.with_suffix('.tmp')
                    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
                    temporary.replace(cache)
                    self.warning = ''
                except (OSError, subprocess.SubprocessError, ValueError):
                    self.warning = '云端同步暂未成功，显示已保存数据，请留意采样时间。'
                self.checked = time.monotonic()
            candidates = []
            for path in (self.root / 'web' / 'data.json', cache):
                try:
                    data = json.loads(path.read_text(encoding='utf-8'))
                    if sample_time(data):
                        candidates.append(data)
                except (OSError, ValueError):
                    pass
            if not candidates:
                raise ValueError('No saved data')
            data = max(candidates, key=sample_time)
            data['sync_status'] = {'warning': self.warning}
            return json.dumps(data, ensure_ascii=False).encode('utf-8')


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if urlsplit(self.path).path == '/data.json':
            try:
                body = self.server.source.read()
            except ValueError:
                self.send_error(503, 'No saved data')
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='本地看板，刷新时同步私有仓库的累计数据')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--repo', default='yikunmousson/waytohome')
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port),
                                partial(Handler, directory=str(ROOT / 'web')))
    server.source = DataSource(args.repo)
    print(f'看板 http://127.0.0.1:{args.port}/', flush=True)
    server.serve_forever()
