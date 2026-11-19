"""Measure HTTP continuity around the isolated Compose promotion exercise."""

from collections import Counter
import json
import math
from pathlib import Path
import re
import subprocess
import sys
from threading import Event, Thread
import time
import urllib.error
import urllib.request

URL = "http://127.0.0.1:8082"
USER = "987654321"
COMPOSE = ["docker", "compose", "-p", "the-neural-network"]


def run(*command):
    return subprocess.check_output(command, text=True, timeout=60).strip()


def api_identity():
    container = run(*COMPOSE, "ps", "-q", "api")
    assert container, "API container is missing"

    processes = run(
        "docker", "top", container, "-eo", "pid,ppid,lstart,comm"
    ).splitlines()

    gunicorn_processes = sorted(
        line for line in processes[1:] if line.split()[-1] == "gunicorn"
    )

    assert gunicorn_processes, "No Gunicorn processes found"

    started = run("docker", "inspect", "--format", "{{.State.StartedAt}}", container)

    return {
        "container": container,
        "started_at": started,
        "processes": gunicorn_processes,
    }


def health():
    with urllib.request.urlopen(f"{URL}/health", timeout=2) as response:
        return json.load(response)["model_version"]


def main():
    if len(sys.argv) < 2:
        raise SystemExit("Supply the isolated Compose promotion command")
    before = api_identity()
    original = health()
    stop, sampled = Event(), Event()
    latencies = []
    counts = dict(http_errors=0, connection_failures=0, timeouts=0, invalid_responses=0)

    def sample():
        while not stop.is_set():
            started = time.monotonic()
            try:
                with urllib.request.urlopen(
                    f"{URL}/recommend/{USER}", timeout=0.6
                ) as response:
                    body = response.read().decode()
                    ids = body.split(",")
                    if not (
                        1 <= len(ids) <= 20
                        and all(ids)
                        and len(set(ids)) == len(ids)
                        and "\n" not in body
                    ):
                        counts["invalid_responses"] += 1
            except urllib.error.HTTPError:
                counts["http_errors"] += 1
            except (OSError, urllib.error.URLError) as error:
                key = (
                    "timeouts"
                    if isinstance(getattr(error, "reason", error), TimeoutError)
                    else "connection_failures"
                )
                counts[key] += 1
            finally:
                latencies.append(round((time.monotonic() - started) * 1000, 2))
                sampled.set()
            stop.wait(0.05)

    sampler = Thread(target=sample, daemon=True)
    sampler.start()
    assert sampled.wait(5), "No initial HTTP sample"
    try:
        swap = subprocess.run(sys.argv[1:], check=False, timeout=180)
    finally:
        stop.set()
        sampler.join(3)
        after = api_identity()
        logs = run(*COMPOSE, "logs", "--no-color", "api")
        versions = re.findall(
            rf"recommendation user_id={USER} .*?model_version=(\S+) .*?status=200", logs
        )
        p95 = sorted(latencies)[math.ceil(len(latencies) * 0.95) - 1]
        evidence = dict(
            counts,
            request_count=len(latencies),
            max_latency_ms=max(latencies),
            p95_latency_ms=p95,
            served_model_versions=dict(Counter(versions)),
            before=before,
            after=after,
            original_default=original,
            final_default=health(),
        )
        Path("reports/ci-model-traffic.json").write_text(
            json.dumps(evidence, indent=2) + "\n"
        )
        print(json.dumps(evidence, indent=2))
    swap.check_returncode()
    assert not any(counts.values()), evidence
    assert before == after, (
        "The API container or worker changed during the model switch"
    )
    assert original == evidence["final_default"]
    assert len(versions) == len(latencies), evidence
    assert {original, "daily-2000-01-01"} <= set(versions), evidence


if __name__ == "__main__":
    main()
