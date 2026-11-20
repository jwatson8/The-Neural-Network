"""Tests for ordered application of independently owned migrations."""

import unittest
from unittest.mock import Mock, patch

from src.database.migrations.apply_all import migrate_all


class MigrationRunnerTests(unittest.TestCase):
    def test_applies_experiment_then_monitoring_migrations(self):
        engine = Mock()
        calls = []
        with patch(
            "src.database.migrations.apply_all.add_experimentation_schema.migrate",
            side_effect=lambda *_args, **_kwargs: calls.append("experiments"),
        ) as experiments, patch(
            "src.database.migrations.apply_all.add_monitoring_schema.migrate",
            side_effect=lambda *_args, **_kwargs: calls.append("monitoring"),
        ) as monitoring:
            migrate_all(engine, bootstrap_if_empty=True)

        self.assertEqual(calls, ["experiments", "monitoring"])
        experiments.assert_called_once_with(engine, bootstrap_if_empty=True)
        monitoring.assert_called_once_with(engine)

    def test_stops_before_monitoring_when_experiment_migration_fails(self):
        with patch(
            "src.database.migrations.apply_all.add_experimentation_schema.migrate",
            side_effect=RuntimeError("failed"),
        ), patch(
            "src.database.migrations.apply_all.add_monitoring_schema.migrate"
        ) as monitoring:
            with self.assertRaisesRegex(RuntimeError, "failed"):
                migrate_all(Mock())

        monitoring.assert_not_called()


if __name__ == "__main__":
    unittest.main()
