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
        self.window = EvaluationWindow(start=start, end=start + timedelta(hours=2))
        self.config = MonitoringConfig(window_hours=2, page_size=2)

    def test_summarizes_all_pages_and_excludes_unpriced_costs(self):
        next_cursor = object()
        pages = (
            SimpleNamespace(
                records=(
                    SimpleNamespace(
                        input_tokens=100,
                        output_tokens=20,
                        estimated_cost_usd=Decimal("0.01"),
                    ),
                    SimpleNamespace(
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
            summary = llm_costs.calculate_llm(self.window, self.config)

        self.assertEqual(summary.request_count, 3)
        self.assertEqual(summary.priced_request_count, 2)
        self.assertEqual(summary.input_tokens, 175)
        self.assertEqual(summary.output_tokens, 35)
        self.assertEqual(summary.estimated_cost_usd, Decimal("0.03"))
        self.assertEqual(
            search.call_args_list,
            [
                call(
                    start=self.window.start,
                    end=self.window.end,
                    cursor=None,
                    limit=2,
                ),
                call(
                    start=self.window.start,
                    end=self.window.end,
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
            summary = llm_costs.calculate_llm(self.window, self.config)

        self.assertEqual(summary.request_count, 0)
        self.assertEqual(summary.priced_request_count, 0)
        self.assertEqual(summary.input_tokens, 0)
        self.assertEqual(summary.output_tokens, 0)
        self.assertEqual(summary.estimated_cost_usd, Decimal("0"))


if __name__ == "__main__":
    unittest.main()
