"""Behavior tests for rolling monitoring drift calculations."""

import unittest
from collections import Counter
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from src.database.interactions import stream_event_counts
from src.database.models import Movie, StreamEvent, User
from src.monitoring.calculations.data_drift import calculate_data_drift_buckets
from src.monitoring.schemas import EvaluationWindow, MonitoringConfig


class FakeAggregate:
    """Bucket in-memory events the way the database aggregate does."""

    def __init__(self, events):
        self.events = events
        self.calls = []

    def __call__(self, *, start, end, bucket_minutes):
        self.calls.append(
            {"start": start, "end": end, "bucket_minutes": bucket_minutes}
        )
        bucket_seconds = bucket_minutes * 60
        counts = Counter()
        for event in self.events:
            if not start <= event["event_time"] < end:
                continue
            epoch = int(event["event_time"].timestamp())
            bucket_start = datetime.fromtimestamp(
                epoch - epoch % bucket_seconds, tz=UTC
            )
            counts[(bucket_start, event["event_type"], event["rating"])] += 1
        return [
            {
                "bucket_start": bucket_start,
                "event_type": event_type,
                "rating": rating,
                "event_count": count,
            }
            for (bucket_start, event_type, rating), count in counts.items()
        ]


class DataDriftCalculationTests(unittest.TestCase):
    def setUp(self):
        self.end = datetime(2026, 1, 1, 12, tzinfo=UTC)
        self.config = MonitoringConfig(
            backfill_hours=1,
            drift_min_samples=1,
        )

    def window(self, end=None):
        end = end or self.end
        return EvaluationWindow(start=end - timedelta(minutes=5), end=end)

    @staticmethod
    def event(event_time, event_type, rating=None):
        return {
            "event_time": event_time,
            "event_type": event_type,
            "rating": rating,
        }

    def test_half_open_boundaries_and_complete_rating_shift(self):
        baseline_start = self.end - timedelta(hours=2)
        recent_start = self.end - timedelta(hours=1)
        aggregate = FakeAggregate(
            [
                self.event(baseline_start, "rating", 1),
                self.event(recent_start, "rating", 10),
                self.event(self.end, "watch"),
            ]
        )

        summary = calculate_data_drift_buckets(
            [self.window()], self.config, aggregate=aggregate
        )[self.end - timedelta(minutes=5)]

        self.assertEqual(summary.rating_drift_tvd, 1)
        self.assertEqual(summary.event_type_drift_tvd, 0)
        self.assertEqual(summary.rating_drift_sample_count, 1)
        self.assertEqual(
            summary.event_type_baseline_counts,
            {"watch": 0, "rating": 1, "account_created": 0},
        )
        self.assertEqual(
            summary.event_type_recent_counts,
            summary.event_type_baseline_counts,
        )

    def test_empty_windows_have_null_scores_and_complete_zero_maps(self):
        summary = calculate_data_drift_buckets(
            [self.window()], self.config, aggregate=FakeAggregate([])
        )[self.end - timedelta(minutes=5)]

        self.assertIsNone(summary.rating_drift_tvd)
        self.assertIsNone(summary.event_type_drift_tvd)
        self.assertEqual(summary.rating_drift_sample_count, 0)
        self.assertEqual(
            summary.event_type_recent_counts,
            {"watch": 0, "rating": 0, "account_created": 0},
        )

    def test_event_type_shift_from_watch_to_rating_is_detected(self):
        aggregate = FakeAggregate(
            [
                self.event(self.end - timedelta(minutes=90), "watch"),
                self.event(self.end - timedelta(minutes=30), "rating", 5),
            ]
        )

        summary = calculate_data_drift_buckets(
            [self.window()], self.config, aggregate=aggregate
        )[self.end - timedelta(minutes=5)]

        self.assertEqual(summary.event_type_drift_tvd, 1)
        self.assertIsNone(summary.rating_drift_tvd)

    def test_partial_distribution_shift_and_min_samples(self):
        baseline = self.end - timedelta(minutes=90)
        recent = self.end - timedelta(minutes=30)
        events = [
            *(self.event(baseline, "rating", 4) for _ in range(3)),
            self.event(baseline, "rating", 8),
            *(self.event(recent, "rating", 4) for _ in range(1)),
            *(self.event(recent, "rating", 8) for _ in range(3)),
        ]

        summary = calculate_data_drift_buckets(
            [self.window()], self.config, aggregate=FakeAggregate(events)
        )[self.end - timedelta(minutes=5)]
        self.assertAlmostEqual(summary.rating_drift_tvd, 0.5)

        strict = MonitoringConfig(backfill_hours=1, drift_min_samples=5)
        summary = calculate_data_drift_buckets(
            [self.window()], strict, aggregate=FakeAggregate(events)
        )[self.end - timedelta(minutes=5)]
        self.assertIsNone(summary.rating_drift_tvd)
        self.assertEqual(summary.rating_drift_sample_count, 4)

    def test_multiple_buckets_share_one_read_and_keep_distinct_snapshots(self):
        later_end = self.end + timedelta(minutes=5)
        aggregate = FakeAggregate(
            [
                self.event(self.end - timedelta(minutes=60), "watch"),
                self.event(self.end, "rating", 7),
            ]
        )

        summaries = calculate_data_drift_buckets(
            [self.window(), self.window(later_end)], self.config, aggregate=aggregate
        )

        self.assertEqual(
            aggregate.calls,
            [
                {
                    "start": self.end - timedelta(hours=2),
                    "end": later_end,
                    "bucket_minutes": 5,
                }
            ],
        )
        first = summaries[self.end - timedelta(minutes=5)]
        second = summaries[self.end]
        self.assertEqual(first.event_type_recent_counts["rating"], 0)
        self.assertEqual(second.event_type_recent_counts["rating"], 1)


class StreamEventCountTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        engine_patch = patch(
            "src.database.database.get_engine", return_value=self.engine
        )
        engine_patch.start()
        self.addCleanup(engine_patch.stop)
        self.start = datetime(2026, 1, 1, tzinfo=UTC)
        with Session(self.engine) as session:
            session.add(User(user_id="u"))
            session.add(Movie(movie_id="m", title="M"))
            session.commit()

    def add(self, seconds, event_type, rating=None):
        with Session(self.engine) as session:
            session.add(
                StreamEvent(
                    user_id="u",
                    event_type=event_type,
                    movie_id=None if event_type == "account_created" else "m",
                    event_time=self.start + timedelta(seconds=seconds),
                    rating=rating,
                )
            )
            session.commit()

    def test_counts_group_by_bucket_type_and_rating_with_half_open_range(self):
        for seconds, event_type, rating in (
            (0, "watch", None),
            (10, "watch", None),
            (20, "rating", 7.0),
            (30, "rating", 7.0),
            (40, "rating", 3.0),
            (300, "account_created", None),
            (600, "watch", None),
        ):
            self.add(seconds, event_type, rating)

        rows = stream_event_counts.aggregate(
            start=self.start,
            end=self.start + timedelta(minutes=10),
            bucket_minutes=5,
        )

        first = self.start
        second = self.start + timedelta(minutes=5)
        self.assertEqual(
            {
                (row["bucket_start"], row["event_type"], row["rating"]): row[
                    "event_count"
                ]
                for row in rows
            },
            {
                (first, "watch", None): 2,
                (first, "rating", 7.0): 2,
                (first, "rating", 3.0): 1,
                (second, "account_created", None): 1,
            },
        )

    def test_postgres_groups_in_database(self):
        session = Mock()
        session.execute.return_value.mappings.return_value = []

        rows = stream_event_counts._postgres_aggregate(
            session,
            start=self.start,
            end=self.start + timedelta(minutes=5),
            bucket_minutes=5,
        )

        statement = str(session.execute.call_args.args[0])
        self.assertEqual(rows, [])
        self.assertIn("date_bin", statement)
        self.assertIn("GROUP BY 1, 2, 3", statement)
        self.assertIn("COUNT(*)", statement)


if __name__ == "__main__":
    unittest.main()
