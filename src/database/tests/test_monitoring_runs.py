"""Tests for completed monitoring-run persistence."""

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import UUID

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from src.database.interactions import monitoring_runs
from src.database.models import MonitoringModelAccuracy
from src.monitoring.schemas import ModelAccuracySummary, MonitorResult


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


if __name__ == "__main__":
    unittest.main()
