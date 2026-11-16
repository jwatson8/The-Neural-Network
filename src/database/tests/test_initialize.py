"""Initialization must not migrate an existing database or repair partial schemas."""

import unittest
from contextlib import redirect_stderr
from io import StringIO
from unittest.mock import patch

from sqlalchemy import event, inspect, text
from sqlmodel import SQLModel, create_engine

from src.database.initialize import create_tables, main, verify_schema
from src.database.migrations.add_experimentation_schema import main as migrate_main
from src.database.models import User


class InitializationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        self.addCleanup(self.engine.dispose)
        engine_patch = patch("src.database.initialize.get_engine", return_value=self.engine)
        engine_patch.start()
        self.addCleanup(engine_patch.stop)
        self.statements = []
        event.listen(self.engine, "before_cursor_execute", self.record_statement)

    def record_statement(self, connection, cursor, statement, parameters, context, executemany):
        self.statements.append(statement.strip().split()[0].upper())

    def assert_read_only(self):
        self.assertTrue(self.statements)
        self.assertLessEqual(set(self.statements), {"SELECT", "PRAGMA"})

    def test_empty_database_creation_and_repeat_startup(self):
        with patch("src.database.migrations.add_experimentation_schema.migrate") as migration:
            create_tables()
            self.assertEqual(set(inspect(self.engine).get_table_names()), set(SQLModel.metadata.tables))
            self.statements.clear()
            with patch.object(SQLModel.metadata, "create_all") as create_all:
                create_tables()
            create_all.assert_not_called()
            migration.assert_not_called()
        self.assert_read_only()
        column = next(c for c in inspect(self.engine).get_columns("recommendation_serves")
                      if c["name"] == "experiment_assignment_id")
        self.assertTrue(column["nullable"])

    def test_existing_legacy_database_fails_without_any_schema_or_record_changes(self):
        User.__table__.create(self.engine)
        with self.engine.begin() as connection:
            connection.execute(text("INSERT INTO users (user_id, first_seen) VALUES ('existing', CURRENT_TIMESTAMP)"))
            connection.execute(text("CREATE TABLE recommendation_serves (request_id TEXT PRIMARY KEY)"))
        self.statements.clear()
        with patch.object(SQLModel.metadata, "create_all") as create_all, \
                patch("src.database.migrations.add_experimentation_schema.migrate") as migration:
            with self.assertRaisesRegex(RuntimeError, "requires an explicit migration") as error:
                create_tables()
            create_all.assert_not_called()
            migration.assert_not_called()
        self.assertIn("add_experimentation_schema --apply", str(error.exception))
        self.assert_read_only()
        self.assertEqual(set(inspect(self.engine).get_table_names()), {"users", "recommendation_serves"})
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT user_id FROM users")).scalar_one(), "existing")
        self.assertEqual([c["name"] for c in inspect(self.engine).get_columns("recommendation_serves")], ["request_id"])

    def test_partial_schema_is_not_completed_implicitly(self):
        User.__table__.create(self.engine)
        self.statements.clear()
        with self.assertRaisesRegex(RuntimeError, "missing table experiments"):
            create_tables()
        self.assert_read_only()
        self.assertEqual(inspect(self.engine).get_table_names(), ["users"])

    def test_check_command_does_not_bootstrap_empty_database(self):
        with patch("sys.argv", ["initialize", "--check"]):
            with self.assertRaisesRegex(RuntimeError, "requires an explicit migration"):
                main()
        self.assert_read_only()
        self.assertEqual(inspect(self.engine).get_table_names(), [])

    def test_schema_check_accepts_current_database_without_ddl(self):
        SQLModel.metadata.create_all(self.engine)
        self.statements.clear()
        verify_schema(self.engine)
        self.assert_read_only()

    def test_explicit_migration_requires_apply_before_connecting(self):
        with patch("sys.argv", ["add_experimentation_schema"]), \
                patch("src.database.database.get_engine") as get_engine, \
                redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as error:
                migrate_main()
        self.assertEqual(error.exception.code, 2)
        get_engine.assert_not_called()
