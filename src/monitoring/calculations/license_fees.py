"""Summarize licensing charges within one monitoring window."""

from decimal import Decimal

from src.database.interactions import licensing_costs
from src.monitoring.schemas import (
    EvaluationWindow,
    LicensingSummary,
    MonitoringConfig,
)


def calculate_license_fees(
    window: EvaluationWindow,
    config: MonitoringConfig,
) -> LicensingSummary:
    """Return licensing totals for the half-open evaluation window."""
    cursor = None
    charge_count = 0
    priced_charge_count = 0
    total_license_cost = Decimal("0")

    while True:
        page = licensing_costs.search(
            start=window.start,
            end=window.end,
            cursor=cursor,
            limit=config.page_size,
        )
        charge_count += len(page.records)
        for charge in page.records:
            if charge.license_cost is not None:
                priced_charge_count += 1
                total_license_cost += charge.license_cost

        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    return LicensingSummary(
        charge_count=charge_count,
        priced_charge_count=priced_charge_count,
        total_license_cost=total_license_cost,
    )
