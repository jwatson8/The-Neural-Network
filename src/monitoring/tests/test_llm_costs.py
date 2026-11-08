"""Tests for LLM usage monitoring calculations."""

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import call, patch

from src.monitoring.calculations import llm_costs
from src.monitoring.schemas import EvaluationWindow, MonitoringConfig


class LLMCalculationTests(unittest.TestCase):
    def setUp(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        self.windows = [
            EvaluationWindow(start=start, end=start + timedelta(minutes=5)),
            EvaluationWindow(
                start=start + timedelta(minutes=5),
                end=start + timedelta(minutes=10),
            ),
        ]
        self.config = MonitoringConfig(backfill_hours=1, page_size=2)

    def test_summarizes_all_pages_and_excludes_unpriced_costs(self):
        next_cursor = object()
        pages = (
            SimpleNamespace(
                records=(
                    SimpleNamespace(
                        occurred_at=self.windows[0].start,
                        input_tokens=100,
                        output_tokens=20,
                        estimated_cost_usd=Decimal("0.01"),
                    ),
                    SimpleNamespace(
                        occurred_at=self.windows[0].start,
                        input_tokens=50,
                        output_tokens=10,
                        estimated_cost_usd=None,
                    ),
                ),
                next_cursor=next_cursor,
            ),
            SimpleNamespace(
                records=(
                    SimpleNamespace(
                        occurred_at=self.windows[1].start,
                        input_tokens=25,
                        output_tokens=5,
                        estimated_cost_usd=Decimal("0.02"),
                    ),
                ),
                next_cursor=None,
            ),
        )

        with patch.object(
            llm_costs.llm_usage, "search", side_effect=pages
        ) as search:
            summaries = llm_costs.calculate_llm_buckets(self.windows, self.config)

        first = summaries[self.windows[0].start]
        second = summaries[self.windows[1].start]
        self.assertEqual(first.request_count, 2)
        self.assertEqual(first.priced_request_count, 1)
        self.assertEqual(first.input_tokens, 150)
        self.assertEqual(first.output_tokens, 30)
        self.assertEqual(first.estimated_cost_usd, Decimal("0.01"))
        self.assertEqual(second.request_count, 1)
        self.assertEqual(
            search.call_args_list,
            [
                call(
                    start=self.windows[0].start,
                    end=self.windows[1].end,
                    cursor=None,
                    limit=2,
                ),
                call(
                    start=self.windows[0].start,
                    end=self.windows[1].end,
                    cursor=next_cursor,
                    limit=2,
                ),
            ],
        )

    def test_empty_window_returns_zero_summary(self):
        empty_page = SimpleNamespace(records=(), next_cursor=None)

        with patch.object(
            llm_costs.llm_usage, "search", return_value=empty_page
        ):
            summaries = llm_costs.calculate_llm_buckets(self.windows, self.config)

        for summary in summaries.values():
            self.assertEqual(summary.request_count, 0)
            self.assertEqual(summary.priced_request_count, 0)
            self.assertEqual(summary.input_tokens, 0)
            self.assertEqual(summary.output_tokens, 0)
            self.assertEqual(summary.estimated_cost_usd, Decimal("0"))


if __name__ == "__main__":
    unittest.main()
