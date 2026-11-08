"""Append-only persistence for completed monitoring runs."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlmodel import Session

from src.database.interactions._pagination import (
    SearchCursor,
    SearchPage,
    search_records,
)
from src.database.interactions._shared import (
    create_record,
    run,
)
from src.database.interactions._shared import (
    list_records as list_model_records,
)
from src.database.models import MonitoringRun
from src.monitoring.schemas import MonitorResult

__all__ = ["create", "get", "list_records", "search"]


def _create(
    session: Session,
    result: MonitorResult,
    run_id: UUID | None,
) -> MonitoringRun:
    values: dict[str, Any] = {
        "generated_at": result.generated_at,
        "window_start": result.window.start,
        "window_end": result.window.end,
        "licensing_charge_count": result.licensing.charge_count,
        "licensing_priced_charge_count": result.licensing.priced_charge_count,
        "total_license_cost": result.licensing.total_license_cost,
        "llm_request_count": result.llm.request_count,
        "llm_priced_request_count": result.llm.priced_request_count,
        "llm_input_tokens": result.llm.input_tokens,
        "llm_output_tokens": result.llm.output_tokens,
        "llm_estimated_cost_usd": result.llm.estimated_cost_usd,
        "evaluated_rating_count": result.quality.evaluated_rating_count,
        "attributed_rating_count": result.quality.attributed_rating_count,
        "positive_rating_count": result.quality.positive_rating_count,
        "recommendation_accuracy": result.quality.accuracy,
    }
    if run_id is not None:
        values["run_id"] = run_id
    return create_record(session, MonitoringRun, values)


def create(
    result: MonitorResult | dict[str, Any],
    *,
    run_id: UUID | None = None,
) -> MonitoringRun:
    """Persist one validated, completed monitoring result."""
    validated_result = MonitorResult.model_validate(result)
    return run(lambda session: _create(session, validated_result, run_id))


def get(run_id: UUID) -> MonitoringRun | None:
    """Return the monitoring run identified by its UUID, or ``None``."""
    return run(lambda session: session.get(MonitoringRun, run_id))


def list_records(*, offset: int = 0, limit: int = 100) -> list[MonitoringRun]:
    """Return monitoring runs using zero-based offset and capped limit."""
    return run(
        lambda session: list_model_records(
            session, MonitoringRun, offset=offset, limit=limit
        )
    )


def search(
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    cursor: SearchCursor | None = None,
    limit: int = 100,
) -> SearchPage[MonitoringRun]:
    """Search runs newest-first by generation time."""
    return run(
        lambda session: search_records(
            session,
            MonitoringRun,
            timestamp_column=MonitoringRun.generated_at,
            id_column=MonitoringRun.run_id,
            scope="monitoring_runs:generated_at",
            conditions=[],
            start=start,
            end=end,
            cursor=cursor,
            limit=limit,
        )
    )
