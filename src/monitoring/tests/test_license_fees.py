"""Tests for licensing-fee monitoring calculations."""

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import call, patch

from src.monitoring.calculations import license_fees
from src.monitoring.schemas import EvaluationWindow, MonitoringConfig


class LicenseFeeCalculationTests(unittest.TestCase):
    def setUp(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        self.window = EvaluationWindow(start=start, end=start + timedelta(hours=2))
        self.config = MonitoringConfig(window_hours=2, page_size=2)

    def test_summarizes_all_pages_and_excludes_unpriced_costs(self):
        next_cursor = object()
        pages = (
            SimpleNamespace(
                records=(
                    SimpleNamespace(license_cost=Decimal("1.25")),
                    SimpleNamespace(license_cost=None),
                ),
                next_cursor=next_cursor,
            ),
            SimpleNamespace(
                records=(SimpleNamespace(license_cost=Decimal("0.50")),),
                next_cursor=None,
            ),
        )

        with patch.object(
            license_fees.licensing_costs, "search", side_effect=pages
        ) as search:
            summary = license_fees.calculate_license_fees(self.window, self.config)

        self.assertEqual(summary.charge_count, 3)
        self.assertEqual(summary.priced_charge_count, 2)
        self.assertEqual(summary.total_license_cost, Decimal("1.75"))
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
            license_fees.licensing_costs, "search", return_value=empty_page
        ):
            summary = license_fees.calculate_license_fees(self.window, self.config)

        self.assertEqual(summary.charge_count, 0)
        self.assertEqual(summary.priced_charge_count, 0)
        self.assertEqual(summary.total_license_cost, Decimal("0"))


if __name__ == "__main__":
    unittest.main()
