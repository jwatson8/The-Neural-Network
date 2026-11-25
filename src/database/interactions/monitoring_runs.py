"""Idempotent persistence for completed monitoring buckets."""

from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import delete
from sqlmodel import Session, select

from src.database.interactions._pagination import (
    SearchCursor,
    SearchPage,
    search_records,
)
from src.database.interactions._shared import (
    create_record,
    dialect_insert,
    run,
)
from src.database.interactions._shared import (
    list_records as list_model_records,
)
from src.database.models import (
    MonitoringModelAccuracy,
    MonitoringRouteLatency,
    MonitoringRun,
)
from src.monitoring.schemas import MonitorResult

__all__ = [
    "create",
    "get",
    "list_records",
    "list_windows",
    "search",
    "upsert_many",
]


def _result_values(result: MonitorResult, run_id: UUID) -> dict[str, Any]:
    return {
        "run_id": run_id,
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
        "request_count": result.latency.request_count,
        "request_count_over_600ms": result.latency.request_count_over_600ms,
        "request_latency_average_ms": result.latency.average_ms,
        "request_latency_p50_ms": result.latency.p50_ms,
        "request_latency_p95_ms": result.latency.p95_ms,
        "request_latency_p99_ms": result.latency.p99_ms,
        "request_latency_maximum_ms": result.latency.maximum_ms,
        "rating_drift_tvd": result.drift.rating_drift_tvd,
        "event_type_drift_tvd": result.drift.event_type_drift_tvd,
        "rating_drift_sample_count": result.drift.rating_drift_sample_count,
        "event_type_baseline_counts": result.drift.event_type_baseline_counts,
        "event_type_recent_counts": result.drift.event_type_recent_counts,
        "availability_request_count": result.availability.request_count,
        "availability_success_count": result.availability.success_count,
        "availability_no_response_count": result.availability.no_response_count,
        "availability_other_failure_count": result.availability.other_failure_count,
        "kafka_record_count": result.rejections.kafka_record_count,
        "rejected_invalid_encoding_count": result.rejections.invalid_encoding_count,
        "rejected_malformed_line_count": result.rejections.malformed_line_count,
        "rejected_unrecognized_event_count": result.rejections.unrecognized_event_count,
        "rejected_invalid_user_id_count": result.rejections.invalid_user_id_count,
        "rejected_invalid_event_time_count": result.rejections.invalid_event_time_count,
        "rejected_invalid_field_count": result.rejections.invalid_field_count,
        "rejected_unknown_movie_count": result.rejections.unknown_movie_count,
        "rejected_total_count": result.rejections.total_count,
    }


def _model_accuracy_values(
    result: MonitorResult, run_id: UUID
) -> list[dict[str, Any]]:
    return [
        {
            "run_id": run_id,
            "model_route": summary.model_route,
            "model_version": summary.model_version,
            "attributed_rating_count": summary.attributed_rating_count,
            "positive_rating_count": summary.positive_rating_count,
            "recommendation_accuracy": summary.accuracy,
        }
        for summary in result.quality.models
    ]


def _route_latency_values(
    result: MonitorResult, run_id: UUID
) -> list[dict[str, Any]]:
    return [
        {
            "run_id": run_id,
            "model_route": summary.model_route,
            "request_count": summary.request_count,
            "request_count_over_600ms": summary.request_count_over_600ms,
            "request_latency_average_ms": summary.average_ms,
            "request_latency_p50_ms": summary.p50_ms,
            "request_latency_p95_ms": summary.p95_ms,
            "request_latency_p99_ms": summary.p99_ms,
            "request_latency_maximum_ms": summary.maximum_ms,
        }
        for summary in result.latency.routes
    ]


def _bucket_run_id(result: MonitorResult) -> UUID:
    identity = (
        f"monitoring:{result.window.start.isoformat()}:{result.window.end.isoformat()}"
    )
    return uuid5(NAMESPACE_URL, identity)


def _create(
    session: Session,
    result: MonitorResult,
    run_id: UUID | None,
) -> MonitoringRun:
    resolved_run_id = run_id or _bucket_run_id(result)
    record = create_record(
        session,
        MonitoringRun,
        _result_values(result, resolved_run_id),
    )
    model_values = _model_accuracy_values(result, resolved_run_id)
    if model_values:
        session.execute(
            dialect_insert(session, MonitoringModelAccuracy).values(model_values)
        )
        session.flush()
    route_values = _route_latency_values(result, resolved_run_id)
    if route_values:
        session.execute(
            dialect_insert(session, MonitoringRouteLatency).values(route_values)
        )
        session.flush()
    return record


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


def _upsert_many(
    session: Session,
    results: list[MonitorResult],
) -> list[MonitoringRun]:
    if not results:
        return []
    values = [_result_values(result, _bucket_run_id(result)) for result in results]
    statement = dialect_insert(session, MonitoringRun).values(values)
    update_fields = tuple(
        key for key in values[0] if key != "run_id"
    )
    statement = statement.on_conflict_do_update(
        index_elements=["run_id"],
        set_={field: getattr(statement.excluded, field) for field in update_fields},
    )
    session.execute(statement)
    session.flush()
    run_ids = [value["run_id"] for value in values]
    session.execute(
        delete(MonitoringModelAccuracy).where(
            MonitoringModelAccuracy.run_id.in_(run_ids)
        )
    )
    session.execute(
        delete(MonitoringRouteLatency).where(
            MonitoringRouteLatency.run_id.in_(run_ids)
        )
    )
    model_values = [
        values
        for result, run_id in zip(results, run_ids, strict=True)
        for values in _model_accuracy_values(result, run_id)
    ]
    if model_values:
        session.execute(
            dialect_insert(session, MonitoringModelAccuracy).values(model_values)
        )
    route_values = [
        values
        for result, run_id in zip(results, run_ids, strict=True)
        for values in _route_latency_values(result, run_id)
    ]
    if route_values:
        session.execute(
            dialect_insert(session, MonitoringRouteLatency).values(route_values)
        )
    session.flush()
    return list(
        session.exec(
            select(MonitoringRun)
            .where(MonitoringRun.run_id.in_(run_ids))
            .order_by(MonitoringRun.window_start)
        ).all()
    )


def upsert_many(
    results: list[MonitorResult | dict[str, Any]],
) -> list[MonitoringRun]:
    """Atomically insert or replace deterministic monitoring buckets."""
    validated = [MonitorResult.model_validate(result) for result in results]
    return run(lambda session: _upsert_many(session, validated))


def list_windows(*, start: datetime, end: datetime) -> set[tuple[datetime, datetime]]:
    """Return stored windows fully contained in a bounded time range."""

    def utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def operation(session: Session):
        rows = session.exec(
            select(MonitoringRun.window_start, MonitoringRun.window_end).where(
                MonitoringRun.window_start >= start,
                MonitoringRun.window_end <= end,
            )
        ).all()
        return {
            (utc(window_start), utc(window_end))
            for window_start, window_end in rows
        }

    return run(operation)


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
