"""Tests for single-run monitoring orchestration."""

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import call, patch

from src.monitoring.schemas import (
    LicensingSummary,
    LLMSummary,
    MonitoringConfig,
    QualitySummary,
)
from src.monitoring import workload


class MonitoringWorkloadTests(unittest.TestCase):
    def setUp(self):
        self.config = MonitoringConfig(window_hours=2)
        self.window_end = datetime(2026, 1, 1, 2, tzinfo=UTC)
        self.generated_at = self.window_end + timedelta(seconds=3)
        self.licensing = LicensingSummary(
            charge_count=2,
            priced_charge_count=1,
            total_license_cost=Decimal("1.25"),
        )
        self.llm = LLMSummary(
            request_count=3,
            priced_request_count=2,
            input_tokens=100,
            output_tokens=20,
            estimated_cost_usd=Decimal("0.01"),
        )
        self.quality = QualitySummary(
            evaluated_rating_count=4,
            attributed_rating_count=2,
            positive_rating_count=1,
            accuracy=0.5,
        )

    def test_calculates_and_persists_one_complete_result(self):
        saved_run = SimpleNamespace(run_id="run-1")

        with patch.object(
            workload, "_utc_now", side_effect=(self.window_end, self.generated_at)
        ), patch.object(
            workload, "calculate_license_fees", return_value=self.licensing
        ) as calculate_license_fees, patch.object(
            workload, "calculate_llm", return_value=self.llm
        ) as calculate_llm, patch.object(
            workload,
            "calculate_live_model_accuracy",
            return_value=self.quality,
        ) as calculate_quality, patch.object(
            workload.monitoring_runs, "create", return_value=saved_run
        ) as create:
            returned_run = workload.run_monitoring_workload(self.config)

        expected_window_start = self.window_end - timedelta(hours=2)
        window = calculate_license_fees.call_args.args[0]
        self.assertEqual(window.start, expected_window_start)
        self.assertEqual(window.end, self.window_end)
        self.assertEqual(calculate_license_fees.call_args, call(window, self.config))
        self.assertEqual(calculate_llm.call_args, call(window, self.config))
        self.assertEqual(calculate_quality.call_args, call(window, self.config))

        result = create.call_args.args[0]
        self.assertEqual(result.generated_at, self.generated_at)
        self.assertEqual(result.window, window)
        self.assertEqual(result.licensing, self.licensing)
        self.assertEqual(result.llm, self.llm)
        self.assertEqual(result.quality, self.quality)
        self.assertIs(returned_run, saved_run)

    def test_calculation_failure_does_not_persist(self):
        with patch.object(
            workload, "_utc_now", return_value=self.window_end
        ), patch.object(
            workload,
            "calculate_license_fees",
            side_effect=RuntimeError("database unavailable"),
        ), patch.object(workload.monitoring_runs, "create") as create:
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                workload.run_monitoring_workload(self.config)

        create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
