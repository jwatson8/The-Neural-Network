from typing import Any

from pydantic import ValidationError

from src.database.schemas import StreamEventCreate


def validate_stream_event(
    raw_event: dict[str, Any],
) -> tuple[StreamEventCreate | None, list[dict]]:
    """
    Validate a Kafka stream event.

    Returns:
        (validated_event, errors)
    """
    try:
        event = StreamEventCreate.model_validate(raw_event)
        return event, []

    except ValidationError as exc:
        return None, exc.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        )
