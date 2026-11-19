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


def capture(engine, start, end, limit=500000, event_limit=100000, telemetry_limit=20000):
    """One consistent PostgreSQL snapshot, including a 60-minute attribution tail.

    Keep the fixed original dataset plus seven days of recent history and one
    held-out day. Each partition retains its newest event_limit events in a
    deterministic order. Record limits explicitly, including whether each was hit.
    User/profile metadata is restricted to selected interactions, not all accounts.
    SQLite is supported for offline integration tests.
    """
    with Session(engine) as session:
        if engine.dialect.name == 'postgresql':
            session.exec(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
        if event_limit < 1 or telemetry_limit < 1:
            raise ValueError('Snapshot limits must be positive')
        history_start = start - timedelta(days=7)
        event_queries = [select(StreamEvent).where(StreamEvent.event_time >= a, StreamEvent.event_time < b)
                         .order_by(StreamEvent.event_time.desc(), StreamEvent.event_id.desc())
                         for a, b in ((history_start, start), (start, end + timedelta(hours=1)))]
        snapshot = {'events': []}
        sampling = dict(history_start=history_start.isoformat(), event_limit_per_partition=event_limit,
                        telemetry_limit=telemetry_limit, selection='newest first by timestamp then primary key',
                        event_partitions={}, telemetry={})
        for name, query in zip(('history', 'heldout_and_tail'), event_queries):
            rows = session.exec(query.limit(min(event_limit, limit) + 1)).all()
            if len(rows) > limit:
                raise ValueError('events exceeds configured hard limit')
            sampling['event_partitions'][name] = dict(retained=min(len(rows), event_limit), capped=len(rows) > event_limit)
            snapshot['events'].extend(row.model_dump(mode='json') for row in rows[:event_limit])
        user_ids = sorted({row['user_id'] for row in snapshot['events']})
        snapshot['users'], snapshot['profiles'] = [], []
        # Bound SQL parameter counts as well as Python memory.
        for offset in range(0, len(user_ids), 1000):
            batch = user_ids[offset:offset + 1000]
            snapshot['users'].extend(row.model_dump(mode='json') for row in session.exec(
                select(User).where(User.user_id.in_(batch))).all())
            snapshot['profiles'].extend(row.model_dump(mode='json') for row in session.exec(
                select(ColdStartProfile).where(ColdStartProfile.user_id.in_(batch), ColdStartProfile.updated_at < start)).all())
        tables = {
            'movies': select(Movie).order_by(Movie.movie_id),
            'serves': select(RecommendationServe).where(
                RecommendationServe.served_at >= start, RecommendationServe.served_at < end)
                .order_by(RecommendationServe.served_at.desc(), RecommendationServe.request_id.desc()),
            'usage': select(LLMUsage).where(LLMUsage.occurred_at >= start, LLMUsage.occurred_at < end)
                .order_by(LLMUsage.occurred_at.desc(), LLMUsage.request_id.desc()),
            'licensing_costs': select(LicensingCost).where(
                LicensingCost.charged_at >= start, LicensingCost.charged_at < end)
                .order_by(LicensingCost.charged_at.desc(), LicensingCost.licensing_cost_id.desc()),
            'monitoring_runs': select(MonitoringRun).where(
                MonitoringRun.window_start >= start, MonitoringRun.window_end <= end)
                .order_by(MonitoringRun.window_start, MonitoringRun.run_id),
        }
        for name, query in tables.items():
            cap = limit if name == 'movies' else min(telemetry_limit, limit)
            rows = session.exec(query.limit(cap + 1)).all()
            if len(rows) > limit:
                raise ValueError(f'{name} exceeds snapshot row limit; increase resources/limit explicitly')
            sampling['telemetry'][name] = dict(retained=min(len(rows), cap), capped=len(rows) > cap)
            snapshot[name] = [row.model_dump(mode='json') for row in rows[:cap]]
        snapshot['sampling'] = sampling
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
