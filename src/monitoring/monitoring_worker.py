"""Schedule complete monitoring workloads in a single worker process."""

import logging
import signal
import time
from datetime import UTC, datetime
from threading import Event

from src.database.interactions import data_quality_logs
from src.monitoring.schemas import MonitoringConfig
from src.monitoring.workload import run_monitoring_workload

LOGGER = logging.getLogger(__name__)


def _persist_monitoring_failure(error: Exception, duration: float) -> None:
    try:
        data_quality_logs.create(
            source="monitoring",
            severity="ERROR",
            issue_type="monitoring_run_failure",
            error_details=[{"error_type": type(error).__name__}],
            log_metadata={
                "operation": "monitoring_workload",
                "duration_seconds": round(duration, 3),
            },
        )
    except Exception:
        LOGGER.error("monitoring_failure_persistence_failed")


def run_monitoring_worker(config: MonitoringConfig, stop_event: Event) -> None:
    """Run immediately, then reconcile on UTC bucket boundaries."""
    LOGGER.info(
        "monitoring_worker_started bucket_minutes=%d", config.bucket_minutes
    )
    while not stop_event.is_set():
        started_at = time.monotonic()
        try:
            saved_runs = run_monitoring_workload(config)
            duration = time.monotonic() - started_at
            LOGGER.info(
                "monitoring_run_completed bucket_count=%d duration_seconds=%.3f",
                len(saved_runs),
                duration,
            )
        except Exception as error:
            duration = time.monotonic() - started_at
            LOGGER.exception(
                "monitoring_run_failed duration_seconds=%.3f",
                duration,
            )
            _persist_monitoring_failure(error, duration)

        now = datetime.now(UTC)
        bucket_seconds = config.bucket_minutes * 60
        elapsed_seconds = now.minute * 60 + now.second + now.microsecond / 1_000_000
        delay = bucket_seconds - elapsed_seconds % bucket_seconds
        stop_event.wait(delay)
    LOGGER.info("monitoring_worker_stopped")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    stop_event = Event()

    def request_shutdown(signum, _frame):
        LOGGER.info("monitoring_worker_shutdown_requested signal=%d", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    run_monitoring_worker(MonitoringConfig(), stop_event)


if __name__ == "__main__":
    main()
