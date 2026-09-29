"""One shared cutoff for local collection and GitHub Actions."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def project_active(now=None):
    end = datetime.fromisoformat(json.loads((ROOT / 'project-lifecycle.json').read_text())['end_at'])
    return (now or datetime.now(timezone.utc)) < end

if __name__ == '__main__':
    active = project_active()
    print('项目运行中' if active else '项目已到期，停止采样并停用工作流')
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
            f.write('active=' + str(active).lower() + '\n')
