"""Repository/sink/ingestion integration using only in-memory SQLite."""

import json
import logging
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateTable
from sqlmodel import SQLModel, create_engine

from src.database.interactions import data_quality_logs, movies, recommendation_serves, stream_events, users
from src.database.models import DataQualityLog
from src.database.schemas import StreamEventCreate
from src.data_quality.drift import detect_rating_drift
from src.data_quality.ingestion import persist_stream_event
from src.data_quality.quality_logging import (
    LogContext, PostgresQualityLogSink, log_drift_result, log_validation_failure,
)


class DataQualityPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        engine_patch = patch("src.database.database.get_engine", return_value=self.engine)
        engine_patch.start()
        self.addCleanup(engine_patch.stop)
        self.logger = logging.getLogger(f"quality-persistence.{self.id()}")
        self.errors = [{"loc": ("rating",), "type": "less_than_equal", "msg": "secret"}]
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def event(self, **changes) -> dict:
        return {
            "event_id": UUID(int=1), "user_id": "test-user", "movie_id": "test-movie",
            "event_type": "rating", "timestamp": self.now.isoformat(), "rating": 7,
            **changes,
        }

    def seed(self) -> None:
        users.create(user_id="test-user")
        movies.create(movie_id="test-movie", title="Test Movie")

    def test_default_postgres_sink_persists_sanitized_errors(self) -> None:
        with self.assertLogs(self.logger, level="WARNING") as captured:
            log_validation_failure("kafka", self.errors, logger=self.logger)
        self.assertEqual(len(captured.records), 1)
        rows = data_quality_logs.list_records()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row.source, row.severity, row.issue_type), ("kafka", "WARNING", "validation_failure"))
        self.assertEqual(row.error_details[0]["validation_error_type"], "less_than_equal")
        self.assertEqual(row.log_metadata["rejected_record_count"], 1)
        self.assertIsNotNone(row.occurred_at)
        self.assertNotIn("secret", json.dumps(row.error_details))
        self.assertEqual(data_quality_logs.get(row.log_id).log_id, row.log_id)
        self.assertIsNone(data_quality_logs.get(UUID(int=0)))

    def test_optional_ids_persist_only_in_database_and_need_no_parent_rows(self) -> None:
        context = LogContext(user_id="private-user", movie_id="private-movie",
                             event_id=UUID(int=5), request_id=UUID(int=6))
        with self.assertLogs(self.logger, level="WARNING") as captured:
            log_validation_failure("movie_api", self.errors, context=context, logger=self.logger)
        row = data_quality_logs.list_records()[0]
        self.assertEqual((row.user_id, row.movie_id, row.event_id, row.request_id),
                         (context.user_id, context.movie_id, context.event_id, context.request_id))
        message = captured.records[0].getMessage()
        self.assertNotIn("private", message)
        self.assertNotIn(str(context.event_id), message)

    def test_context_is_optional(self) -> None:
        with self.assertLogs(self.logger, level="WARNING"):
            log_validation_failure("user_api", self.errors, logger=self.logger)
        row = data_quality_logs.list_records()[0]
        self.assertIsNone(row.user_id)
        self.assertIsNone(row.event_id)

    def test_drift_results_are_persisted_for_all_statuses(self) -> None:
        for old, new in (([1], [10]), ([1], [1]), ([], []), ([None], [1])):
            result = detect_rating_drift([{"rating": x} for x in old], [{"rating": x} for x in new], min_samples=1)
            with self.assertLogs(self.logger, level="INFO"):
                log_drift_result(result, logger=self.logger)
        rows = data_quality_logs.list_records()
        self.assertEqual({row.log_metadata["status"] for row in rows},
                         {"drift_detected", "no_drift", "insufficient_data", "invalid_data"})
        self.assertEqual({row.severity for row in rows}, {"WARNING", "INFO", "ERROR"})

    def test_search_time_filters_and_pagination(self) -> None:
        for index in range(3):
            data_quality_logs.create(source="kafka", severity="WARNING", issue_type="validation_failure",
                                     occurred_at=self.now + timedelta(hours=index), event_id=UUID(int=index))
        page = data_quality_logs.search(start=self.now, end=self.now + timedelta(hours=2), source="kafka", limit=1)
        self.assertEqual(page.records[0].event_id, UUID(int=1))
        next_page = data_quality_logs.search(start=self.now, end=self.now + timedelta(hours=2),
                                             source="kafka", limit=1, cursor=page.next_cursor)
        self.assertEqual(next_page.records[0].event_id, UUID(int=0))
        self.assertIsNone(next_page.next_cursor)
        self.assertEqual(len(data_quality_logs.search(event_id=UUID(int=2)).records), 1)
        self.assertEqual(data_quality_logs.search(source="movie_api").records, [])

    def test_repository_rejects_invalid_schema_and_read_parameters(self) -> None:
        with self.assertRaises(ValidationError):
            data_quality_logs.create(source="unknown", severity="WARNING", issue_type="validation_failure")
        with self.assertRaises(ValidationError):
            data_quality_logs.create(source="kafka", severity="WARNING", issue_type="validation_failure",
                                     occurred_at=self.now.replace(tzinfo=None))
        for values in ({"limit": 0}, {"limit": 1001}, {"offset": -1}, {"limit": True}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                data_quality_logs.list_records(**values)
        with self.assertRaises(ValueError):
            data_quality_logs.search(start=self.now, end=self.now)

    def test_postgres_ddl_uses_jsonb_and_has_no_identity_foreign_keys(self) -> None:
        ddl = str(CreateTable(DataQualityLog.__table__).compile(dialect=postgresql.dialect()))
        self.assertIn("JSONB", ddl)
        self.assertIn("TIMESTAMP WITH TIME ZONE", ddl)
        self.assertNotIn("FOREIGN KEY", ddl)

    def test_sink_failure_falls_back_without_leaking_sql_or_ids(self) -> None:
        with patch("src.database.interactions.data_quality_logs.create", side_effect=SQLAlchemyError("password secret SQL")):
            with self.assertLogs(self.logger, level="WARNING") as captured:
                log_validation_failure("kafka", self.errors, logger=self.logger,
                                       context=LogContext(user_id="private-user"))
        self.assertEqual(len(captured.records), 2)
        self.assertEqual(json.loads(captured.records[-1].getMessage())["issue_type"], "quality_log_persistence_failure")
        self.assertNotIn("secret", "".join(captured.output))
        self.assertNotIn("private-user", "".join(captured.output))
        self.assertEqual(data_quality_logs.list_records(), [])

    def test_missing_table_fallback_does_not_poison_other_transactions(self) -> None:
        DataQualityLog.__table__.drop(self.engine)
        with self.assertLogs(self.logger, level="WARNING") as captured:
            log_validation_failure("kafka", self.errors, logger=self.logger)
        self.assertEqual(len(captured.records), 2)
        users.create(user_id="after-log-failure")
        self.assertIsNotNone(users.get("after-log-failure"))

    def test_replaceable_sink_and_json_only_mode(self) -> None:
        sink = Mock()
        with self.assertLogs(self.logger, level="WARNING"):
            log_validation_failure("kafka", self.errors, sink=sink, logger=self.logger)
        self.assertEqual(sink.write.call_args.args[0]["source"], "kafka")
        self.assertNotIn("msg", sink.write.call_args.args[0]["error_details"][0])
        with self.assertLogs(self.logger, level="WARNING"):
            log_validation_failure("kafka", self.errors, sink=None, logger=self.logger)
        self.assertEqual(data_quality_logs.list_records(), [])

    def test_invalid_sink_or_context_are_programming_errors(self) -> None:
        for kwargs in ({"sink": object()}, {"context": {"user_id": "private"}}):
            with self.subTest(kwargs=kwargs), self.assertNoLogs(self.logger), self.assertRaises(TypeError):
                log_validation_failure("kafka", self.errors, logger=self.logger, **kwargs)
        with self.assertRaises(ValidationError):
            LogContext(event_id="not-a-uuid")

    def test_unexpected_sink_failure_is_reported_not_silently_ignored(self) -> None:
        sink = Mock()
        sink.write.side_effect = RuntimeError("secret backend failure")
        with self.assertLogs(self.logger, level="WARNING") as captured:
            log_validation_failure("kafka", self.errors, sink=sink, logger=self.logger)
        self.assertEqual(captured.records[-1].levelno, logging.ERROR)
        self.assertNotIn("secret", "".join(captured.output))

    def test_kafka_handoff_validates_exactly_once_and_replay_is_idempotent(self) -> None:
        self.seed()
        with patch.object(StreamEventCreate, "model_validate", wraps=StreamEventCreate.model_validate) as validate:
            with patch("src.data_quality.boundary.validate_stream_event", side_effect=AssertionError("double validation")):
                self.assertEqual(persist_stream_event(self.event()), (1, []))
            self.assertEqual(validate.call_count, 1)
        self.assertEqual(persist_stream_event(self.event()), (0, []))

    def test_invalid_kafka_event_is_logged_and_next_event_can_be_ingested(self) -> None:
        self.seed()
        with self.assertLogs(self.logger, level="WARNING"):
            inserted, errors = persist_stream_event(self.event(rating=99), logger=self.logger)
        self.assertIsNone(inserted)
        self.assertTrue(errors)
        self.assertNotIn("input", errors[0])
        self.assertEqual(len(data_quality_logs.list_records()), 1)
        self.assertEqual(stream_events.list_records(), [])
        self.assertEqual(persist_stream_event(self.event()), (1, []))

    def test_malformed_kafka_shapes_and_missing_fields_do_not_crash_handoff(self) -> None:
        for raw in ({}, None, [], "not-a-record"):
            with self.subTest(raw=raw), self.assertLogs(self.logger, level="WARNING"):
                inserted, errors = persist_stream_event(raw, logger=self.logger)
                self.assertIsNone(inserted)
                self.assertTrue(errors)

    def test_logging_outage_does_not_turn_validation_rejection_into_consumer_error(self) -> None:
        with patch("src.database.interactions.data_quality_logs.create", side_effect=SQLAlchemyError("outage")):
            with self.assertLogs(self.logger, level="WARNING"):
                inserted, errors = persist_stream_event(self.event(rating=99), logger=self.logger)
        self.assertIsNone(inserted)
        self.assertTrue(errors)

    def test_other_database_callers_still_validate(self) -> None:
        with self.assertRaises(ValidationError):
            stream_events.create(user_id="test", event_type="rating", event_time=self.now, movie_id="test", rating=99)
        with self.assertRaises(ValidationError):
            stream_events.create_many([self.event(rating=99)])

    def test_direct_serve_writes_enforce_score_range_and_preserve_valid_scores(self) -> None:
        self.seed()
        fields = dict(user_id="test-user", served_at=self.now, model_route="test-route",
                      model_version="test-version", request_received_at=self.now, serve_duration_ms=1)
        for score in (-1, 1):
            row = recommendation_serves.create(
                **fields, recommendations=[{"movie_id": "test-movie", "model_score": score}]
            )
            self.assertEqual(row.recommendations[0]["model_score"], score)
        for score in (-1.01, 1.02, True, float("nan"), float("inf"), -float("inf"), [], "bad"):
            with self.subTest(score=score), self.assertRaises(ValidationError) as raised:
                recommendation_serves.create(
                    **fields, recommendations=[{"movie_id": "test-movie", "model_score": score}]
                )
            self.assertEqual(raised.exception.errors()[0]["loc"], ("recommendations", 0, "model_score"))
        self.assertEqual(len(recommendation_serves.list_records()), 2)

    def test_ingestion_database_outage_propagates_for_retry(self) -> None:
        writer = Mock(side_effect=SQLAlchemyError("outage"))
        with self.assertRaises(SQLAlchemyError):
            persist_stream_event(self.event(), create_many=writer)
        writer.assert_called_once()


if __name__ == "__main__":
    unittest.main()
