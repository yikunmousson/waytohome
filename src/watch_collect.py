"""Check cloud freshness and dispatch one collection if the scheduler missed it."""
import argparse
import json
import subprocess
from datetime import datetime, timezone
from lifecycle import project_active

REPO = 'yikunmousson/waytohome'


def gh(*args):
    return subprocess.run(['gh', *args], check=True, capture_output=True,
                          text=True, timeout=30).stdout


def age_minutes(ts, now):
    if not ts:
        return None
    return (now - datetime.fromisoformat(ts.replace('Z', '+00:00'))).total_seconds() / 60


def decide(data, runs, now):
    ages = {}
    for direction in ('outbound', 'return'):
        times = []
        for corridor in data.get(direction, {}).get('corridors', []):
            latest = corridor.get('last_sample') or {}
            current = corridor.get('now') or {}
            ts = latest.get('sampled_at') or (current.get('sampled_at') if current.get('ok') else None)
            if ts:
                times.append(ts)
        ages[direction] = age_minutes(max(times) if times else None, now)
    result = {'age_minutes': ages}
    if all(age is not None and -5 <= age < 15 for age in ages.values()):
        return {**result, 'status': 'fresh'}
    active = [run for run in runs if run['status'] != 'completed']
    if active:
        return {**result, 'status': 'busy', 'run_id': active[0]['databaseId']}
    if runs and age_minutes(runs[0]['createdAt'], now) < 10:
        return {**result, 'status': 'cooldown'}
    return {**result, 'status': 'due'}


def check(dispatch=False):
    now = datetime.now(timezone.utc)
    if not project_active(now):
        return {'status': 'ended'}
    data = json.loads(gh('api', f'repos/{REPO}/contents/web/data.json?ref=main',
                         '-H', 'Accept: application/vnd.github.raw+json'))
    runs = json.loads(gh('run', 'list', '--repo', REPO, '--workflow', 'collect.yml',
                         '--limit', '20', '--json', 'databaseId,status,createdAt,conclusion'))
    result = decide(data, runs, now)
    # Recheck the cutoff immediately before mutating remote state.
    if result['status'] == 'due' and dispatch and project_active():
        output = gh('workflow', 'run', 'collect.yml', '--repo', REPO, '--ref', 'main')
        result.update(status='dispatched', output=output.strip())
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dispatch', action='store_true', help='必要时触发一轮云端采样')
    args = parser.parse_args()
    try:
        print(json.dumps(check(args.dispatch), ensure_ascii=False))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        # Never print command output or credentials on failure.
        print(json.dumps({'status': 'error', 'error': type(exc).__name__}))
        raise SystemExit(1)
