"""Opt-in real PostgreSQL tests using a newly initialized, Unix-socket-only cluster.

Set M3_TEST_POSTGRES_BIN_DIR to a directory containing initdb and pg_ctl.
No existing database URLs, services, Docker contexts or production resources are used.
"""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import URL, inspect, text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlmodel import SQLModel, create_engine

from src.database.interactions import experiments, experiment_assignments, experiment_reports, recommendation_serves
from src.database.initialize import create_tables
from src.database.migrations.phase1 import migrate
from src.database.models import User
from src.database.schemas import ExperimentCreate, ExperimentAssignmentCreate, ExperimentReportCreate

BIN_DIR = os.getenv("M3_TEST_POSTGRES_BIN_DIR")


@unittest.skipUnless(BIN_DIR, "set M3_TEST_POSTGRES_BIN_DIR for a disposable local PostgreSQL cluster")
class ExperimentMigrationPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="m3pg-", dir="/tmp")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.root = Path(cls.directory.name)
        cls.data = cls.root / "data"
        cls.socket = cls.root / "socket"
        cls.socket.mkdir()
        # Explicit options avoid inheriting connection destinations from the shell.
        cls.env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
        cls.command("initdb", "-D", str(cls.data), "-U", "m3test", "--auth=trust", "--no-locale", "--encoding=UTF8")
        cls.command("pg_ctl", "-D", str(cls.data), "-l", str(cls.root / "postgres.log"),
                    "-o", f"-F -h '' -k {cls.socket}", "-w", "-t", "15", "start")
        cls.addClassCleanup(cls.command, "pg_ctl", "-D", str(cls.data), "-m", "immediate", "-w", "stop")
        cls.engine = create_engine(URL.create("postgresql+psycopg", username="m3test", database="postgres",
                                             query={"host": str(cls.socket)}))
        cls.addClassCleanup(cls.engine.dispose)

    @classmethod
    def command(cls, binary, *arguments):
        result = subprocess.run([str(Path(BIN_DIR) / binary), *arguments], env=cls.env,
                                text=True, capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError(f"{binary} failed: {result.stdout}\n{result.stderr}")
        return result

    def setUp(self):
        # Every test gets an empty schema inside our own temporary cluster.
        self.schema = "test_" + uuid4().hex
        with self.engine.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{self.schema}"')
        self.test_engine = create_engine(self.engine.url, connect_args={"options": f"-csearch_path={self.schema}"})
        self.addCleanup(self.test_engine.dispose)
        self.now = datetime(2026, 11, 15, tzinfo=UTC)

    def legacy_database(self):
        User.__table__.create(self.test_engine)
        with self.test_engine.begin() as connection:
            connection.exec_driver_sql("""CREATE TABLE recommendation_serves (
                request_id UUID PRIMARY KEY, user_id VARCHAR(255) NOT NULL REFERENCES users(user_id),
                served_at TIMESTAMPTZ NOT NULL, model_route VARCHAR(100) NOT NULL,
                model_version VARCHAR(255) NOT NULL, recommendations JSONB NOT NULL,
                request_received_at TIMESTAMPTZ NOT NULL, serve_duration_ms FLOAT NOT NULL
            )""")
            connection.execute(text("INSERT INTO users(user_id, first_seen) VALUES ('legacy-user', :now)"), {"now": self.now})
            connection.execute(text("""INSERT INTO recommendation_serves VALUES
                (:id, 'legacy-user', :now, 'popularity_fallback', 'legacy-v1',
                 '[{"movie_id":"old-movie"}]'::jsonb, :now, 12.5)"""), {"id": uuid4(), "now": self.now})

    def test_legacy_upgrade_twice_preserves_rows_and_enforces_postgres_constraints(self):
        self.legacy_database()
        with self.test_engine.connect() as connection:
            before = connection.execute(text("SELECT * FROM recommendation_serves")).one()
        migrate(self.test_engine)
        migrate(self.test_engine)
        with self.test_engine.connect() as connection:
            after = connection.execute(text("SELECT * FROM recommendation_serves")).one()
            self.assertEqual(tuple(after[:-1]), tuple(before))
            self.assertIsNone(after[-1])
            self.assertTrue(connection.execute(text("""SELECT convalidated FROM pg_constraint
                WHERE conrelid='recommendation_serves'::regclass
                  AND conname='recommendation_serves_experiment_assignment_id_fkey'""")).scalar_one())
        inspector = inspect(self.test_engine)
        for table in ("experiments", "experiment_assignments", "experiment_reports"):
            self.assertTrue(inspector.has_table(table))
        self.assertEqual(str(next(c for c in inspector.get_columns("experiment_reports") if c["name"] == "results")["type"]), "JSONB")
        with patch("src.database.database.get_engine", return_value=self.test_engine):
            experiment = experiments.create(ExperimentCreate(name="pg-test", control_policy="c", control_version="1",
                treatment_policy="t", treatment_version="2"))
            assignment_data = ExperimentAssignmentCreate(experiment_id=experiment.experiment_id,
                user_id="legacy-user", variant="control")
            assignment = experiment_assignments.create(assignment_data)
            with self.assertRaises(IntegrityError):
                experiment_assignments.create(assignment_data)
            report = experiment_reports.create(ExperimentReportCreate(experiment_id=experiment.experiment_id,
                window_start=self.now, window_end=self.now + timedelta(hours=1), analysis_version="v1",
                results={"nested": {"unknown": None}}))
            linked = recommendation_serves.create(user_id="legacy-user", served_at=self.now,
                request_received_at=self.now, serve_duration_ms=1, model_route="test", model_version="1",
                recommendations=[{"movie_id": "old-movie"}], experiment_assignment_id=assignment.assignment_id)
            self.assertEqual(recommendation_serves.get(linked.request_id).experiment_assignment_id, assignment.assignment_id)
            self.assertEqual(experiment_reports.get(report.report_id).results, {"nested": {"unknown": None}})
        for sql in ("UPDATE experiments SET traffic_allocation=2",
                    "UPDATE experiment_assignments SET variant='invalid'",
                    "UPDATE experiment_reports SET window_end=window_start",
                    "UPDATE recommendation_serves SET experiment_assignment_id='00000000-0000-0000-0000-000000000001'"):
            with self.subTest(sql=sql), self.assertRaises(IntegrityError), self.test_engine.begin() as connection:
                connection.exec_driver_sql(sql)
        migrate(self.test_engine)
        with self.test_engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT count(*) FROM experiment_assignments")).scalar_one(), 1)

    def test_failed_migration_rolls_back_all_ddl(self):
        # Missing recommendation_serves forces a failure after the three CREATEs.
        User.__table__.create(self.test_engine)
        with self.assertRaises(ProgrammingError):
            migrate(self.test_engine)
        inspector = inspect(self.test_engine)
        self.assertFalse(inspector.has_table("experiments"))
        self.assertFalse(inspector.has_table("experiment_assignments"))
        self.assertFalse(inspector.has_table("experiment_reports"))
        # Retry after fixing the prerequisite is supported.
        with self.test_engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE recommendation_serves (request_id UUID PRIMARY KEY)")
        migrate(self.test_engine)

    def test_fresh_schema_and_legacy_migration_have_matching_experiment_structure(self):
        SQLModel.metadata.create_all(self.test_engine)
        migrate(self.test_engine)
        migrated_schema = "legacy_" + uuid4().hex
        with self.engine.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{migrated_schema}"')
        legacy_engine = create_engine(self.engine.url, connect_args={"options": f"-csearch_path={migrated_schema}"})
        self.addCleanup(legacy_engine.dispose)
        with patch.object(self, "test_engine", legacy_engine):
            self.legacy_database()
        migrate(legacy_engine)
        fresh, upgraded = inspect(self.test_engine), inspect(legacy_engine)
        for table in ("experiments", "experiment_assignments", "experiment_reports"):
            with self.subTest(table=table):
                columns = lambda inspector: [(c["name"], str(c["type"]), c["nullable"]) for c in inspector.get_columns(table)]
                self.assertEqual(columns(fresh), columns(upgraded))
                checks = lambda inspector: {c["name"]: c["sqltext"] for c in inspector.get_check_constraints(table)}
                self.assertEqual(checks(fresh), checks(upgraded))
                indexes = lambda inspector: {(i["name"], tuple(i["column_names"])) for i in inspector.get_indexes(table)}
                self.assertEqual(indexes(fresh), indexes(upgraded))
                unique_keys = lambda inspector: {tuple(c["column_names"]) for c in inspector.get_unique_constraints(table)}
                self.assertEqual(unique_keys(fresh), unique_keys(upgraded))
                foreign_keys = lambda inspector: {(tuple(c["constrained_columns"]), c["referred_table"], tuple(c["referred_columns"])) for c in inspector.get_foreign_keys(table)}
                self.assertEqual(foreign_keys(fresh), foreign_keys(upgraded))

    def test_initializer_upgrades_legacy_schema_and_is_repeatable(self):
        self.legacy_database()
        with patch("src.database.initialize.get_engine", return_value=self.test_engine):
            create_tables()
            create_tables()
        with self.test_engine.connect() as connection:
            self.assertIsNone(connection.execute(text(
                "SELECT experiment_assignment_id FROM recommendation_serves"
            )).scalar_one())
            self.assertEqual(connection.execute(text("SELECT count(*) FROM experiments")).scalar_one(), 0)
