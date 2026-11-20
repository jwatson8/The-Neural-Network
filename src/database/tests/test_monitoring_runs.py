"""Tests for completed monitoring-run persistence."""

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import UUID

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from src.database.interactions import monitoring_runs
from src.database.models import MonitoringModelAccuracy, MonitoringRouteLatency
from src.monitoring.schemas import (
    DriftSummary,
    LatencySummary,
    ModelAccuracySummary,
    MonitorResult,
    RouteLatencySummary,
)


class MonitoringRunTests(unittest.TestCase):
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
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def result(self, *, generated_at: datetime) -> MonitorResult:
        return MonitorResult(
            generated_at=generated_at,
            window={
                "start": generated_at - timedelta(hours=24),
                "end": generated_at,
            },
            licensing={
                "charge_count": 4,
                "priced_charge_count": 3,
                "total_license_cost": "1.25",
            },
            llm={
                "request_count": 2,
                "priced_request_count": 1,
                "input_tokens": 100,
                "output_tokens": 20,
                "estimated_cost_usd": "0.004",
            },
            quality={
                "evaluated_rating_count": 40,
                "attributed_rating_count": 20,
                "positive_rating_count": 15,
                "accuracy": 0.75,
                "models": [
                    {
                        "model_route": "collaborative_svd",
                        "model_version": "m1",
                        "attributed_rating_count": 4,
                        "positive_rating_count": 3,
                        "accuracy": 0.75,
                    }
                ],
            },
        )

    def test_completed_runs_round_trip_and_search_newest_first(self):
        first = monitoring_runs.create(
            self.result(generated_at=self.now), run_id=UUID(int=1)
        )
        second = monitoring_runs.create(
            self.result(generated_at=self.now + timedelta(hours=1)),
            run_id=UUID(int=2),
        )

        stored = monitoring_runs.get(first.run_id)
        page = monitoring_runs.search()

        self.assertEqual(
            stored.window_start.replace(tzinfo=UTC),
            self.now - timedelta(hours=24),
        )
        self.assertEqual(stored.licensing_charge_count, 4)
        self.assertEqual(stored.llm_input_tokens, 100)
        self.assertEqual(stored.recommendation_accuracy, 0.75)
        self.assertEqual(
            [record.run_id for record in page.records],
            [second.run_id, first.run_id],
        )

    def test_bucket_upserts_are_idempotent_and_refresh_values(self):
        result = self.result(generated_at=self.now)
        result.window.start = self.now - timedelta(minutes=5)
        result.window.end = self.now

        first = monitoring_runs.upsert_many([result])[0]
        result.generated_at = self.now + timedelta(minutes=5)
        result.licensing.total_license_cost = 2
        second = monitoring_runs.upsert_many([result])[0]

        self.assertEqual(first.run_id, second.run_id)
        self.assertEqual(len(monitoring_runs.list_records()), 1)
        self.assertEqual(second.total_license_cost, 2)
        with Session(self.engine) as session:
            model_accuracy = session.exec(select(MonitoringModelAccuracy)).all()
        self.assertEqual(len(model_accuracy), 1)
        self.assertEqual(model_accuracy[0].model_route, "collaborative_svd")
        self.assertEqual(model_accuracy[0].recommendation_accuracy, 0.75)
        self.assertEqual(
            monitoring_runs.list_windows(
                start=self.now - timedelta(hours=1), end=self.now
            ),
            {(self.now - timedelta(minutes=5), self.now)},
        )

    def test_bucket_upsert_replaces_model_accuracy_children(self):
        result = self.result(generated_at=self.now)
        result.window.start = self.now - timedelta(minutes=5)
        result.window.end = self.now
        monitoring_runs.upsert_many([result])

        result.quality.models = [
            ModelAccuracySummary(
                model_route="popularity_fallback",
                model_version="m2",
                attributed_rating_count=0,
                positive_rating_count=0,
                accuracy=None,
            )
        ]
        monitoring_runs.upsert_many([result])

        with Session(self.engine) as session:
            rows = session.exec(select(MonitoringModelAccuracy)).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].model_route, "popularity_fallback")
        self.assertEqual(rows[0].model_version, "m2")
        self.assertIsNone(rows[0].recommendation_accuracy)

    def test_bucket_upsert_replaces_route_latency_children(self):
        result = self.result(generated_at=self.now)
        result.window.start = self.now - timedelta(minutes=5)
        result.window.end = self.now
        route = RouteLatencySummary(
            model_route="control",
            request_count=1,
            average_ms=600,
            p50_ms=600,
            p95_ms=600,
            p99_ms=600,
            maximum_ms=600,
        )
        result.latency = LatencySummary(
            request_count=1,
            average_ms=600,
            p50_ms=600,
            p95_ms=600,
            p99_ms=600,
            maximum_ms=600,
            routes=[route],
        )
        monitoring_runs.upsert_many([result])

        fallback = route.model_copy(
            update={"model_route": "fallback", "request_count_over_600ms": 1}
        )
        result.latency = result.latency.model_copy(update={"routes": [fallback]})
        monitoring_runs.upsert_many([result])

        with Session(self.engine) as session:
            rows = session.exec(select(MonitoringRouteLatency)).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].model_route, "fallback")
        self.assertEqual(rows[0].request_count_over_600ms, 1)

    def test_reconciliation_replaces_drift_values_and_preserves_other_metrics(self):
        result = self.result(generated_at=self.now)
        result.window.start = self.now - timedelta(minutes=5)
        result.window.end = self.now
        result.drift = DriftSummary(
            rating_drift_tvd=0.2,
            event_type_drift_tvd=0.1,
            rating_drift_sample_count=50,
            event_type_baseline_counts={
                "watch": 40, "rating": 10, "account_created": 0
            },
            event_type_recent_counts={
                "watch": 35, "rating": 15, "account_created": 0
            },
        )
        monitoring_runs.upsert_many([result])

        result.generated_at += timedelta(minutes=5)
        result.drift = DriftSummary(
            rating_drift_tvd=0.4,
            event_type_drift_tvd=0.3,
            rating_drift_sample_count=60,
            event_type_baseline_counts={
                "watch": 30, "rating": 20, "account_created": 1
            },
            event_type_recent_counts={
                "watch": 25, "rating": 25, "account_created": 2
            },
        )
        reconciled = monitoring_runs.upsert_many([result])[0]

        self.assertEqual(reconciled.rating_drift_tvd, 0.4)
        self.assertEqual(reconciled.event_type_drift_tvd, 0.3)
        self.assertEqual(reconciled.rating_drift_sample_count, 60)
        self.assertEqual(reconciled.total_license_cost, 1.25)
        self.assertEqual(
            reconciled.event_type_baseline_counts,
            {"watch": 30, "rating": 20, "account_created": 1},
        )
        self.assertEqual(
            reconciled.event_type_recent_counts,
            {"watch": 25, "rating": 25, "account_created": 2},
        )


if __name__ == "__main__":
    unittest.main()
