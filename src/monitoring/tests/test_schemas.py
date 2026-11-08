"""Tests for monitoring configuration and result schemas."""

import unittest
from datetime import UTC, datetime, timedelta, timezone

from pydantic import ValidationError

from src.monitoring.schemas import (
    EvaluationWindow,
    LicensingSummary,
    LLMSummary,
    MonitorResult,
    QualitySummary,
)


class MonitoringSchemaTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 1, 2, 3, tzinfo=UTC)
        self.end = self.start + timedelta(hours=2)

    def test_evaluation_window_normalizes_boundaries_to_utc(self):
        central_time = timezone(timedelta(hours=-6))
        window = EvaluationWindow(
            start=self.start.astimezone(central_time),
            end=self.end.astimezone(central_time),
        )

        self.assertEqual(window.start, self.start)
        self.assertEqual(window.end, self.end)
        self.assertIs(window.start.tzinfo, UTC)
        self.assertIs(window.end.tzinfo, UTC)
        with self.assertRaises(ValidationError):
            EvaluationWindow(start=self.start.replace(tzinfo=None), end=self.end)
        with self.assertRaises(ValidationError):
            EvaluationWindow(start=self.start, end=self.start)

    def test_priced_counts_cannot_exceed_total_counts(self):
        with self.assertRaises(ValidationError):
            LicensingSummary(
                charge_count=1,
                priced_charge_count=2,
                total_license_cost="1.25",
            )
        with self.assertRaises(ValidationError):
            LLMSummary(
                request_count=1,
                priced_request_count=2,
                input_tokens=0,
                output_tokens=0,
                estimated_cost_usd="0",
            )

    def test_quality_summary_enforces_count_and_accuracy_contract(self):
        summary = QualitySummary(
            evaluated_rating_count=40,
            attributed_rating_count=20,
            positive_rating_count=15,
            accuracy=0.75,
        )

        self.assertEqual(summary.evaluated_rating_count, 40)
        self.assertEqual(summary.attributed_rating_count, 20)
        self.assertEqual(summary.positive_rating_count, 15)
        self.assertEqual(summary.accuracy, 0.75)
        empty_summary = QualitySummary(
            evaluated_rating_count=12,
            attributed_rating_count=0,
            positive_rating_count=0,
        )
        self.assertIsNone(empty_summary.accuracy)

        invalid_values = (
            (10, 11, 5, 5 / 11),
            (10, 5, 6, 1),
            (10, 5, 4, 0.5),
        )
        for evaluated, attributed, positive, accuracy in invalid_values:
            with self.subTest(values=(evaluated, attributed, positive, accuracy)):
                with self.assertRaises(ValidationError):
                    QualitySummary(
                        evaluated_rating_count=evaluated,
                        attributed_rating_count=attributed,
                        positive_rating_count=positive,
                        accuracy=accuracy,
                    )

    def test_monitor_result_constructs_and_serializes_nested_payload(self):
        result = MonitorResult(
            generated_at=self.end.astimezone(timezone(timedelta(hours=2))),
            window={"start": self.start, "end": self.end},
            licensing={
                "charge_count": 2,
                "priced_charge_count": 1,
                "total_license_cost": "0.50",
            },
            llm={
                "request_count": 1,
                "priced_request_count": 1,
                "input_tokens": 20,
                "output_tokens": 5,
                "estimated_cost_usd": "0.001",
            },
            quality={
                "evaluated_rating_count": 40,
                "attributed_rating_count": 20,
                "positive_rating_count": 15,
                "accuracy": 0.75,
            },
        )

        payload = result.model_dump(mode="json")
        self.assertEqual(result.generated_at, self.end)
        self.assertEqual(
            payload["window"]["start"],
            self.start.isoformat().replace("+00:00", "Z"),
        )
        self.assertEqual(payload["quality"]["accuracy"], 0.75)


if __name__ == "__main__":
    unittest.main()
