import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateTable
from sqlmodel import SQLModel, create_engine

from src.database.interactions import rejected_requests
from src.database.migrations import add_rejected_requests_schema
from src.database.models import RejectedRequest
from src.database.schemas import RAW_LINE_LIMIT, RejectedRequestCreate, StreamEventCreate
from src.service import course_stream

TOPIC = "movielog1"


def rejection(offset, **overrides):
    return {
        "topic": TOPIC,
        "partition_id": 0,
        "offset_id": offset,
        "raw_line": f"2026-01-01T00:00:00,{offset},GET /data/m/x/1.mpg",
        "reason": "unknown_movie",
        "event_kind": "watch",
        **overrides,
    }


class RejectedRequestTests(unittest.TestCase):
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

    def test_create_round_trips_and_replay_is_skipped(self):
        values = rejection(
            7,
            reason="invalid_user_id",
            user_id="abc12",
            event_time=self.now,
            raw_line="2026-01-01T00:00:00,abc12,GET /data/m/x/1.mpg",
        )
        self.assertTrue(rejected_requests.create(**values))
        self.assertFalse(rejected_requests.create(**values))

        event_id = course_stream._event_id(TOPIC, 0, 7)
        stored = rejected_requests.get(event_id)
        self.assertEqual(stored.user_id, "abc12")
        self.assertEqual(stored.reason, "invalid_user_id")
        self.assertEqual(stored.event_kind, "watch")
        self.assertEqual(stored.raw_line, values["raw_line"])
        self.assertFalse(stored.raw_line_truncated)
        self.assertEqual(stored.error_details, [])
        self.assertEqual(len(rejected_requests.list_records()), 1)

    def test_event_id_matches_consumer_stream_event_id(self):
        record = RejectedRequestCreate(**rejection(123456789012, partition_id=3))
        self.assertEqual(
            record.event_id, course_stream._event_id(TOPIC, 3, 123456789012)
        )

    def test_unidentifiable_line_needs_only_position_and_raw_value(self):
        rejected_requests.create(
            topic=TOPIC, partition_id=1, offset_id=2,
            raw_line="garbage without commas", reason="malformed_line",
        )
        stored = rejected_requests.get(course_stream._event_id(TOPIC, 1, 2))
        self.assertEqual(stored.event_kind, "unknown")
        self.assertIsNone(stored.user_id)
        self.assertIsNone(stored.event_time)

    def test_raw_bytes_are_escaped_and_long_lines_truncated(self):
        record = RejectedRequestCreate(
            **rejection(1, reason="invalid_encoding", raw_line=b"bad \xff\x00 bytes")
        )
        self.assertEqual(record.raw_line, "bad \\xff\\x00 bytes")

        record = RejectedRequestCreate(**rejection(2, raw_line="x" * (RAW_LINE_LIMIT + 5)))
        self.assertEqual(len(record.raw_line), RAW_LINE_LIMIT)
        self.assertTrue(record.raw_line_truncated)

        record = RejectedRequestCreate(**rejection(3, user_id="a\x00b" + "9" * 300))
        self.assertTrue(record.user_id.startswith("a\\x00b"))
        self.assertEqual(len(record.user_id), 255)

    def test_accepts_every_error_shape_the_consumer_produces(self):
        parsed = course_stream._parse_line(
            TOPIC, 0, 1, "2026-01-01T00:00:00,abc,GET /data/m/x/1.mpg"
        )
        self.assertEqual(parsed["kind"], "invalid")
        try:
            StreamEventCreate.model_validate({
                "user_id": "1", "event_type": "rating", "event_time": self.now,
                "movie_id": "x", "rating": 99,
            })
        except ValidationError as exc:
            pydantic_errors = exc.errors(
                include_url=False, include_context=False, include_input=False
            )
        unknown_movie = {"loc": ("movie_id",), "type": "value_error",
                         "msg": "Unknown movie identifier"}

        record = RejectedRequestCreate(**rejection(
            1, error_details=[*parsed["errors"], *pydantic_errors, unknown_movie],
        ))
        self.assertEqual(record.error_details[0], {
            "loc": ["user_id"], "type": "value_error",
            "msg": "User identifier must contain ASCII digits",
        })
        self.assertEqual(record.error_details[-1]["loc"], ["movie_id"])
        self.assertTrue(all(set(d) <= {"loc", "type", "msg"} for d in record.error_details))

    def test_invalid_inputs_are_rejected(self):
        for override in (
            {"reason": "something_else"},
            {"event_kind": "click"},
            {"partition_id": True},
            {"offset_id": -1},
            {"topic": ""},
            {"raw_line": None},
            {"user_id": 12},
            {"event_time": datetime(2026, 1, 1)},
            {"received_at": datetime(2026, 1, 1)},
            {"error_details": [{"type": "value_error"}]},
            {"error_details": [{"loc": (True,), "type": "value_error"}]},
            {"error_details": ["not a mapping"]},
        ):
            with self.subTest(override=override), self.assertRaises(ValidationError):
                rejected_requests.create(**rejection(1, **override))
        self.assertEqual(rejected_requests.list_records(), [])

    def test_create_many_is_atomic_and_replay_safe(self):
        first, second = rejection(1), rejection(2, received_at=self.now)
        self.assertEqual(rejected_requests.create_many([first, first, second]), 2)
        self.assertEqual(rejected_requests.create_many([first, second]), 0)
        self.assertEqual(len(rejected_requests.list_records()), 2)

        with self.assertRaises(ValidationError):
            rejected_requests.create_many([rejection(3), rejection(4, reason="nope")])
        self.assertEqual(len(rejected_requests.list_records()), 2)
        self.assertEqual(rejected_requests.create_many([]), 0)

    def test_search_filters_and_paginates_newest_first(self):
        rejected_requests.create_many([
            rejection(i, reason=reason, event_kind=kind, user_id=user,
                      received_at=self.now + timedelta(minutes=i))
            for i, (reason, kind, user) in enumerate((
                ("invalid_user_id", "watch", "abc"),
                ("malformed_line", "unknown", None),
                ("invalid_user_id", "rating", "x1"),
                ("unrecognized_event", "unknown", "42"),
            ))
        ])

        def ids(page):
            return [(r.offset_id) for r in page.records]

        page = rejected_requests.search(reason="invalid_user_id", limit=1)
        self.assertEqual(ids(page), [2])
        page = rejected_requests.search(
            reason="invalid_user_id", cursor=page.next_cursor, limit=1
        )
        self.assertEqual(ids(page), [0])
        self.assertEqual(ids(rejected_requests.search(event_kind="unknown")), [3, 1])
        self.assertEqual(ids(rejected_requests.search(user_id="abc")), [0])
        self.assertEqual(ids(rejected_requests.search(topic="other")), [])
        windowed = rejected_requests.search(
            start=self.now + timedelta(minutes=1), end=self.now + timedelta(minutes=3)
        )
        self.assertEqual(ids(windowed), [2, 1])

    def test_postgres_table_matches_migration(self):
        ddl = str(CreateTable(RejectedRequest.__table__).compile(dialect=postgresql.dialect()))
        self.assertIn("error_details JSONB NOT NULL", ddl)
        self.assertIn("offset_id BIGINT NOT NULL", ddl)
        sql = add_rejected_requests_schema.SQL_PATH.read_text()
        for column in RejectedRequest.__table__.columns:
            self.assertIn(f"    {column.name} ", sql)
        for index in RejectedRequest.__table__.indexes:
            self.assertIn(index.name, sql)
        for constraint in RejectedRequest.__table__.constraints:
            if constraint.name:
                self.assertIn(constraint.name, sql)


class RejectedRequestMigrationTests(unittest.TestCase):
    def test_requires_postgres(self):
        engine = Mock()
        engine.dialect.name = "sqlite"
        with self.assertRaisesRegex(ValueError, "PostgreSQL"):
            add_rejected_requests_schema.migrate(engine)

    def test_runs_every_statement_in_one_transaction(self):
        engine = MagicMock()
        engine.dialect.name = "postgresql"
        connection = engine.begin.return_value.__enter__.return_value
        add_rejected_requests_schema.migrate(engine)

        executed = [call.args[0] for call in connection.exec_driver_sql.call_args_list]
        self.assertEqual(engine.begin.call_count, 1)
        self.assertTrue(executed[0].strip().endswith("lock_timeout = '5s';"))
        self.assertIn("pg_advisory_xact_lock", executed[2])
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS rejected_requests" in s for s in executed))
        self.assertEqual(Path(add_rejected_requests_schema.SQL_PATH).suffix, ".sql")


if __name__ == "__main__":
    unittest.main()
