"""Tests for successful-request latency monitoring buckets."""

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from src.database.models import RecommendationServe, User
from src.database.interactions import request_latency as latency_store
from src.monitoring.calculations import calculate_request_latency_buckets
from src.monitoring.schemas import EvaluationWindow, MonitoringConfig


class RequestLatencyCalculationTests(unittest.TestCase):
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
        self.config = MonitoringConfig(backfill_hours=1)

    def add_serve(self, offset: timedelta, duration: float, route: str = "control"):
        with Session(self.engine) as session:
            user = session.get(User, "latency-user")
            if user is None:
                session.add(User(user_id="latency-user"))
                session.flush()
            served_at = self.start + offset
            session.add(
                RecommendationServe(
                    user_id="latency-user",
                    served_at=served_at,
                    model_route=route,
                    model_version="v1",
                    recommendations=[{"movie_id": "movie"}],
                    request_received_at=served_at,
                    serve_duration_ms=duration,
                )
            )
            session.commit()

    def window(self, offset_minutes: int) -> EvaluationWindow:
        start = self.start + timedelta(minutes=offset_minutes)
        return EvaluationWindow(start=start, end=start + timedelta(minutes=5))

    def test_empty_window_returns_zero_counts_and_null_statistics(self):
        window = self.window(0)

        summary = calculate_request_latency_buckets([window], self.config)[
            window.start
        ]

        self.assertEqual(summary.request_count, 0)
        self.assertEqual(summary.request_count_over_600ms, 0)
        self.assertIsNone(summary.average_ms)
        self.assertIsNone(summary.p50_ms)
        self.assertIsNone(summary.p95_ms)
        self.assertIsNone(summary.p99_ms)
        self.assertIsNone(summary.maximum_ms)
        self.assertEqual(summary.routes, [])

    def test_postgres_percentiles_use_one_ordered_aggregate(self):
        session = Mock()
        session.execute.return_value.mappings.return_value = []

        rows = latency_store._postgres_aggregate(
            session,
            start=self.start,
            end=self.start + timedelta(minutes=5),
            bucket_minutes=5,
        )

        statement = str(session.execute.call_args.args[0])
        self.assertEqual(rows, [])
        self.assertEqual(statement.count("percentile_cont"), 1)
        self.assertIn("ARRAY[0.50, 0.95, 0.99]", statement)

    def test_threshold_percentiles_routes_and_half_open_boundaries(self):
        for seconds, duration, route in (
            (0, 100, "control"),
            (60, 600, "control"),
            (120, 600.1, "fallback"),
            (299, 1000, "fallback"),
            (300, 700, "control"),
            (600, 900, "excluded"),
        ):
            self.add_serve(timedelta(seconds=seconds), duration, route)
        first, second = self.window(0), self.window(5)

        summaries = calculate_request_latency_buckets(
            [first, second], self.config
        )

        first_summary = summaries[first.start]
        self.assertEqual(first_summary.request_count, 4)
        self.assertEqual(first_summary.request_count_over_600ms, 2)
        self.assertAlmostEqual(first_summary.average_ms, 575.025)
        self.assertAlmostEqual(first_summary.p50_ms, 600.05)
        self.assertAlmostEqual(first_summary.p95_ms, 940.015)
        self.assertAlmostEqual(first_summary.p99_ms, 988.003)
        self.assertEqual(first_summary.maximum_ms, 1000)
        self.assertEqual(
            [route.model_route for route in first_summary.routes],
            ["control", "fallback"],
        )
        self.assertEqual(first_summary.routes[0].request_count_over_600ms, 0)
        self.assertEqual(first_summary.routes[1].request_count_over_600ms, 2)

        second_summary = summaries[second.start]
        self.assertEqual(second_summary.request_count, 1)
        self.assertEqual(second_summary.request_count_over_600ms, 1)
        for value in (
            second_summary.average_ms,
            second_summary.p50_ms,
            second_summary.p95_ms,
            second_summary.p99_ms,
            second_summary.maximum_ms,
        ):
            self.assertEqual(value, 700)


if __name__ == "__main__":
    unittest.main()
