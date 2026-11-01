"""Immutable LLM usage audit interactions."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlmodel import Session

from src.database.interactions._shared import (
    create_record,
    run,
    schema_values,
)
from src.database.interactions._shared import (
    list_records as list_model_records,
)
from src.database.models import LLMUsage
from src.database.schemas import LLMUsageCreate

__all__ = ["create", "get", "list_records"]

### ----------- Helper Functions -----------


def _create(session: Session, data: LLMUsageCreate) -> LLMUsage:
    return create_record(session, LLMUsage, schema_values(data))


def _get(session: Session, request_id: UUID) -> LLMUsage | None:
    return session.get(LLMUsage, request_id)


def _list(session: Session, *, offset: int, limit: int) -> list[LLMUsage]:
    return list_model_records(session, LLMUsage, offset=offset, limit=limit)


### ----------- Individual Functions -----------


def create(
    *,
    provider: str,
    model_name: str,
    status: str,
    user_id: str | None = None,
    occurred_at: datetime | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    estimated_cost_usd: Decimal | None = None,
    request_id: UUID | None = None,
) -> LLMUsage:
    """Commit one LLM usage audit.

    Supply provider/model/status plus optional user correlation, occurrence time,
    non-negative token counts, estimated USD cost, and caller request UUID.
    """
    data = LLMUsageCreate(**locals())
    return run(lambda session: _create(session, data))


def get(request_id: UUID) -> LLMUsage | None:
    """Return the LLM usage audit identified by ``request_id``, or ``None``."""
    return run(lambda session: _get(session, request_id))


### ----------- Batch Functions -----------


def list_records(*, offset: int = 0, limit: int = 100) -> list[LLMUsage]:
    """Return LLM audits using zero-based ``offset`` and capped ``limit``."""
    return run(lambda session: _list(session, offset=offset, limit=limit))
