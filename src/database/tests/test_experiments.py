"""Isolated persistence tests; no serving, randomization or analysis logic."""

import inspect
import unittest
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import event, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateTable
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

from src.database.interactions import (
    experiments, experiment_assignments, experiment_reports,
    recommendation_serves, users,
)
from src.database.migrations.phase1 import migrate
from src.database.models import Experiment, ExperimentAssignment, ExperimentReport, RecommendationServe
from src.database.schemas import ExperimentCreate, ExperimentAssignmentCreate, ExperimentReportCreate


class ExperimentPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(self.engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
        SQLModel.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        engine_patch = patch("src.database.database.get_engine", return_value=self.engine)
        engine_patch.start()
        self.addCleanup(engine_patch.stop)
        self.now = datetime(2026, 11, 15, tzinfo=UTC)
        users.create(user_id="user-1")

    def experiment(self, **overrides):
        return experiments.create(ExperimentCreate(**(dict(
            name="license-cost", control_policy="baseline", control_version="v1",
            treatment_policy="cost-rerank", treatment_version="v2",
            created_at=self.now, updated_at=self.now,
            config={"nested": {"enabled": False}},
        ) | overrides)))

    def assignment(self, experiment, **overrides):
        return experiment_assignments.create(ExperimentAssignmentCreate(**(dict(
            experiment_id=experiment.experiment_id, user_id="user-1", variant="control",
        ) | overrides)))

    def serve(self, **overrides):
        return recommendation_serves.create(**(dict(
            user_id="user-1", served_at=self.now, model_route="popularity_fallback",
            model_version="v1", recommendations=[{"movie_id": "unchanged-json-id"}],
            request_received_at=self.now, serve_duration_ms=1,
        ) | overrides))

    def test_round_trip_defaults_json_and_bounded_reads(self):
        experiment = self.experiment()
        self.assertEqual(experiment.status, "draft")
        self.assertEqual(experiment.traffic_allocation, Decimal("0"))
        self.assertEqual(experiment.treatment_allocation, Decimal("0.5"))
        self.assertEqual(experiments.get(experiment.experiment_id).config, {"nested": {"enabled": False}})
        assignment = self.assignment(experiment)
        report = experiment_reports.create(ExperimentReportCreate(
            experiment_id=experiment.experiment_id, window_start=self.now,
            window_end=self.now + timedelta(hours=1), analysis_version="schema-only-v1",
            results={"cost": {"control": None}, "counts": [0, 1]},
        ))
        self.assertEqual(experiment_assignments.get(assignment.assignment_id).variant, "control")
        self.assertEqual(experiment_reports.get(report.report_id).results["counts"], [0, 1])
        self.assertEqual(experiment_reports.list_records(experiment_id=experiment.experiment_id)[0].report_id, report.report_id)
        self.assertEqual(experiment_reports.list_records(experiment_id=uuid4()), [])
        for module in (experiments, experiment_assignments, experiment_reports):
            self.assertIsNone(module.get(uuid4()))
            for kwargs in ({"limit": 0}, {"limit": 1001}, {"offset": -1}):
                with self.assertRaises(ValueError):
                    module.list_records(**kwargs)
            for name in module.__all__:
                self.assertTrue(inspect.getdoc(getattr(module, name)))
                self.assertNotIn("session", inspect.signature(getattr(module, name)).parameters)

    def test_duplicate_assignment_and_name_rollback_without_replacement(self):
        experiment = self.experiment()
        assignment = self.assignment(experiment)
        with self.assertRaises(IntegrityError):
            self.assignment(experiment, variant="treatment")
        self.assertEqual(experiment_assignments.get(assignment.assignment_id).variant, "control")
        with self.assertRaises(IntegrityError):
            self.experiment()
        self.assertEqual(len(experiments.list_records()), 1)
        other = self.experiment(name="other")
        self.assignment(other, variant="treatment")
        self.assertEqual(len(experiment_assignments.list_records()), 2)

    def test_foreign_keys_and_parent_delete_restriction(self):
        experiment = self.experiment()
        for overrides in ({"experiment_id": uuid4()}, {"user_id": "missing"}):
            with self.assertRaises(IntegrityError):
                self.assignment(experiment, **overrides)
        self.assignment(experiment)
        with self.assertRaises(IntegrityError), self.engine.begin() as connection:
            connection.execute(text("DELETE FROM experiments"))
        with self.assertRaises(IntegrityError):
            experiment_reports.create(ExperimentReportCreate(
                experiment_id=uuid4(), window_start=self.now,
                window_end=self.now + timedelta(hours=1), analysis_version="v1"))

    def test_legacy_serves_and_explicit_assignment_link(self):
        legacy = self.serve()
        self.assertIsNone(recommendation_serves.get(legacy.request_id).experiment_assignment_id)
        experiment = self.experiment()
        assignment = self.assignment(experiment)
        linked = self.serve(experiment_assignment_id=assignment.assignment_id)
        self.assertEqual(recommendation_serves.get(linked.request_id).experiment_assignment_id, assignment.assignment_id)
        with self.assertRaises(ValueError):
            self.serve(experiment_assignment_id=uuid4())
        users.create(user_id="user-2")
        with self.assertRaises(ValueError):
            self.serve(user_id="user-2", experiment_assignment_id=assignment.assignment_id)
        with self.assertRaises(IntegrityError), self.engine.begin() as connection:
            connection.execute(text("DELETE FROM experiment_assignments"))

    def test_schema_validation_and_utc_normalization(self):
        for overrides in ({"name": " "}, {"status": "enabled"}, {"traffic_allocation": "1.01"},
                          {"treatment_allocation": "-0.1"}, {"control_version": ""},
                          {"traffic_allocation": "NaN"}, {"unexpected": True},
                          {"starts_at": self.now.replace(tzinfo=None)},
                          {"ends_at": self.now},
                          {"starts_at": self.now, "ends_at": self.now},
                          {"updated_at": self.now - timedelta(seconds=1)}):
            with self.subTest(overrides=overrides), self.assertRaises(ValidationError):
                self.experiment(**overrides)
        localized = self.now.astimezone(timezone(timedelta(hours=8)))
        data = ExperimentCreate(name="utc", control_policy="c", control_version="1",
                                treatment_policy="t", treatment_version="1", starts_at=localized)
        self.assertEqual(data.starts_at.tzinfo, UTC)
        for overrides in ({"variant": "bad"}, {"user_id": " "}, {"assigned_at": self.now.replace(tzinfo=None)}):
            with self.assertRaises(ValidationError):
                self.assignment(self.experiment(name=str(uuid4())), **overrides)
        for overrides in ({"window_end": self.now}, {"analysis_version": " "},
                          {"status": "running"}, {"generated_at": self.now.replace(tzinfo=None)}):
            with self.assertRaises(ValidationError):
                ExperimentReportCreate(**(dict(experiment_id=uuid4(), window_start=self.now,
                    window_end=self.now + timedelta(hours=1), analysis_version="v1") | overrides))

    def test_database_checks_cannot_be_bypassed_with_raw_updates(self):
        experiment = self.experiment()
        self.assignment(experiment)
        report = experiment_reports.create(ExperimentReportCreate(experiment_id=experiment.experiment_id,
            window_start=self.now, window_end=self.now + timedelta(hours=1), analysis_version="v1"))
        for sql in (
            "UPDATE experiments SET status='invalid'", "UPDATE experiments SET name=' '",
            "UPDATE experiments SET traffic_allocation=2", "UPDATE experiments SET treatment_allocation=-1",
            "UPDATE experiments SET control_policy=' '",
            "UPDATE experiments SET ends_at=created_at", "UPDATE experiments SET updated_at='2000-01-01'",
            "UPDATE experiment_assignments SET variant='invalid'",
            "UPDATE experiment_reports SET window_end=window_start", "UPDATE experiment_reports SET status='running'",
            "UPDATE experiment_reports SET analysis_version=' '",
        ):
            with self.subTest(sql=sql), self.assertRaises(IntegrityError), self.engine.begin() as connection:
                connection.execute(text(sql))
        self.assertEqual(experiment_reports.get(report.report_id).status, "provisional")

    def test_postgres_ddl_has_jsonb_timezone_and_nullable_fk(self):
        for model in (Experiment, ExperimentReport):
            ddl = str(CreateTable(model.__table__).compile(dialect=postgresql.dialect()))
            self.assertIn("JSONB", ddl)
            self.assertIn("TIMESTAMP WITH TIME ZONE", ddl)
        column = RecommendationServe.__table__.c.experiment_assignment_id
        self.assertTrue(column.nullable)
        self.assertEqual(next(iter(column.foreign_keys)).target_fullname, "experiment_assignments.assignment_id")
        ddl = str(CreateTable(ExperimentAssignment.__table__).compile(dialect=postgresql.dialect()))
        self.assertIn("UNIQUE (experiment_id, user_id)", ddl)
        with self.assertRaisesRegex(ValueError, "requires PostgreSQL"):
            migrate(self.engine)
