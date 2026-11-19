"""Single-validation handoff for decoded Kafka events to PostgreSQL."""

import logging
from collections.abc import Callable, Iterable

from pydantic import ValidationError

from src.data_quality.quality_logging import (
    POSTGRES_SINK, LogContext, QualityLogSink, log_validation_failure,
)


def validate_stream_events(raw_events: Iterable[object]):
    """Validate records independently so one rejection cannot poison a batch."""
    from src.database.schemas import StreamEventCreate

    valid = []
    failures = []
    for raw_event in raw_events:
        try:
            valid.append(StreamEventCreate.model_validate(raw_event))
        except ValidationError as exc:
            failures.append(
                exc.errors(
                    include_url=False, include_context=False, include_input=False
                )
            )
    return valid, failures


def persist_stream_events(
    raw_events: Iterable[object], *, create_many: Callable | None = None,
) -> tuple[int, list[list[dict]]]:
    """Validate independently and persist all accepted events with one call."""
    if create_many is None:
        from src.database.interactions import stream_events

        create_many = stream_events.create_many
    valid, failures = validate_stream_events(raw_events)
    inserted = create_many(valid) if valid else 0
    return inserted, failures


def persist_stream_event(
    raw_event: object, *, create_many: Callable | None = None,
    logger: logging.Logger | None = None, sink: QualityLogSink | None = POSTGRES_SINK,
    context: LogContext | None = None,
) -> tuple[int | None, list[dict]]:
    """Let the database validate once; catch only rejected input, not DB outages.

    Return (inserted_count, []) on success, including zero for duplicate UUIDs,
    or (None, errors) on invalid input. Do not pre-call validate_stream_event.
    SQL/connection failures propagate so the consumer can retry without commits.
    """
    if create_many is None:
        from src.database.interactions import stream_events

        create_many = stream_events.create_many
    try:
        return create_many([raw_event]), []
    except ValidationError as exc:
        errors = exc.errors(
            include_url=False, include_context=False, include_input=False
        )
        log_validation_failure("kafka", errors, logger=logger, sink=sink, context=context)
        return None, errors
