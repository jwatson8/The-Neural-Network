"""Pure snapshot comparisons. No database, Kafka, clock, or network access."""

from collections import Counter
from collections.abc import Iterable, Mapping
from decimal import Decimal
from fractions import Fraction
from math import isfinite
from typing import Literal, TypedDict

from src.database.schemas import StreamEventCreate

Record = Mapping[str, object] | StreamEventCreate
Status = Literal["drift_detected", "no_drift", "insufficient_data", "invalid_data"]

DEFAULT_TVD_THRESHOLD = 0.20
DEFAULT_MISSING_THRESHOLD = 0.10
DEFAULT_MIN_SAMPLES = 100
EVENT_TYPES = ("watch", "rating", "account_created")


class DriftResult(TypedDict):
    metric: str
    baseline_size: int
    recent_size: int
    drift_score: float | None
    threshold: float
    min_samples: int
    status: Status
    errors: list[dict]


def _configuration(threshold: float, min_samples: int) -> None:
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not 0 <= threshold <= 1
        or not isfinite(threshold)
    ):
        raise ValueError("threshold must be a finite number between 0 and 1")
    if isinstance(min_samples, bool) or not isinstance(min_samples, int) or min_samples < 1:
        raise ValueError("min_samples must be a positive integer")


def _error(location: tuple, message: str) -> dict:
    return {"loc": location, "type": "value_error", "msg": message}


def _snapshot(raw: Iterable[Record], name: str) -> tuple[list[Mapping], list[dict]]:
    if isinstance(raw, (str, bytes, Mapping)) or not isinstance(raw, Iterable):
        return [], [_error((name,), "Snapshot must be an iterable of records")]
    records = []
    errors = []
    for index, record in enumerate(raw):
        if isinstance(record, StreamEventCreate):
            records.append(record.model_dump())
        elif isinstance(record, Mapping):
            records.append(record)
        else:
            # Retain the row position and denominator; never silently drop it.
            records.append({})
            errors.append(_error((name, index), "Record must be a mapping or StreamEventCreate"))
    return records, errors


def _result(
    metric: str, baseline_size: int, recent_size: int, score: float,
    threshold: float, min_samples: int, errors: list[dict],
) -> DriftResult:
    status: Status
    if errors:
        status = "invalid_data"
    elif min(baseline_size, recent_size) < min_samples:
        status = "insufficient_data"
    else:
        status = "drift_detected" if score >= threshold else "no_drift"
    return {
        "metric": metric, "baseline_size": baseline_size, "recent_size": recent_size,
        "drift_score": score if status in ("drift_detected", "no_drift") else None,
        "threshold": threshold, "min_samples": min_samples,
        "status": status, "errors": errors,
    }


def _categorical_drift(
    baseline: Iterable[Record], recent: Iterable[Record], *, field: str,
    threshold: float, min_samples: int,
) -> DriftResult:
    _configuration(threshold, min_samples)
    counts = []
    sizes = []
    errors = []
    for name, raw in (("baseline", baseline), ("recent", recent)):
        rows, row_errors = _snapshot(raw, name)
        errors.extend(row_errors)
        sizes.append(len(rows))
        counter = Counter()
        for index, row in enumerate(rows):
            value = row.get(field)
            if field == "rating":
                valid = (
                    not isinstance(value, bool)
                    and isinstance(value, (int, float, Decimal))
                    and (not isinstance(value, Decimal) or value.is_finite())
                    and 1 <= value <= 10
                )
                if valid:
                    value = float(value)
                    valid = isfinite(value)
            else:
                valid = isinstance(value, str) and value in EVENT_TYPES
            if not valid:
                errors.append(_error((name, index, field), f"Missing or invalid {field} measurement"))
            else:
                counter[value] += 1
        counts.append(counter)
    categories = sorted(counts[0].keys() | counts[1].keys())
    score = float(sum(
        abs(Fraction(counts[0][key], sizes[0]) - Fraction(counts[1][key], sizes[1]))
        for key in categories
    ) / 2) if all(sizes) else 0.0
    return _result(f"{field}_distribution_tvd", *sizes, score, threshold, min_samples, errors)


def detect_rating_drift(
    baseline: Iterable[Record], recent: Iterable[Record], *,
    threshold: float = DEFAULT_TVD_THRESHOLD,
    min_samples: int = DEFAULT_MIN_SAMPLES,
) -> DriftResult:
    """Compare rating-only records. Finite fractional ratings in [1, 10] are valid.

    Each exact numeric rating is a category; no rounding or hidden binning.
    Numeric strings, booleans, absent/null and non-finite ratings are invalid.
    """
    return _categorical_drift(
        baseline, recent, field="rating", threshold=threshold, min_samples=min_samples
    )


def detect_event_type_drift(
    baseline: Iterable[Record], recent: Iterable[Record], *,
    threshold: float = DEFAULT_TVD_THRESHOLD,
    min_samples: int = DEFAULT_MIN_SAMPLES,
) -> DriftResult:
    """Compare all-event snapshots over the three schema-defined event types."""
    return _categorical_drift(
        baseline, recent, field="event_type", threshold=threshold, min_samples=min_samples
    )


def detect_missing_value_drift(
    baseline: Iterable[Record], recent: Iterable[Record], fields: Iterable[str], *,
    threshold: float = DEFAULT_MISSING_THRESHOLD,
    min_samples: int = DEFAULT_MIN_SAMPLES,
) -> list[DriftResult]:
    """Return one absolute missing-rate change per selected field.

    Missing means absent key or None, not empty text, zero, false, or NaN.
    Non-finite numeric values are invalid measurements, not missingness.
    Fields may be absent from every row: that is a measured 100% missing rate.
    """
    _configuration(threshold, min_samples)
    if isinstance(fields, (str, bytes)) or not isinstance(fields, Iterable):
        raise ValueError("fields must be a nonempty iterable of unique field names")
    selected = list(fields)
    if (
        not selected
        or any(not isinstance(field, str) or not field.strip() for field in selected)
        or len(set(selected)) != len(selected)
    ):
        raise ValueError("fields must be a nonempty iterable of unique field names")
    old, old_errors = _snapshot(baseline, "baseline")
    new, new_errors = _snapshot(recent, "recent")
    results = []
    for field in selected:
        rates = []
        errors = [*old_errors, *new_errors]
        for name, rows in (("baseline", old), ("recent", new)):
            missing = 0
            for index, row in enumerate(rows):
                value = row.get(field)
                if value is None:
                    missing += 1
                elif (
                    isinstance(value, float) and not isfinite(value)
                ) or (isinstance(value, Decimal) and not value.is_finite()):
                    errors.append(_error((name, index, field), "Non-finite value is invalid, not null"))
            rates.append(Fraction(missing, len(rows)) if rows else Fraction(0))
        results.append(_result(
            f"missing_value_rate:{field}", len(old), len(new), float(abs(rates[0] - rates[1])),
            threshold, min_samples, errors,
        ))
    return results
