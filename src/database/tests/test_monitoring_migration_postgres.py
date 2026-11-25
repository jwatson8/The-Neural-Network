"""Opt-in PostgreSQL tests for the independently owned monitoring migration."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
import subprocess
import tempfile
import unittest
from uuid import uuid4

from sqlalchemy import URL, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from src.database.migrations.add_monitoring_schema import migrate

BIN_DIR = os.getenv("M3_TEST_POSTGRES_BIN_DIR")


@unittest.skipUnless(
    BIN_DIR,
    "set M3_TEST_POSTGRES_BIN_DIR for a disposable local PostgreSQL cluster",
)
class MonitoringMigrationPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="monitoring-pg-", dir="/tmp")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.root = Path(cls.directory.name)
        cls.data = cls.root / "data"
        cls.socket = cls.root / "socket"
        cls.socket.mkdir()
        cls.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("PG")
        }
        cls.command(
            "initdb",
            "-D",
            str(cls.data),
            "-U",
            "monitoring-test",
            "--auth=trust",
            "--no-locale",
            "--encoding=UTF8",
        )
        cls.command(
            "pg_ctl",
            "-D",
            str(cls.data),
            "-l",
            str(cls.root / "postgres.log"),
            "-o",
            f"-F -h '' -k {cls.socket}",
            "-w",
            "-t",
            "15",
            "start",
        )
        cls.addClassCleanup(
            cls.command,
            "pg_ctl",
            "-D",
            str(cls.data),
            "-m",
            "immediate",
            "-w",
            "stop",
        )
        cls.server_engine = create_engine(
            URL.create(
                "postgresql+psycopg",
                username="monitoring-test",
                database="postgres",
                query={"host": str(cls.socket)},
            )
        )
        cls.addClassCleanup(cls.server_engine.dispose)

    @classmethod
    def command(cls, *args):
        result = subprocess.run(
            [str(Path(BIN_DIR) / args[0]), *args[1:]],
            env=cls.env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        if result.returncode:
            raise RuntimeError(f"{args[0]} failed: {result.stdout}\n{result.stderr}")
        return result

    def setUp(self):
        self.schema = "monitoring_" + uuid4().hex
        with self.server_engine.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{self.schema}"')
        self.engine = create_engine(
            self.server_engine.url,
            connect_args={"options": f"-csearch_path={self.schema}"},
        )
        self.addCleanup(self.engine.dispose)
        self.now = datetime(2026, 11, 19, tzinfo=UTC)

    def create_legacy_monitoring_table(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                """CREATE TABLE monitoring_runs (
                    run_id UUID PRIMARY KEY, generated_at TIMESTAMPTZ NOT NULL,
                    window_start TIMESTAMPTZ NOT NULL, window_end TIMESTAMPTZ NOT NULL,
                    licensing_charge_count INTEGER NOT NULL,
                    licensing_priced_charge_count INTEGER NOT NULL,
                    total_license_cost NUMERIC(16,10) NOT NULL,
                    llm_request_count INTEGER NOT NULL,
                    llm_priced_request_count INTEGER NOT NULL,
                    llm_input_tokens INTEGER NOT NULL,
                    llm_output_tokens INTEGER NOT NULL,
                    llm_estimated_cost_usd NUMERIC(14,8) NOT NULL,
                    evaluated_rating_count INTEGER NOT NULL,
                    attributed_rating_count INTEGER NOT NULL,
                    positive_rating_count INTEGER NOT NULL,
                    recommendation_accuracy DOUBLE PRECISION
                )"""
            )
            connection.execute(
                text(
                    """INSERT INTO monitoring_runs VALUES
                    (:id, :now, :start, :now, 0, 0, 0, 0, 0, 0, 0, 0,
                     0, 0, 0, NULL)"""
                ),
                {
                    "id": uuid4(),
                    "now": self.now,
                    "start": self.now - timedelta(minutes=5),
                },
            )

    def test_legacy_upgrade_is_idempotent_and_preserves_rows(self):
        self.create_legacy_monitoring_table()

        migrate(self.engine)
        migrate(self.engine)

        inspector = inspect(self.engine)
        self.assertTrue(inspector.has_table("monitoring_route_latency"))
        columns = {
            column["name"] for column in inspector.get_columns("monitoring_runs")
        }
        self.assertTrue(
            {
                "request_count",
                "request_count_over_600ms",
                "request_latency_average_ms",
                "request_latency_p50_ms",
                "request_latency_p95_ms",
                "request_latency_p99_ms",
                "request_latency_maximum_ms",
                "rating_drift_tvd",
                "event_type_drift_tvd",
                "rating_drift_sample_count",
                "event_type_baseline_counts",
                "event_type_recent_counts",
                "availability_request_count",
                "availability_success_count",
                "availability_no_response_count",
                "availability_other_failure_count",
                "kafka_record_count",
                "rejected_invalid_encoding_count",
                "rejected_malformed_line_count",
                "rejected_unrecognized_event_count",
                "rejected_invalid_user_id_count",
                "rejected_invalid_event_time_count",
                "rejected_invalid_field_count",
                "rejected_unknown_movie_count",
                "rejected_total_count",
            }
            <= columns
        )
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    """SELECT request_count, request_count_over_600ms,
                    rating_drift_sample_count, event_type_baseline_counts,
                    event_type_recent_counts, request_latency_p50_ms
                    FROM monitoring_runs"""
                )
            ).one()
        self.assertEqual(tuple(row[:3]), (0, 0, 0))
        self.assertEqual(row[3], {})
        self.assertEqual(row[4], {})
        self.assertIsNone(row[5])

        with self.assertRaises(IntegrityError), self.engine.begin() as connection:
            connection.execute(
                text(
                    """UPDATE monitoring_runs
                    SET request_count = 1, request_count_over_600ms = 2"""
                )
            )

    def test_missing_monitoring_runs_rolls_back(self):
        with self.assertRaises(ProgrammingError):
            migrate(self.engine)
        self.assertFalse(inspect(self.engine).has_table("monitoring_route_latency"))

    def test_bootstrap_creates_current_monitoring_schema(self):
        migrate(self.engine, bootstrap_if_empty=True)

        inspector = inspect(self.engine)
        self.assertTrue(inspector.has_table("monitoring_runs"))
        self.assertTrue(inspector.has_table("monitoring_route_latency"))


if __name__ == "__main__":
    unittest.main()
