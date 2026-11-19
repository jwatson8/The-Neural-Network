"""Record HTTP continuity and actual model identities around a production update."""
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread
import time
from urllib.request import urlopen


def health():
    with urlopen('http://127.0.0.1:8082/health', timeout=2) as response:
        return json.load(response)


def identity():
    container = subprocess.check_output(
        ['docker', 'compose', '-p', 'the-neural-network', 'ps', '-q', 'api'], text=True).strip()
    return subprocess.check_output(
        ['docker', 'inspect', '--format', '{{.Id}} {{.State.StartedAt}}', container], text=True).strip()


def observe(day):
    datetime.strptime(day, '%Y-%m-%d')
    before, container_before = health(), identity()
    started = datetime.now(timezone.utc).isoformat()
    stop, sampled = Event(), Event()
    latencies, versions, errors = [], Counter(), Counter()

    def probe():
        while not stop.is_set():
            timer = time.monotonic()
            try:
                with urlopen('http://127.0.0.1:8082/recommend/1', timeout=.6) as response:
                    body = response.read().decode()
                    ids = body.split(',')
                    if not (response.status == 200 and 1 <= len(ids) <= 20
                            and all(ids) and len(set(ids)) == len(ids) and '\n' not in body):
                        raise ValueError('Malformed recommendation response')
                    versions[response.headers.get('X-Model-Version-ID', 'unreported')] += 1
            except Exception as error:
                errors[type(error).__name__] += 1
            finally:
                latencies.append((time.monotonic() - timer) * 1000)
                sampled.set()
            stop.wait(1)

    thread = Thread(target=probe, daemon=True)
    thread.start()
    sampled.wait(5)
    result = None
    after = None
    try:
        result = subprocess.run(['bash', 'deployment/daily_model.sh', 'auto', day], timeout=7000)
        time.sleep(2)  # Capture requests after acknowledgement, as well as during loading.
        after = health()
    finally:
        stop.set()
        thread.join(3)
        folder = Path('artifacts/daily') / day
        folder.mkdir(parents=True, exist_ok=True)
        evidence = dict(started_at=started, finished_at=datetime.now(timezone.utc).isoformat(),
                        before=before, after=after, container_before=container_before,
                        container_after=identity(), request_count=len(latencies),
                        failed_requests=sum(errors.values()), errors=dict(errors),
                        p95_ms=sorted(latencies)[math.ceil(len(latencies)*.95)-1] if latencies else None,
                        max_ms=max(latencies) if latencies else None, served_versions=dict(versions),
                        update_exit_code=result.returncode if result else None,
                        scope='Local one-request-per-second probe, not course-wide availability')
        # Preserve prior attempt evidence rather than replacing it on retries.
        target = folder / ('continuity-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.json')
        target.write_text(json.dumps(evidence, indent=2) + '\n')
        print(json.dumps({'continuity_file': str(target), **evidence}))
    return result.returncode if result else 1


if __name__ == '__main__':
    raise SystemExit(observe(sys.argv[1]))
