"""Summarize licensing charges into independent monitoring buckets."""

from datetime import datetime
from decimal import Decimal

from src.database.interactions import licensing_costs
from src.monitoring.buckets import floor_to_bucket
from src.monitoring.schemas import (
    EvaluationWindow,
    LicensingSummary,
    MonitoringConfig,
)


def calculate_license_fee_buckets(
    windows: list[EvaluationWindow],
    config: MonitoringConfig,
) -> dict[datetime, LicensingSummary]:
    """Read the enclosing range once and summarize only requested buckets."""
    if not windows:
        return {}
    starts = {window.start for window in windows}
    totals = {
        start: {"count": 0, "priced": 0, "cost": Decimal("0")}
        for start in starts
    }
    cursor = None
    while True:
        page = licensing_costs.search(
            start=min(starts),
            end=max(window.end for window in windows),
            cursor=cursor,
            limit=config.page_size,
        )
        for charge in page.records:
            bucket_start = floor_to_bucket(charge.charged_at, config.bucket_minutes)
            if bucket_start not in totals:
                continue
            totals[bucket_start]["count"] += 1
            if charge.license_cost is not None:
                totals[bucket_start]["priced"] += 1
                totals[bucket_start]["cost"] += charge.license_cost

        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    return {
        start: LicensingSummary(
            charge_count=values["count"],
            priced_charge_count=values["priced"],
            total_license_cost=values["cost"],
        )
        for start, values in totals.items()
    }


def calculate_license_fees(
    window: EvaluationWindow,
    config: MonitoringConfig,
) -> LicensingSummary:
    """Compatibility wrapper for calculating one bucket."""
    return calculate_license_fee_buckets([window], config)[window.start]
