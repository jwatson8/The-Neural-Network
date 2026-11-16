"""Report name search and assignment lookup through existing unique keys."""

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

from sqlmodel import SQLModel, create_engine

from src.database.interactions import experiments, experiment_assignments, experiment_reports, users
from src.database.interactions._pagination import SearchCursor
from src.database.schemas import ExperimentCreate, ExperimentAssignmentCreate, ExperimentReportCreate


class ExperimentQueryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        engine_patch = patch("src.database.database.get_engine", return_value=self.engine)
        engine_patch.start()
        self.addCleanup(engine_patch.stop)
        self.now = datetime(2026, 11, 15, tzinfo=UTC)
        self.first = self.experiment("license-cost")
        self.second = self.experiment("cold-start")

    def experiment(self, name):
        return experiments.create(ExperimentCreate(name=name, control_policy="c", control_version="v1",
                                                  treatment_policy="t", treatment_version="v1"))

    def report(self, experiment, report_id, generated_at=None):
        return experiment_reports.create(ExperimentReportCreate(
            report_id=UUID(int=report_id), experiment_id=experiment.experiment_id,
            generated_at=generated_at or self.now, window_start=self.now - timedelta(hours=1),
            window_end=self.now, analysis_version="v1", results={"count": report_id}))

    def test_report_search_by_name_id_and_unknown_name(self):
        first = self.report(self.first, 1)
        self.report(self.second, 2)
        self.assertEqual([r.report_id for r in experiment_reports.search(experiment_name="license-cost").records], [first.report_id])
        self.assertEqual([r.report_id for r in experiment_reports.search(experiment_id=self.first.experiment_id).records], [first.report_id])
        self.assertEqual(experiment_reports.search(experiment_name="missing").records, [])
        self.assertEqual(experiment_reports.search(experiment_name="License-Cost").records, [])
        self.assertEqual(experiment_reports.search(experiment_name="license-cost", experiment_id=self.second.experiment_id).records, [])
        self.assertEqual(len(experiment_reports.search().records), 2)

    def test_report_search_keyset_ties_and_generated_time_bounds(self):
        for report_id in (1, 2, 3):
            self.report(self.first, report_id)
        self.report(self.first, 4, self.now + timedelta(hours=1))
        self.report(self.second, 5)
        first_page = experiment_reports.search(experiment_name="license-cost", start=self.now,
            end=self.now + timedelta(hours=1), limit=2)
        self.assertEqual([r.report_id.int for r in first_page.records], [3, 2])
        self.assertIsNotNone(first_page.next_cursor)
        second_page = experiment_reports.search(experiment_name="license-cost", start=self.now,
            end=self.now + timedelta(hours=1), limit=2, cursor=first_page.next_cursor)
        self.assertEqual([r.report_id.int for r in second_page.records], [1])
        self.assertIsNone(second_page.next_cursor)
        self.assertEqual(experiment_reports.search(experiment_name="license-cost", end=self.now).records, [])

    def test_report_search_validates_filters(self):
        for kwargs in ({"experiment_name": " "}, {"experiment_name": "x" * 256}, {"limit": 0},
                       {"limit": 1001}, {"start": self.now.replace(tzinfo=None)},
                       {"start": self.now, "end": self.now},
                       {"cursor": SearchCursor("wrong-scope", self.now, uuid4())}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                experiment_reports.search(**kwargs)

    def test_assignment_pair_lookup_is_scoped_and_does_not_create_rows(self):
        users.create(user_id="42")
        users.create(user_id="43")
        first = experiment_assignments.create(ExperimentAssignmentCreate(
            experiment_id=self.first.experiment_id, user_id="42", variant="control"))
        second = experiment_assignments.create(ExperimentAssignmentCreate(
            experiment_id=self.second.experiment_id, user_id="42", variant="treatment"))
        third = experiment_assignments.create(ExperimentAssignmentCreate(
            experiment_id=self.first.experiment_id, user_id="43", variant="treatment"))
        for experiment, user, expected in ((self.first, "42", first), (self.second, "42", second), (self.first, "43", third)):
            stored = experiment_assignments.get_for_user(experiment_id=experiment.experiment_id, user_id=user)
            self.assertEqual(stored.assignment_id, expected.assignment_id)
            self.assertEqual(stored.variant, expected.variant)
            self.assertEqual(experiment_assignments.get(stored.assignment_id).assignment_id, expected.assignment_id)
        self.assertIsNone(experiment_assignments.get_for_user(experiment_id=uuid4(), user_id="42"))
        self.assertIsNone(experiment_assignments.get_for_user(experiment_id=self.first.experiment_id, user_id="missing"))
        self.assertEqual(len(experiment_assignments.list_records()), 3)
