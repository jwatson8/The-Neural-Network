"""Freeze database inputs in one transaction; never query production during evaluation."""
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlmodel import Session, select

from src.database.models import (StreamEvent, User, Movie, RecommendationServe,
                                 LLMUsage, ColdStartProfile, LicensingCost, MonitoringRun)
from src.database.schemas import StreamEventCreate


def utc(value):
    value = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)  # Existing database schema defines naive times as UTC.
    return value.astimezone(UTC)


def window(day):
    zone = ZoneInfo('America/New_York')
    start = datetime.combine(day, time(), zone)
    end = datetime.combine(day + timedelta(days=1), time(), zone)
    return start.astimezone(UTC), end.astimezone(UTC)


def capture(engine, start, end, limit=500000):
    """One consistent PostgreSQL snapshot, including a 60-minute attribution tail.

    Bound every table read; fail rather than silently training on truncated data.
    SQLite is supported for offline integration tests.
    """
    with Session(engine) as session:
        if engine.dialect.name == 'postgresql':
            session.exec(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
        tables = {
            'events': select(StreamEvent).where(StreamEvent.event_time < end + timedelta(hours=1)),
            'users': select(User), 'movies': select(Movie),
            'serves': select(RecommendationServe).where(
                RecommendationServe.served_at >= start, RecommendationServe.served_at < end),
            'usage': select(LLMUsage).where(LLMUsage.occurred_at >= start, LLMUsage.occurred_at < end),
            'profiles': select(ColdStartProfile).where(ColdStartProfile.updated_at < start),
            'licensing_costs': select(LicensingCost).where(
                LicensingCost.charged_at >= start, LicensingCost.charged_at < end),
            'monitoring_runs': select(MonitoringRun).where(
                MonitoringRun.window_start >= start, MonitoringRun.window_end <= end),
        }
        snapshot = {}
        for name, query in tables.items():
            rows = session.exec(query.limit(limit + 1)).all()
            if len(rows) > limit:
                raise ValueError(f'{name} exceeds snapshot row limit; increase resources/limit explicitly')
            snapshot[name] = [row.model_dump(mode='json') for row in rows]
        snapshot['captured_at'] = datetime.now(UTC).isoformat()
        return snapshot


def normalize_events(rows):
    """Validate and deduplicate interactions; normalize chronology before SVD fitting."""
    output = {}
    fields = ('user_id', 'event_type', 'movie_id', 'rating', 'minute_index')
    for row in rows:
        payload = {key: row[key] for key in fields if key in row}
        payload['event_time'] = row.get('event_time', row.get('timestamp'))
        event = StreamEventCreate.model_validate(payload)
        normalized = dict(timestamp=utc(event.event_time).isoformat(), user_id=event.user_id,
                          movie_id=event.movie_id or '', event_type=event.event_type,
                          rating=event.rating, minute_index=event.minute_index)
        key = tuple(normalized.values())
        output[key] = normalized
    return sorted(output.values(), key=lambda row: (row['timestamp'], row['user_id'], row['movie_id'], row['event_type']))
