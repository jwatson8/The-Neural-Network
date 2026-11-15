"""Isolated database coverage for experiment decisions, persistence and lifecycle."""

import unittest
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, create_engine

from src.database.interactions import experiments, experiment_assignments, users, RecordNotFoundError
from src.database.schemas import ExperimentCreate, ExperimentAssignmentCreate, ExperimentLifecycleUpdate
from src.experimentation.manager import DecisionReason, ExperimentManager


class ExperimentManagerDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        event.listen(self.engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
        SQLModel.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        engine_patch = patch("src.database.database.get_engine", return_value=self.engine)
        engine_patch.start()
        self.addCleanup(engine_patch.stop)
        self.now = datetime(2026, 11, 15, tzinfo=UTC)
        users.create(user_id="user-1")
        self.manager = ExperimentManager(enabled=True)

    def experiment(self, **overrides):
        return experiments.create(ExperimentCreate(**(dict(
            name=str(uuid4()), created_at=self.now, updated_at=self.now,
            status="running", starts_at=self.now, ends_at=self.now + timedelta(days=1),
            traffic_allocation=Decimal("1"), treatment_allocation=Decimal("0.5"),
            control_policy="baseline", control_version="baseline-v1",
            treatment_policy="cost-rerank", treatment_version="cost-v2",
        ) | overrides)))

    def decide(self, experiment, **overrides):
        return self.manager.decide(**(dict(experiment_id=experiment.experiment_id,
            user_id="user-1", eligible=True, now=self.now) | overrides))

    def test_active_eligibility_and_time_boundaries_make_no_inactive_assignments(self):
        for status in ("draft", "paused", "completed", "aborted"):
            with self.subTest(status=status):
                decision = self.decide(self.experiment(status=status))
                self.assertEqual(decision.reason, DecisionReason.INACTIVE)
                self.assertIsNone(decision.variant)
        cases = [
            (dict(starts_at=None, ends_at=None), {}, DecisionReason.UNSCHEDULED),
            (dict(starts_at=self.now + timedelta(hours=1)), {}, DecisionReason.NOT_STARTED),
            ({}, dict(now=self.now + timedelta(days=1)), DecisionReason.EXPIRED),
            ({}, dict(eligible=False), DecisionReason.INELIGIBLE),
            ({}, dict(user_id="missing"), DecisionReason.UNKNOWN_USER),
            (dict(traffic_allocation=Decimal("0")), {}, DecisionReason.NOT_ENROLLED),
        ]
        for settings, request, reason in cases:
            with self.subTest(reason=reason):
                experiment = self.experiment(**settings)
                self.assertEqual(self.decide(experiment, **request).reason, reason)
        self.assertEqual(experiment_assignments.list_records(), [])
        self.assertIsNone(users.get("missing"))
        decision = self.manager.decide(experiment_id=uuid4(), user_id="user-1", eligible=True, now=self.now)
        self.assertEqual(decision.reason, DecisionReason.NOT_FOUND)
        experiment = self.experiment()
        self.assertTrue(self.decide(experiment).assigned)
        self.assertTrue(self.decide(experiment, now=self.now + timedelta(days=1, microseconds=-1)).assigned)

    def test_policy_selection_and_assignment_survive_manager_restart(self):
        for allocation, variant in ((Decimal("0"), "control"), (Decimal("1"), "treatment")):
            experiment = self.experiment(treatment_allocation=allocation)
            first = self.decide(experiment)
            second = ExperimentManager(enabled=True).decide(
                experiment_id=experiment.experiment_id, user_id="user-1", eligible=True, now=self.now)
            self.assertTrue(first.newly_assigned)
            self.assertFalse(second.newly_assigned)
            self.assertEqual(first.assignment_id, second.assignment_id)
            self.assertEqual(first.variant, variant)
            self.assertEqual(first.policy, experiment.control_policy if variant == "control" else experiment.treatment_policy)
            self.assertEqual(first.version, experiment.control_version if variant == "control" else experiment.treatment_version)
            self.assertEqual(experiment_assignments.get_for_user(experiment_id=experiment.experiment_id,
                                                               user_id="user-1").assignment_id, first.assignment_id)

    def test_persisted_assignment_wins_over_changed_thresholds_and_hash_prediction(self):
        experiment = self.experiment(treatment_allocation=Decimal("1"))
        seeded = experiment_assignments.create(ExperimentAssignmentCreate(
            experiment_id=experiment.experiment_id, user_id="user-1", variant="control", assigned_at=self.now))
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE experiments SET traffic_allocation=0"))
        decision = self.decide(experiment)
        self.assertEqual(decision.assignment_id, seeded.assignment_id)
        self.assertEqual(decision.variant, "control")
        self.assertFalse(decision.newly_assigned)
        self.assertFalse(self.decide(experiment, eligible=False).assigned)
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE experiments SET status='paused'"))
        self.assertEqual(self.decide(experiment).reason, DecisionReason.INACTIVE)
        self.assertEqual(len(experiment_assignments.list_records()), 1)

    def test_get_or_create_never_overwrites_winning_assignment(self):
        experiment = self.experiment()
        first, created = experiment_assignments.get_or_create(ExperimentAssignmentCreate(
            experiment_id=experiment.experiment_id, user_id="user-1", variant="control", assigned_at=self.now))
        second, repeated = experiment_assignments.get_or_create(ExperimentAssignmentCreate(
            experiment_id=experiment.experiment_id, user_id="user-1", variant="treatment",
            assigned_at=self.now + timedelta(hours=1)))
        self.assertTrue(created)
        self.assertFalse(repeated)
        self.assertEqual(first.assignment_id, second.assignment_id)
        self.assertEqual(second.variant, "control")
        self.assertEqual(second.assigned_at.replace(tzinfo=UTC), self.now)
        self.assertIsNone(experiment_assignments.get_for_user(experiment_id=experiment.experiment_id, user_id="missing"))
        with self.assertRaises(IntegrityError):
            experiment_assignments.get_or_create(ExperimentAssignmentCreate(
                experiment_id=experiment.experiment_id, user_id="missing", variant="control"))
        users.create(user_id="user-2")
        with self.assertRaises(IntegrityError):
            experiment_assignments.get_or_create(ExperimentAssignmentCreate(
                assignment_id=first.assignment_id, experiment_id=experiment.experiment_id,
                user_id="user-2", variant="control"))

    def test_assignment_rolls_back_if_decision_transaction_fails(self):
        experiment = self.experiment()
        original = experiment_assignments._get_or_create

        def fail_after_insert(session, data):
            original(session, data)
            raise RuntimeError("simulated failure after insert")

        with patch("src.database.interactions.experiment_assignments._get_or_create", side_effect=fail_after_insert):
            with self.assertRaisesRegex(RuntimeError, "simulated failure"):
                self.decide(experiment)
        self.assertEqual(experiment_assignments.list_records(), [])
        self.assertTrue(self.decide(experiment).newly_assigned)

    def transition(self, experiment, status, **overrides):
        return experiments.transition(ExperimentLifecycleUpdate(**(dict(experiment_id=experiment.experiment_id,
            status=status, occurred_at=self.now) | overrides)))

    def test_lifecycle_pause_resume_and_terminal_states_preserve_assignment(self):
        experiment = self.experiment(status="draft")
        self.transition(experiment, "running")
        assigned = self.decide(experiment)
        self.transition(experiment, "paused")
        self.assertEqual(self.decide(experiment).reason, DecisionReason.INACTIVE)
        self.transition(experiment, "running")
        self.assertEqual(self.decide(experiment).assignment_id, assigned.assignment_id)
        self.transition(experiment, "completed")
        self.assertEqual(self.decide(experiment).reason, DecisionReason.INACTIVE)
        self.assertEqual(self.transition(experiment, "completed").status, "completed")
        for target in ("draft", "running", "paused", "aborted"):
            with self.assertRaises(ValueError):
                self.transition(experiment, target)
        aborted = self.experiment(status="draft")
        self.transition(aborted, "aborted")
        with self.assertRaises(ValueError):
            self.transition(aborted, "running")
        self.assertEqual(experiments.get(aborted.experiment_id).status, "aborted")

    def test_invalid_lifecycle_changes_roll_back(self):
        experiment = self.experiment(status="draft")
        with self.assertRaises(ValueError):
            self.transition(experiment, "completed")
        with self.assertRaises(ValueError):
            self.transition(experiment, "running", occurred_at=self.now - timedelta(seconds=1))
        self.assertEqual(experiments.get(experiment.experiment_id).status, "draft")
        for config in (dict(starts_at=None, ends_at=None),
                       dict(starts_at=self.now - timedelta(days=2), ends_at=self.now)):
            with self.assertRaises(ValueError):
                self.transition(self.experiment(status="draft", **config), "running")
        with self.assertRaises(RecordNotFoundError):
            experiments.transition(ExperimentLifecycleUpdate(experiment_id=uuid4(), status="running", occurred_at=self.now))

    def test_utc_normalization_and_open_ended_schedule(self):
        experiment = self.experiment(status="draft", ends_at=None)
        self.transition(experiment, "running", occurred_at=self.now.astimezone(timezone(timedelta(hours=8))))
        decision = self.decide(experiment, now=self.now.astimezone(timezone(timedelta(hours=-8))))
        assignment = experiment_assignments.get(decision.assignment_id)
        self.assertEqual(assignment.assigned_at.replace(tzinfo=UTC), self.now)
        self.assertTrue(self.decide(experiment, now=self.now + timedelta(days=100)).assigned)


    def test_same_user_has_independent_persisted_assignments_per_experiment(self):
        control = self.decide(self.experiment(treatment_allocation="0"))
        treatment = self.decide(self.experiment(treatment_allocation="1"))
        self.assertNotEqual(control.assignment_id, treatment.assignment_id)
        self.assertEqual((control.variant, treatment.variant), ("control", "treatment"))
        self.assertEqual(len(experiment_assignments.list_records()), 2)

    def test_transition_graph_and_idempotent_updated_at(self):
        transitions = {
            "draft": {"running", "aborted"},
            "running": {"paused", "completed", "aborted"},
            "paused": {"running", "completed", "aborted"},
            "completed": set(), "aborted": set(),
        }
        for source, targets in transitions.items():
            for target in transitions:
                with self.subTest(source=source, target=target):
                    experiment = self.experiment(status=source)
                    if source == target or target in targets:
                        result = self.transition(experiment, target, occurred_at=self.now + timedelta(seconds=1))
                        self.assertEqual(result.status, target)
                        expected_time = self.now if source == target else self.now + timedelta(seconds=1)
                        self.assertEqual(result.updated_at.replace(tzinfo=UTC), expected_time)
                    else:
                        with self.assertRaises(ValueError):
                            self.transition(experiment, target)
                        self.assertEqual(experiments.get(experiment.experiment_id).status, source)

    def test_scheduling_does_not_enroll_until_start(self):
        experiment = self.experiment(status="draft", starts_at=self.now + timedelta(hours=1))
        self.transition(experiment, "running")
        self.assertEqual(self.decide(experiment).reason, DecisionReason.NOT_STARTED)
        self.assertEqual(experiment_assignments.list_records(), [])
        self.assertTrue(self.decide(experiment, now=self.now + timedelta(hours=1)).assigned)
