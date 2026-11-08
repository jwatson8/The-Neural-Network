"""Tests for bounded monitoring bucket orchestration."""

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from src.monitoring import workload
from src.monitoring.schemas import (
    LicensingSummary,
    LLMSummary,
    MonitoringConfig,
    QualitySummary,
)


class MonitoringWorkloadTests(unittest.TestCase):
    def setUp(self):
        self.config = MonitoringConfig(backfill_hours=1)
        self.now = datetime(2026, 1, 1, 2, 3, tzinfo=UTC)
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

    def _summaries(self, value):
        return lambda windows, _config: {window.start: value for window in windows}

    def test_backfills_missing_completed_buckets_and_persists_one_batch(self):
        saved = [object() for _ in range(12)]
        with patch.object(workload, "_utc_now", return_value=self.now), patch.object(
            workload.monitoring_runs, "list_windows", return_value=set()
        ), patch.object(
            workload,
            "calculate_license_fee_buckets",
            side_effect=self._summaries(self.licensing),
        ), patch.object(
            workload,
            "calculate_llm_buckets",
            side_effect=self._summaries(self.llm),
        ), patch.object(
            workload,
            "calculate_live_model_accuracy_buckets",
            side_effect=self._summaries(self.quality),
        ), patch.object(
            workload.monitoring_runs, "upsert_many", return_value=saved
        ) as upsert:
            returned = workload.run_monitoring_workload(self.config)

        results = upsert.call_args.args[0]
        self.assertIs(returned, saved)
        self.assertEqual(len(results), 12)
        self.assertEqual(results[0].window.start, datetime(2026, 1, 1, 1, tzinfo=UTC))
        self.assertEqual(results[-1].window.end, datetime(2026, 1, 1, 2, tzinfo=UTC))
        self.assertTrue(all(result.generated_at == self.now for result in results))

    def test_existing_history_still_reconciles_only_the_latest_hour(self):
        config = MonitoringConfig(backfill_hours=2)
        bucket_end = datetime(2026, 1, 1, 2, tzinfo=UTC)
        windows = workload.windows_between(
            bucket_end - timedelta(hours=2), bucket_end, 5
        )
        stored = {(window.start, window.end) for window in windows}

        with patch.object(workload.monitoring_runs, "list_windows", return_value=stored):
            targets = workload._target_windows(config, bucket_end)

        self.assertEqual(len(targets), 12)
        self.assertEqual(targets[0].start, bucket_end - timedelta(hours=1))

    def test_calculation_failure_does_not_persist_partial_results(self):
        with patch.object(workload, "_utc_now", return_value=self.now), patch.object(
            workload.monitoring_runs, "list_windows", return_value=set()
        ), patch.object(
            workload,
            "calculate_license_fee_buckets",
            side_effect=RuntimeError("database unavailable"),
        ), patch.object(workload.monitoring_runs, "upsert_many") as upsert:
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                workload.run_monitoring_workload(self.config)

        upsert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
