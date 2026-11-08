"""Schedule complete monitoring workloads in a single worker process."""

import logging
import signal
import time
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
    """Run monitoring workloads at a fixed start-to-start interval."""
    LOGGER.info(
        "monitoring_worker_started interval_seconds=%d", config.interval_seconds
    )
    while not stop_event.is_set():
        started_at = time.monotonic()
        try:
            saved_run = run_monitoring_workload(config)
            duration = time.monotonic() - started_at
            LOGGER.info(
                "monitoring_run_completed run_id=%s duration_seconds=%.3f",
                saved_run.run_id,
                duration,
            )
        except Exception as error:
            duration = time.monotonic() - started_at
            LOGGER.exception(
                "monitoring_run_failed duration_seconds=%.3f",
                duration,
            )
            _persist_monitoring_failure(error, duration)

        delay = max(0, config.interval_seconds - duration)
        if delay == 0 and duration >= config.interval_seconds:
            LOGGER.warning(
                "monitoring_run_exceeded_interval duration_seconds=%.3f",
                duration,
            )
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
