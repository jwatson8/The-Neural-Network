"""Stable, domain-separated SHA-256 enrollment and variant allocation."""

import hashlib
import json
from decimal import Decimal
from typing import Literal
from uuid import UUID

Variant = Literal["control", "treatment"]
HASH_SPACE = 1 << 256
HASH_VERSION = "experiment-assignment-v1"


def assignment_bucket(experiment_id: UUID, user_id: str, purpose: Literal["enrollment", "variant"]) -> int:
    """Hash a canonical UTF-8 tuple; do not change this contract for existing experiments."""
    if not isinstance(experiment_id, UUID):
        raise ValueError("experiment_id must be a UUID")
    if not isinstance(user_id, str) or not user_id.strip() or len(user_id) > 255:
        raise ValueError("user_id must be a nonblank string of at most 255 characters")
    if purpose not in ("enrollment", "variant"):
        raise ValueError("purpose must be enrollment or variant")
    payload = json.dumps([HASH_VERSION, str(experiment_id), purpose, user_id],
                         ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest(), "big")


def below_allocation(bucket: int, allocation: Decimal) -> bool:
    """Compare exactly against [0, allocation), without float/Decimal rounding."""
    if not 0 <= bucket < HASH_SPACE:
        raise ValueError("bucket must be in the SHA-256 range")
    if not isinstance(allocation, Decimal) or not allocation.is_finite() or not 0 <= allocation <= 1:
        raise ValueError("allocation must be a finite Decimal between zero and one")
    numerator, denominator = allocation.as_integer_ratio()
    return bucket * denominator < numerator * HASH_SPACE


def enrolled(experiment_id: UUID, user_id: str, allocation: Decimal) -> bool:
    """Select eligible traffic independently from control/treatment allocation."""
    return below_allocation(assignment_bucket(experiment_id, user_id, "enrollment"), allocation)


def assigned_variant(experiment_id: UUID, user_id: str, treatment_allocation: Decimal) -> Variant:
    """Return treatment below its threshold, otherwise control."""
    return "treatment" if below_allocation(
        assignment_bucket(experiment_id, user_id, "variant"), treatment_allocation
    ) else "control"
