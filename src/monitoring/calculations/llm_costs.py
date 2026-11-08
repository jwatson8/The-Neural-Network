"""Summarize LLM usage within one monitoring window."""

from decimal import Decimal

from src.database.interactions import llm_usage
from src.monitoring.schemas import EvaluationWindow, LLMSummary, MonitoringConfig


def calculate_llm(
    window: EvaluationWindow,
    config: MonitoringConfig,
) -> LLMSummary:
    """Return LLM usage totals for the half-open evaluation window."""
    cursor = None
    request_count = 0
    priced_request_count = 0
    input_tokens = 0
    output_tokens = 0
    estimated_cost_usd = Decimal("0")

    while True:
        page = llm_usage.search(
            start=window.start,
            end=window.end,
            cursor=cursor,
            limit=config.page_size,
        )
        request_count += len(page.records)
        for usage in page.records:
            input_tokens += usage.input_tokens
            output_tokens += usage.output_tokens
            if usage.estimated_cost_usd is not None:
                priced_request_count += 1
                estimated_cost_usd += usage.estimated_cost_usd

        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    return LLMSummary(
        request_count=request_count,
        priced_request_count=priced_request_count,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_usd=estimated_cost_usd,
    )
