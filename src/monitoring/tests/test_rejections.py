"""Tests for Kafka rejection-count monitoring buckets."""

import json
import re
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID
from unittest.mock import Mock, patch

from pydantic import ValidationError
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from src.database.interactions import (
    course_requests,
    kafka_rejection_counts,
    monitoring_runs,
    rejected_requests,
    users,
)
from src.database.migrations import add_monitoring_schema
from src.database.models import MonitoringRun, StreamEvent
from src.database.schemas import REJECTION_REASONS
from src.monitoring.calculations import calculate_rejection_buckets
from src.monitoring.schemas import (
    EvaluationWindow,
    MonitoringConfig,
    MonitorResult,
    RejectionSummary,
)

START = datetime(2026, 1, 1, tzinfo=UTC)
REJECTION_COLUMNS = [f"rejected_{reason}_count" for reason in REJECTION_REASONS]


def window(offset_minutes: int) -> EvaluationWindow:
    start = START + timedelta(minutes=offset_minutes)
    return EvaluationWindow(start=start, end=start + timedelta(minutes=5))


class RejectionSummaryTests(unittest.TestCase):
    def test_has_one_count_per_reason_and_sums_a_total(self):
        self.assertEqual(
            {name for name in RejectionSummary.model_fields if name != "kafka_record_count"},
            {f"{reason}_count" for reason in REJECTION_REASONS},
        )
        summary = RejectionSummary(
            kafka_record_count=10, malformed_line_count=2, unknown_movie_count=3
        )
        self.assertEqual(summary.total_count, 5)
        self.assertEqual(RejectionSummary().total_count, 0)

    def test_rejections_cannot_exceed_consumed_records(self):
        with self.assertRaisesRegex(ValidationError, "cannot exceed"):
            RejectionSummary(kafka_record_count=1, invalid_field_count=2)


class RejectionCalculationTests(unittest.TestCase):
    def test_rows_map_to_windows_and_empty_windows_stay_zero(self):
        aggregate = Mock(return_value=[
            {"bucket_start": START.replace(tzinfo=None), "kafka_record_count": 8,
             **{f"{reason}_count": 0 for reason in REJECTION_REASONS},
             "invalid_user_id_count": 2},
            # Outside the requested windows: ignored.
            {"bucket_start": START + timedelta(hours=1), "kafka_record_count": 1,
             **{f"{reason}_count": 0 for reason in REJECTION_REASONS}},
        ])
        windows = [window(0), window(5)]

        summaries = calculate_rejection_buckets(
            windows, MonitoringConfig(), aggregate=aggregate
        )

        aggregate.assert_called_once_with(
            start=START, end=START + timedelta(minutes=10), bucket_minutes=5
        )
        self.assertEqual(summaries[START].kafka_record_count, 8)
        self.assertEqual(summaries[START].invalid_user_id_count, 2)
        self.assertEqual(summaries[START].total_count, 2)
        self.assertEqual(summaries[windows[1].start], RejectionSummary())
        self.assertEqual(calculate_rejection_buckets([], MonitoringConfig()), {})


class KafkaRejectionCountTests(unittest.TestCase):
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

    def test_counts_all_consumed_records_and_rejections_by_reason(self):
        at = START + timedelta(minutes=1)
        later = START + timedelta(minutes=7)
        users.create_missing_many([{"user_id": "1"}])
        with Session(self.engine) as session:
            session.add_all([
                StreamEvent(user_id="1", event_type="account_created",
                            event_time=stored_at, stored_at=stored_at)
                for stored_at in (at, later)
            ])
            session.commit()
        course_requests.create_many([
            {"topic": "t", "partition_id": 0, "offset_id": 1, "raw_line": "x",
             "received_at": at},
        ])
        rejected_requests.create_many([
            {"topic": "t", "partition_id": 0, "offset_id": offset, "raw_line": "bad",
             "reason": reason, "received_at": received_at}
            for offset, reason, received_at in (
                (2, "malformed_line", at),
                (3, "malformed_line", at),
                (4, "invalid_user_id", later),
                # Outside the queried range.
                (5, "malformed_line", START + timedelta(minutes=30)),
            )
        ])

        rows = kafka_rejection_counts.aggregate(
            start=START, end=START + timedelta(minutes=10), bucket_minutes=5
        )

        self.assertEqual([row["bucket_start"] for row in rows],
                         [START, START + timedelta(minutes=5)])
        first, second = rows
        self.assertEqual(first["kafka_record_count"], 4)
        self.assertEqual(first["malformed_line_count"], 2)
        self.assertEqual(second["kafka_record_count"], 2)
        self.assertEqual(second["invalid_user_id_count"], 1)
        self.assertEqual(second["malformed_line_count"], 0)

    def test_postgres_query_counts_every_reason(self):
        sql = kafka_rejection_counts._POSTGRES_SQL
        for reason in REJECTION_REASONS:
            self.assertIn(f"WHERE reason = '{reason}')::bigint AS {reason}_count", sql)
        for table in ("stream_events", "course_requests", "rejected_requests"):
            self.assertIn(f"FROM {table}", sql)


