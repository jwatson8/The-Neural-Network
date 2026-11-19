"""Pure allocation contract and disabled-manager tests."""

import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch
from uuid import UUID, uuid4

from src.experimentation.assignment import (
    HASH_SPACE, assigned_variant, assignment_bucket, below_allocation, enrolled,
)
from src.experimentation.manager import DecisionReason, ExperimentManager


class AssignmentAlgorithmTests(unittest.TestCase):
    def test_hash_contract_and_domain_separation(self):
        experiment = UUID(int=1)
        enrollment = assignment_bucket(experiment, "user-42", "enrollment")
        variant = assignment_bucket(experiment, "user-42", "variant")
        self.assertEqual(format(enrollment, "064x"), "e0e3af6b77ab9495f123fed7084d30977ac6c60dbd266815f9b689ffcd3e656a")
        self.assertEqual(format(variant, "064x"), "8a658cd4ef43f6d8aa5eddaf1245c82572a39a27725d8e2b970319ebe3a8e5d9")
        self.assertNotEqual(enrollment, variant)
        self.assertEqual(enrollment, assignment_bucket(experiment, "user-42", "enrollment"))
        self.assertNotEqual(enrollment, assignment_bucket(UUID(int=2), "user-42", "enrollment"))
        self.assertNotEqual(enrollment, assignment_bucket(experiment, "user-43", "enrollment"))
        self.assertNotEqual(assignment_bucket(experiment, "用户:42", "enrollment"), enrollment)

    def test_exact_half_open_thresholds_without_rounding(self):
        self.assertFalse(below_allocation(0, Decimal("0")))
        self.assertTrue(below_allocation(HASH_SPACE - 1, Decimal("1")))
        self.assertTrue(below_allocation(HASH_SPACE // 2 - 1, Decimal("0.5")))
        self.assertFalse(below_allocation(HASH_SPACE // 2, Decimal("0.5")))
        self.assertTrue(below_allocation(0, Decimal("1e-100")))
        self.assertFalse(below_allocation(1, Decimal("1e-100")))
        with self.assertRaises(ValueError):
            below_allocation(HASH_SPACE, Decimal("1"))
        for allocation in (Decimal("NaN"), Decimal("Infinity"), Decimal("-0.1"), Decimal("1.1"), 0.5):
            with self.subTest(allocation=allocation), self.assertRaises(ValueError):
                below_allocation(0, allocation)

    def test_zero_full_traffic_and_variant_endpoints(self):
        experiment = UUID(int=1)
        for user in ("1", "2", "测试"):
            self.assertFalse(enrolled(experiment, user, Decimal("0")))
            self.assertTrue(enrolled(experiment, user, Decimal("1")))
            self.assertEqual(assigned_variant(experiment, user, Decimal("0")), "control")
            self.assertEqual(assigned_variant(experiment, user, Decimal("1")), "treatment")

    def test_enrollment_and_variant_use_independent_buckets(self):
        experiment = UUID(int=1)
        users = [str(index) for index in range(10000)]
        selected = [user for user in users if enrolled(experiment, user, Decimal("0.2"))]
        treated = [user for user in selected if assigned_variant(experiment, user, Decimal("0.25")) == "treatment"]
        self.assertTrue(1800 < len(selected) < 2200)
        self.assertTrue(0.20 < len(treated) / len(selected) < 0.30)
        # Increasing traffic preserves membership without changing variant hashes.
        self.assertTrue(all(enrolled(experiment, user, Decimal("0.3")) for user in selected))

    def test_invalid_identifiers_are_rejected_without_canonicalizing_user_ids(self):
        for user in ("", " ", "x" * 256, None, 42):
            with self.subTest(user=user), self.assertRaises(ValueError):
                assignment_bucket(UUID(int=1), user, "enrollment")
        with self.assertRaises(ValueError):
            assignment_bucket("not-a-uuid", "42", "enrollment")
        with self.assertRaises(ValueError):
            assignment_bucket(UUID(int=1), "42", "unknown")
        self.assertNotEqual(assignment_bucket(UUID(int=1), "42", "enrollment"),
                            assignment_bucket(UUID(int=1), " 42", "enrollment"))

    def test_default_disabled_manager_never_connects(self):
        with patch("src.database.interactions.experiment_assignments.decide", side_effect=AssertionError("unexpected database access")):
            decision = ExperimentManager().decide(experiment_id=uuid4(), user_id="42", eligible=True)
        self.assertEqual(decision.reason, DecisionReason.DISABLED)
        self.assertFalse(decision.assigned)
        self.assertIsNone(decision.assignment_id)
        self.assertIsNone(decision.variant)
        self.assertIsNone(decision.policy)
        self.assertIsNone(decision.version)

    def test_strict_flags_and_aware_request_time(self):
        for value in ("false", 1, None):
            with self.assertRaises(ValueError):
                ExperimentManager(enabled=value)
            with self.assertRaises(ValueError):
                ExperimentManager().decide(experiment_id=uuid4(), user_id="42", eligible=value)
        with self.assertRaises(ValueError):
            ExperimentManager().decide(experiment_id=uuid4(), user_id="42", eligible=True, now=datetime(2026, 1, 1))


    def test_decisions_are_immutable_and_database_errors_propagate(self):
        manager = ExperimentManager()
        decision = manager.decide(experiment_id=uuid4(), user_id="42", eligible=True)
        with self.assertRaises(FrozenInstanceError):
            decision.variant = "treatment"
        with patch("src.database.interactions.experiment_assignments.decide", side_effect=RuntimeError("database failure")):
            with self.assertRaisesRegex(RuntimeError, "database failure"):
                ExperimentManager(enabled=True).decide(experiment_id=uuid4(), user_id="42", eligible=True)
