"""Hourly trigger, persisted 36-hour cadence; uses the trusted deployed checkout."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
from zoneinfo import ZoneInfo


def parse_time(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Schedule timestamps must include a timezone')
    return result.astimezone(timezone.utc)


def due(now, state, start_at, hours):
    if not 36 <= hours <= 48:
        raise ValueError('MODEL_UPDATE_INTERVAL_HOURS must be between 36 and 48')
    earliest = parse_time(start_at) if start_at else now
    if state:
        earliest = max(earliest, parse_time(state['started_at']) + timedelta(hours=hours))
    return now >= earliest


def main():
    import fcntl
    root = Path(__file__).resolve().parents[1]
    state_dir = root / 'state'
    state_dir.mkdir(exist_ok=True)
    with (state_dir / '.scheduled-update.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state_path = state_dir / 'scheduled-update.json'
        state = json.loads(state_path.read_text()) if state_path.exists() else None
        now = datetime.now(timezone.utc)
        hours = int(os.getenv('MODEL_UPDATE_INTERVAL_HOURS', '36'))
        if not due(now, state, os.getenv('MODEL_UPDATE_START_AT', ''), hours):
            print(json.dumps({'status': 'not_due', 'last_attempt': state}))
            return
        day = (now.astimezone(ZoneInfo('America/New_York')).date() - timedelta(days=1)).isoformat()
        record = dict(started_at=now.isoformat(), day=day, status='running', interval_hours=hours)
        temporary = state_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(record, indent=2) + '\n')
        temporary.replace(state_path)
        result = subprocess.run(['python3', 'deployment/observe_update.py', day], cwd=root)
        outcome = root / 'artifacts' / 'daily' / day / 'promotion.json'
        record.update(finished_at=datetime.now(timezone.utc).isoformat(),
                      status='completed' if result.returncode == 0 else 'failed',
                      exit_code=result.returncode,
                      promotion=json.loads(outcome.read_text())['status'] if outcome.exists() else 'not_promoted')
        temporary.write_text(json.dumps(record, indent=2) + '\n')
        temporary.replace(state_path)
        print(json.dumps(record))
        raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()