class RejectionPersistenceTests(unittest.TestCase):
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

    def result(self, rejections: RejectionSummary) -> MonitorResult:
        return MonitorResult(
            generated_at=START + timedelta(minutes=5),
            window=window(0),
            licensing={"charge_count": 0, "priced_charge_count": 0,
                       "total_license_cost": 0},
            llm={"request_count": 0, "priced_request_count": 0, "input_tokens": 0,
                 "output_tokens": 0, "estimated_cost_usd": 0},
            quality={"evaluated_rating_count": 0, "attributed_rating_count": 0,
                     "positive_rating_count": 0},
            rejections=rejections,
        )

    def test_counts_and_total_are_stored_and_refreshed_by_upsert(self):
        first = monitoring_runs.upsert_many([self.result(RejectionSummary(
            kafka_record_count=20, malformed_line_count=3, unrecognized_event_count=1,
        ))])[0]
        self.assertEqual(first.kafka_record_count, 20)
        self.assertEqual(first.rejected_malformed_line_count, 3)
        self.assertEqual(first.rejected_unrecognized_event_count, 1)
        self.assertEqual(first.rejected_total_count, 4)

        second = monitoring_runs.upsert_many([self.result(RejectionSummary(
            kafka_record_count=25, malformed_line_count=5,
        ))])[0]
        self.assertEqual(second.run_id, first.run_id)
        self.assertEqual(second.rejected_total_count, 5)
        self.assertEqual(second.rejected_unrecognized_event_count, 0)

    def test_database_rejects_inconsistent_totals(self):
        values = monitoring_runs._result_values(
            self.result(RejectionSummary(kafka_record_count=2)), UUID(int=1)
        )
        for override in (
            {"rejected_total_count": 1},
            {"rejected_malformed_line_count": 3, "rejected_total_count": 3},
        ):
            with self.subTest(override=override), Session(self.engine) as session:
                with self.assertRaises(IntegrityError):
                    session.execute(
                        insert(MonitoringRun.__table__).values(**{**values, **override})
                    )

    def test_model_columns_and_constraint_match_monitoring_migration(self):
        columns = set(MonitoringRun.__table__.columns.keys())
        self.assertLessEqual(
            {"kafka_record_count", "rejected_total_count", *REJECTION_COLUMNS}, columns
        )
        sql = add_monitoring_schema.SQL_PATH.read_text()
        for column in ("kafka_record_count", "rejected_total_count", *REJECTION_COLUMNS):
            self.assertRegex(
                sql, rf"ADD COLUMN IF NOT EXISTS {column} INTEGER NOT NULL DEFAULT 0"
            )
        self.assertIn("ck_monitoring_rejection_counts", sql)
        migration_sum = re.search(
            r"AND (rejected_\w+(?: \+ rejected_\w+)*) = rejected_total_count", sql
        ).group(1)
        self.assertEqual(migration_sum.split(" + "), REJECTION_COLUMNS)


class RejectionGrafanaTests(unittest.TestCase):
    def test_alerts_use_rolling_rate_floor_and_point_at_dashboard(self):
        grafana = Path(__file__).parents[1] / "grafana"
        dashboard = json.loads((grafana / "dashboards" / "kafka-rejections.json").read_text())
        rules = (grafana / "provisioning" / "alerting" / "operational-rules.yaml").read_text()
        rate_panel = next(panel for panel in dashboard["panels"] if panel["id"] == 1)
        self.assertIn("Rolling 15-minute", rate_panel["targets"][0]["rawSql"])

        for uid, evaluator, severity in (
            ("kafka-rejection-rate-warning", "{params: [0.1, 0.2], type: within_range}", "warning"),
            ("kafka-rejection-rate-critical", "{params: [0.2], type: gt}", "critical"),
        ):
            rule = rules[rules.index(f"uid: {uid}"):]
            rule = rule[: rule.find("isPaused")]
            self.assertIn(evaluator, rule)
            self.assertIn(f"severity: {severity}", rule)
            self.assertIn("consumed >= 100", rule)
            self.assertIn("INTERVAL '15 minutes'", rule)
            self.assertIn(f"dashboardUid: {dashboard['uid']}", rule)
            self.assertIn("for: 5m", rule)


if __name__ == "__main__":
    unittest.main()
