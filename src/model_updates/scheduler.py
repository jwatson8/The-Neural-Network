"""At/after 02:00 America/New_York, process yesterday once (including restart catch-up)."""
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from src.model_updates.daily import run


def due_day(now):
    local = now.astimezone(ZoneInfo('America/New_York'))
    return local.date() - timedelta(days=1) if local.hour >= 2 else None


@contextmanager
def lock(artifacts):
    import fcntl  # Linux container only; pure evaluation tests remain cross-platform.
    with (Path(artifacts) / '.daily.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def main():
    artifacts = Path('/artifacts')
    while True:
        day = due_day(datetime.now(ZoneInfo('America/New_York')))
        enabled = os.getenv('DAILY_MODEL_UPDATES_ENABLED', 'true').lower() == 'true'
        if enabled and day and not (artifacts / 'daily' / day.isoformat() / 'report.json').exists():
            try:
                with lock(artifacts):
                    report = run(day, artifacts, Path('/app/data'))
                    print(json.dumps({'day': str(day), 'status': report['status']}), flush=True)
            except BlockingIOError:
                pass  # Retry next minute after deployment/promotion releases lock.
            except Exception as error:
                print(json.dumps({'day': str(day), 'status': 'failed', 'error_type': type(error).__name__}), flush=True)
        time.sleep(60)


if __name__ == '__main__':
    main()
