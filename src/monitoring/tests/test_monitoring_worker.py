"""Tests for the UTC-aligned monitoring worker."""

import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from src.monitoring import monitoring_worker
from src.monitoring.schemas import MonitoringConfig


class StopEvent:
    def __init__(self, stop_after_waits=1, *, stopped=False):
        self.stop_after_waits = stop_after_waits
        self.stopped = stopped
        self.waits = []

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, timeout):
        self.waits.append(timeout)
        if len(self.waits) >= self.stop_after_waits:
            self.stopped = True
        return self.stopped


class MonitoringWorkerTests(unittest.TestCase):
    def test_runs_immediately_and_waits_for_the_next_utc_boundary(self):
        config = MonitoringConfig()
        stop_event = StopEvent()
        with patch.object(
            monitoring_worker.time, "monotonic", side_effect=(100, 103)
        ), patch.object(
            monitoring_worker, "run_monitoring_workload", return_value=[object()]
        ) as run_workload, patch.object(monitoring_worker, "datetime") as clock:
            clock.now.return_value = datetime(2026, 1, 1, 2, 3, 20, tzinfo=UTC)
            monitoring_worker.run_monitoring_worker(config, stop_event)

        run_workload.assert_called_once_with(config)
        self.assertEqual(stop_event.waits, [100])

    def test_failure_is_logged_and_next_run_still_executes(self):
        config = MonitoringConfig()
        stop_event = StopEvent(stop_after_waits=2)
        outcomes = (RuntimeError("database unavailable"), [object()])
        with patch.object(
            monitoring_worker.time,
            "monotonic",
            side_effect=(100, 101, 160, 162),
        ), patch.object(
            monitoring_worker, "run_monitoring_workload", side_effect=outcomes
        ) as run_workload, patch.object(
            monitoring_worker.data_quality_logs, "create"
        ) as persist_failure, patch.object(
            monitoring_worker, "datetime"
        ) as clock, self.assertLogs(
            monitoring_worker.LOGGER, level="ERROR"
        ):
            clock.now.side_effect = (
                datetime(2026, 1, 1, 2, 4, tzinfo=UTC),
                datetime(2026, 1, 1, 2, 5, 30, tzinfo=UTC),
            )
            monitoring_worker.run_monitoring_worker(config, stop_event)

        self.assertEqual(run_workload.call_count, 2)
        self.assertEqual(stop_event.waits, [60, 270])
        failure = persist_failure.call_args.kwargs
        self.assertEqual(failure["source"], "monitoring")
        self.assertEqual(failure["issue_type"], "monitoring_run_failure")
        self.assertEqual(failure["error_details"], [{"error_type": "RuntimeError"}])

    def test_failure_persistence_is_best_effort(self):
        with patch.object(
            monitoring_worker.data_quality_logs,
            "create",
            side_effect=RuntimeError("database unavailable"),
        ), self.assertLogs(monitoring_worker.LOGGER, level="ERROR") as captured:
            monitoring_worker._persist_monitoring_failure(RuntimeError("failed"), 1.2)

        self.assertIn(
            "monitoring_failure_persistence_failed",
            captured.records[0].getMessage(),
        )

    def test_stopped_worker_does_not_start_workload(self):
        stop_event = StopEvent(stopped=True)
        with patch.object(
            monitoring_worker, "run_monitoring_workload"
        ) as run_workload:
            monitoring_worker.run_monitoring_worker(MonitoringConfig(), stop_event)

        run_workload.assert_not_called()
        self.assertEqual(stop_event.waits, [])

    def test_main_uses_default_config_and_registers_shutdown_signals(self):
        stop_event = StopEvent(stopped=True)
        with patch.object(
            monitoring_worker, "Event", return_value=stop_event
        ), patch.object(
            monitoring_worker.signal, "signal"
        ) as register_signal, patch.object(
            monitoring_worker, "run_monitoring_worker"
        ) as run_worker:
            monitoring_worker.main()

        self.assertEqual(run_worker.call_args.args[0], MonitoringConfig())
        self.assertIs(run_worker.call_args.args[1], stop_event)
        self.assertEqual(
            [registered.args[0] for registered in register_signal.call_args_list],
            [monitoring_worker.signal.SIGTERM, monitoring_worker.signal.SIGINT],
        )


if __name__ == "__main__":
    unittest.main()
