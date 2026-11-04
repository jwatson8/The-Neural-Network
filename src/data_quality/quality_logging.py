"""Privacy-filtered JSON messages; handlers and formatting belong to callers."""

import json
import logging
from collections.abc import Mapping, Sequence
from math import isfinite
from typing import get_args

from pydantic_core import ErrorType

from src.database.schemas import MovieCreate, RecommendationItem, StreamEventCreate, UserCreate

LOGGER = logging.getLogger(__name__)
SOURCES = frozenset({"kafka", "user_api", "movie_api", "recommendation_api"})
FIELDS = frozenset(
    field
    for schema in (UserCreate, MovieCreate, StreamEventCreate, RecommendationItem)
    for field in schema.model_fields
) | {"user", "query_params", "recommendations", "k", "limit", "baseline", "recent"}
ERROR_TYPES = frozenset(get_args(ErrorType))
DRIFT_LEVELS = {
    "drift_detected": logging.WARNING,
    "no_drift": logging.INFO,
    "insufficient_data": logging.INFO,
    "invalid_data": logging.ERROR,
}


def _logger(logger: logging.Logger | None) -> logging.Logger:
    if logger is None:
        return LOGGER
    if not isinstance(logger, logging.Logger):
        raise TypeError("logger must be a logging.Logger")
    return logger


def _count(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _number(value: object, name: str) -> int | float:
    if type(value) not in (int, float) or not 0 <= value <= 1 or not isfinite(value):
        raise ValueError(f"{name} must be a finite number in [0, 1]")
    return value


def _issues(errors: object) -> list[dict]:
    if not isinstance(errors, (list, tuple)):
        raise TypeError("errors must be a list or tuple of structured errors")
    issues = []
    for error in errors:
        if not isinstance(error, Mapping):
            raise TypeError("Each error must be a mapping")
        location = error["loc"]
        error_type = error["type"]
        if not isinstance(location, (list, tuple)) or not isinstance(error_type, str):
            raise TypeError("Error locations must be sequences and types must be strings")
        # Never stringify unknown objects/keys: even repr() can reveal payloads.
        safe_location = [
            part if type(part) is str and part in FIELDS
            else "<index>" if type(part) is int and part >= 0
            else "<redacted>"
            for part in location
        ]
        issues.append({
            "field": ".".join(safe_location) if safe_location else "$",
            "location": safe_location,
            "validation_error_type": error_type if error_type in ERROR_TYPES else "<redacted>",
        })
    return issues


def log_validation_failure(
    source: str,
    errors: Sequence[Mapping],
    *,
    rejected_record_count: int = 1,
    logger: logging.Logger | None = None,
) -> None:
    """Emit one WARNING summary; never accept a raw record or identifier.

    Default count describes one rejected record, regardless of error count.
    Callers combining errors across records must supply their actual count.
    Empty errors are a no-op, not a validation failure.
    """
    target = _logger(logger)
    if not isinstance(source, str) or source not in SOURCES:
        raise ValueError("source must be kafka, user_api, movie_api or recommendation_api")
    count = _count(rejected_record_count, "rejected_record_count", minimum=1)
    issues = _issues(errors)
    if not issues:
        return
    payload = {
        "source": source, "issue_type": "validation_failure",
        "validation_failure_count": len(issues), "rejected_record_count": count,
        "errors": issues,
    }
    target.warning(json.dumps(payload, allow_nan=False, sort_keys=True))


def log_drift_result(
    result: Mapping[str, object], *, logger: logging.Logger | None = None,
) -> None:
    """Emit one existing drift result, retaining metrics but excluding raw errors.

    Unknown/malformed inputs raise rather than being silently dropped. Only
    detector metrics are supported; unknown missingness field names are redacted.
    """
    target = _logger(logger)
    if not isinstance(result, Mapping):
        raise TypeError("result must be a drift result mapping")
    metric = result["metric"]
    if not isinstance(metric, str):
        raise TypeError("metric must be a string")
    if metric.startswith("missing_value_rate:"):
        field = metric.removeprefix("missing_value_rate:")
        metric = "missing_value_rate:" + (field if field in FIELDS else "<redacted>")
    elif metric not in ("rating_distribution_tvd", "event_type_distribution_tvd"):
        raise ValueError("Unsupported drift metric")
    status = result["status"]
    if not isinstance(status, str) or status not in DRIFT_LEVELS:
        raise ValueError("Unsupported drift status")
    score = result["drift_score"]
    if status in ("drift_detected", "no_drift"):
        score = _number(score, "drift_score")
    elif score is not None:
        raise ValueError("Unevaluated drift results must have a null score")
    issues = _issues(result["errors"])
    payload = {
        "source": "drift", "issue_type": "drift_result", "metric": metric,
        "baseline_size": _count(result["baseline_size"], "baseline_size"),
        "recent_size": _count(result["recent_size"], "recent_size"),
        "drift_score": score, "threshold": _number(result["threshold"], "threshold"),
        "min_samples": _count(result["min_samples"], "min_samples", minimum=1),
        "status": status, "validation_failure_count": len(issues), "errors": issues,
    }
    target.log(DRIFT_LEVELS[status], json.dumps(payload, allow_nan=False, sort_keys=True))
