"""UTC bucket helpers shared by monitoring calculations and scheduling."""

from datetime import UTC, datetime, timedelta

from src.monitoring.schemas import EvaluationWindow


def utc_datetime(value: datetime) -> datetime:
    """Return an aware UTC datetime, treating SQLite's naive values as UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def floor_to_bucket(value: datetime, bucket_minutes: int) -> datetime:
    """Floor a datetime to a UTC bucket boundary."""
    value = utc_datetime(value)
    elapsed_minutes = value.hour * 60 + value.minute
    bucket_minute = elapsed_minutes - elapsed_minutes % bucket_minutes
    return value.replace(
        hour=bucket_minute // 60,
        minute=bucket_minute % 60,
        second=0,
        microsecond=0,
    )


def windows_between(
    start: datetime,
    end: datetime,
    bucket_minutes: int,
) -> list[EvaluationWindow]:
    """Build consecutive half-open windows between aligned UTC boundaries."""
    bucket_size = timedelta(minutes=bucket_minutes)
    current = utc_datetime(start)
    end = utc_datetime(end)
    windows = []
    while current < end:
        windows.append(EvaluationWindow(start=current, end=current + bucket_size))
        current += bucket_size
    return windows
