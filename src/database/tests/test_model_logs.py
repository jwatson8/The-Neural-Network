import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateTable
from sqlmodel import Session, SQLModel, create_engine

from src.database.interactions import data_quality_logs, model_logs
from src.database.models import ModelLog


class ModelLogTests(unittest.TestCase):
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

    def test_training_log_round_trips_input_window_and_artifact_size(self):
        row = model_logs.create_train(
            model_name="collaborative-svd-v2",
            training_data_source="user_movie_history",
            training_data_start_at=self.now,
            training_data_end_at=self.now + timedelta(days=1),
            training_row_count=250,
            model_size_bytes=4096,
            occurred_at=self.now + timedelta(days=1, minutes=5),
        )

        stored = model_logs.get(row.log_id)
        page = model_logs.search(
            event_type="train",
            training_data_source="user_movie_history",
        )

        self.assertEqual(stored.event_type, "train")
        self.assertEqual(stored.training_data_source, "user_movie_history")
        self.assertEqual(stored.training_row_count, 250)
        self.assertEqual(stored.model_size_bytes, 4096)
        self.assertIsNone(stored.test_result)
        self.assertEqual([record.log_id for record in page.records], [row.log_id])

    def test_test_log_is_structured_and_isolated_from_data_quality_logs(self):
        result = {"precision_at_20": 0.31, "recall_at_20": 0.72, "passed": True}

        row = model_logs.create_test(
            model_name="collaborative-svd-v2",
            test_type="ranking_at_20",
            test_result=result,
            occurred_at=self.now,
        )

        self.assertEqual(model_logs.get(row.log_id).test_result, result)
        self.assertIsNone(row.training_data_source)
        self.assertEqual(data_quality_logs.list_records(), [])

    def test_search_filters_and_paginates_newest_first(self):
        for hour, model_name in enumerate(
            ("other-model", "target-model", "target-model")
        ):
            model_logs.create_test(
                log_id=UUID(int=hour + 1),
                model_name=model_name,
                test_type="ranking_at_20",
                test_result={"passed": True},
                occurred_at=self.now + timedelta(hours=hour),
            )

        first_page = model_logs.search(
            model_name="target-model", test_type="ranking_at_20", limit=1
        )
        second_page = model_logs.search(
            model_name="target-model",
            test_type="ranking_at_20",
            cursor=first_page.next_cursor,
            limit=1,
        )

        self.assertEqual(first_page.records[0].log_id, UUID(int=3))
        self.assertEqual(second_page.records[0].log_id, UUID(int=2))
        self.assertIsNone(second_page.next_cursor)

    def test_write_schemas_reject_invalid_event_data(self):
        valid_training = {
            "model_name": "model",
            "training_data_source": "stream_events",
            "training_data_start_at": self.now,
            "training_data_end_at": self.now + timedelta(hours=1),
            "training_row_count": 1,
        }
        invalid_training = (
            {**valid_training, "training_data_end_at": self.now},
            {**valid_training, "training_data_start_at": self.now.replace(tzinfo=None)},
            {**valid_training, "training_row_count": -1},
            {**valid_training, "model_size_bytes": -1},
            {**valid_training, "training_data_source": "unknown"},
        )
        for values in invalid_training:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                model_logs.create_train(**values)

        for values in (
            {"model_name": "model", "test_type": "ranking", "test_result": {}},
            {"model_name": "model", "test_type": "", "test_result": {"passed": True}},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                model_logs.create_test(**values)

    def test_database_constraint_rejects_mixed_event_shape(self):
        with Session(self.engine) as session:
            session.add(
                ModelLog(
                    event_type="test",
                    model_name="model",
                    occurred_at=self.now,
                    training_data_source="stream_events",
                    training_data_start_at=self.now,
                    training_data_end_at=self.now + timedelta(hours=1),
                    training_row_count=1,
                )
            )
            with self.assertRaises(IntegrityError):
                session.commit()

    def test_postgres_ddl_uses_jsonb_and_has_no_foreign_keys(self):
        ddl = str(
            CreateTable(ModelLog.__table__).compile(dialect=postgresql.dialect())
        )

        self.assertIn("JSONB", ddl)
        self.assertIn("ck_model_log_event_shape", ddl)
        self.assertNotIn("FOREIGN KEY", ddl)


if __name__ == "__main__":
    unittest.main()
