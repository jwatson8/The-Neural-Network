"""Summarize LLM usage into independent monitoring buckets."""

from datetime import datetime
from decimal import Decimal

from src.database.interactions import llm_usage
from src.monitoring.buckets import floor_to_bucket
from src.monitoring.schemas import EvaluationWindow, LLMSummary, MonitoringConfig


def calculate_llm_buckets(
    windows: list[EvaluationWindow],
    config: MonitoringConfig,
) -> dict[datetime, LLMSummary]:
    """Read the enclosing range once and summarize only requested buckets."""
    if not windows:
        return {}
    starts = {window.start for window in windows}
    totals = {
        start: {
            "count": 0,
            "priced": 0,
            "input": 0,
            "output": 0,
            "cost": Decimal("0"),
        }
        for start in starts
    }
    cursor = None
    while True:
        page = llm_usage.search(
            start=min(starts),
            end=max(window.end for window in windows),
            cursor=cursor,
            limit=config.page_size,
        )
        for usage in page.records:
            bucket_start = floor_to_bucket(usage.occurred_at, config.bucket_minutes)
            if bucket_start not in totals:
                continue
            values = totals[bucket_start]
            values["count"] += 1
            values["input"] += usage.input_tokens
            values["output"] += usage.output_tokens
            if usage.estimated_cost_usd is not None:
                values["priced"] += 1
                values["cost"] += usage.estimated_cost_usd

        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    return {
        start: LLMSummary(
            request_count=values["count"],
            priced_request_count=values["priced"],
            input_tokens=values["input"],
            output_tokens=values["output"],
            estimated_cost_usd=values["cost"],
        )
        for start, values in totals.items()
    }


def calculate_llm(
    window: EvaluationWindow,
    config: MonitoringConfig,
) -> LLMSummary:
    """Compatibility wrapper for calculating one bucket."""
    return calculate_llm_buckets([window], config)[window.start]
