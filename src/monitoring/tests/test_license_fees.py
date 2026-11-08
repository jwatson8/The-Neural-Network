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
                        charged_at=self.windows[0].start,
                        license_cost=Decimal("1.25"),
                    ),
                    SimpleNamespace(charged_at=self.windows[0].start, license_cost=None),
                ),
                next_cursor=next_cursor,
            ),
            SimpleNamespace(
                records=(
                    SimpleNamespace(
                        charged_at=self.windows[1].start,
                        license_cost=Decimal("0.50"),
                    ),
                ),
                next_cursor=None,
            ),
        )

        with patch.object(
            license_fees.licensing_costs, "search", side_effect=pages
        ) as search:
            summaries = license_fees.calculate_license_fee_buckets(
                self.windows, self.config
            )

        self.assertEqual(summaries[self.windows[0].start].charge_count, 2)
        self.assertEqual(summaries[self.windows[0].start].priced_charge_count, 1)
        self.assertEqual(
            summaries[self.windows[0].start].total_license_cost, Decimal("1.25")
        )
        self.assertEqual(summaries[self.windows[1].start].charge_count, 1)
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
            license_fees.licensing_costs, "search", return_value=empty_page
        ):
            summaries = license_fees.calculate_license_fee_buckets(
                self.windows, self.config
            )

        for summary in summaries.values():
            self.assertEqual(summary.charge_count, 0)
            self.assertEqual(summary.priced_charge_count, 0)
            self.assertEqual(summary.total_license_cost, Decimal("0"))


if __name__ == "__main__":
    unittest.main()
