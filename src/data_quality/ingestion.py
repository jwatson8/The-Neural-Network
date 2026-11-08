"""Single-validation handoff for decoded Kafka events to PostgreSQL."""

import logging
from collections.abc import Callable

from pydantic import ValidationError

from src.data_quality.quality_logging import (
    POSTGRES_SINK, LogContext, QualityLogSink, log_validation_failure,
)


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
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
        log_validation_failure("kafka", errors, logger=logger, sink=sink, context=context)
        return None, errors
