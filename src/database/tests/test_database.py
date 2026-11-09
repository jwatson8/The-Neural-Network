import gzip
import inspect
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import BigInteger, inspect as sqlalchemy_inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateTable
from sqlmodel import Session, SQLModel, create_engine, select

from src.database.database import get_database_url
from src.database.interactions import (
    RecordNotFoundError,
    cold_start_jobs,
    cold_start_profiles,
    course_requests,
    database_summary,
    licensing_costs,
    llm_usage,
    model_logs,
    movies,
    recommendation_serves,
    stream_events,
    user_movie_history,
    users,
)
from src.database.models import (
    CourseRequest,
    LicensingCost,
    StreamEvent,
    User,
    UserMovieHistory,
)
from src.database.roll_up import roll_up
from src.database.schemas import StreamEventCreate, UserCreate


class DatabaseInteractionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.engine_patch = patch(
            "src.database.database.get_engine", return_value=self.engine
        )
        self.engine_patch.start()
        self.addCleanup(self.engine_patch.stop)
        self.addCleanup(self.engine.dispose)
        self.now = datetime.now(UTC)

    def seed_catalog(self):
        users.create(user_id="user-1", age=34)
        return movies.create(
            movie_id="movie-1",
            title="Initial title",
            genres="Drama|Romance",
            license_cost=Decimal("0.125"),
        )

    def test_schema_and_database_url_configuration(self):
        inspector = sqlalchemy_inspect(self.engine)
        self.assertEqual(len(inspector.get_table_names()), 13)
        self.assertEqual(
            inspector.get_pk_constraint("user_movie_history")["constrained_columns"],
            ["user_id", "movie_id"],
        )
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                get_database_url()
        with patch.dict(
            os.environ,
            {"DATABASE_PASSWORD": "special@:/# password"},
            clear=True,
        ):
            self.assertEqual(get_database_url().password, "special@:/# password")

    def test_public_crud_owns_transactions_and_returns_loaded_records(self):
        movie = self.seed_catalog()
        event = stream_events.create(
            user_id="user-1",
            movie_id="movie-1",
            event_type="rating",
            event_time=self.now,
            rating=8,
        )
        movies.update("movie-1", title="Updated", overview=None)

        self.assertEqual(movie.title, "Initial title")
        self.assertEqual(event.rating, 8)
        self.assertEqual(movies.get("movie-1").title, "Updated")
        self.assertEqual(
            user_movie_history.get("user-1", "movie-1").latest_rating, 8
        )
        with self.assertRaises(RecordNotFoundError):
            users.update("missing", age=20)

    def test_get_user_history_returns_only_requested_users_rows(self):
        self.seed_catalog()
        movies.create(movie_id="movie-2", title="Second movie")
        movies.create(movie_id="movie-3", title="Third movie")
        users.create(user_id="user-2")
        stream_events.create(
            user_id="user-1",
            movie_id="movie-2",
            event_type="rating",
            event_time=self.now,
            rating=7,
        )
        stream_events.create(
            user_id="user-1",
            movie_id="movie-1",
            event_type="rating",
            event_time=self.now,
            rating=9,
        )
        stream_events.create(
            user_id="user-2",
            movie_id="movie-3",
            event_type="rating",
            event_time=self.now,
            rating=5,
        )

        history = user_movie_history.get_user_history("user-1")

        self.assertEqual([row.movie_id for row in history], ["movie-1", "movie-2"])
        self.assertEqual([row.latest_rating for row in history], [9, 7])
        self.assertTrue(all(row.user_id == "user-1" for row in history))
        self.assertEqual(user_movie_history.get_user_history("missing"), [])

    def test_history_tracks_latest_watch_rating_and_rating_change(self):
        self.seed_catalog()
        start = datetime(2026, 1, 1, tzinfo=UTC)

        stream_events.create(
            user_id="user-1",
            movie_id="movie-1",
            event_type="watch",
            event_time=start,
            minute_index=12,
        )
        watch = user_movie_history.get("user-1", "movie-1")
        stream_events.create(
            user_id="user-1",
            movie_id="movie-1",
            event_type="rating",
            rating=7,
            event_time=start + timedelta(minutes=1),
        )
        first_rating = user_movie_history.get("user-1", "movie-1")
        stream_events.create(
            user_id="user-1",
            movie_id="movie-1",
            event_type="rating",
            rating=7,
            event_time=start + timedelta(minutes=2),
        )
        same_rating = user_movie_history.get("user-1", "movie-1")
        stream_events.create(
            user_id="user-1",
            movie_id="movie-1",
            event_type="rating",
            rating=9,
            event_time=start + timedelta(minutes=3),
        )
        changed_rating = user_movie_history.get("user-1", "movie-1")
        stream_events.create(
            user_id="user-1",
            movie_id="movie-1",
            event_type="watch",
            event_time=start + timedelta(minutes=4),
        )
        stream_events.create(
            user_id="user-1",
            movie_id="movie-1",
            event_type="rating",
            event_time=start - timedelta(minutes=1),
            rating=2,
        )
        stream_events.create(
            user_id="user-1",
            movie_id="movie-1",
            event_type="rating",
            event_time=start + timedelta(minutes=3),
            rating=4,
        )
        history = user_movie_history.get("user-1", "movie-1")

        self.assertIsNone(watch.latest_rating)
        self.assertFalse(first_rating.rating_changed)
        self.assertFalse(same_rating.rating_changed)
        self.assertTrue(changed_rating.rating_changed)
        self.assertEqual(history.latest_rating, 9)
        self.assertTrue(history.rating_changed)
        self.assertEqual(
            history.last_recorded_watch_at.replace(tzinfo=UTC),
            start + timedelta(minutes=4),
        )
        self.assertEqual(
            history.last_recorded_rating_at.replace(tzinfo=UTC),
            start + timedelta(minutes=3),
        )
        self.assertEqual(len(stream_events.list_records()), 7)

    def test_history_batch_is_ordered_and_duplicate_events_are_ignored(self):
        self.seed_catalog()
        start = datetime(2026, 1, 1, tzinfo=UTC)
        records = [
            {
                "event_id": uuid4(),
                "user_id": "user-1",
                "movie_id": "movie-1",
                "event_type": "rating",
                "event_time": start + timedelta(minutes=3),
                "rating": 9,
            },
            {
                "event_id": uuid4(),
                "user_id": "user-1",
                "movie_id": "movie-1",
                "event_type": "watch",
                "event_time": start + timedelta(minutes=2),
            },
            {
                "event_id": uuid4(),
                "user_id": "user-1",
                "movie_id": "movie-1",
                "event_type": "rating",
                "event_time": start + timedelta(minutes=1),
                "rating": 5,
            },
        ]

        self.assertEqual(stream_events.create_many(records), 3)
        self.assertEqual(stream_events.create_many(records), 0)
        history = user_movie_history.get("user-1", "movie-1")

        self.assertEqual(history.latest_rating, 9)
        self.assertTrue(history.rating_changed)
        self.assertEqual(
            history.last_recorded_watch_at.replace(tzinfo=UTC),
            start + timedelta(minutes=2),
        )

        movies.create(movie_id="movie-2", title="Second movie")
        duplicate_id = uuid4()
        duplicate_records = [
            {
                "event_id": duplicate_id,
                "user_id": "user-1",
                "movie_id": "movie-2",
                "event_type": "rating",
                "event_time": start,
                "rating": 4,
            },
            {
                "event_id": duplicate_id,
                "user_id": "user-1",
                "movie_id": "movie-2",
                "event_type": "rating",
                "event_time": start + timedelta(minutes=5),
                "rating": 10,
            },
        ]
        self.assertEqual(stream_events.create_many(duplicate_records), 1)
        self.assertEqual(
            user_movie_history.get("user-1", "movie-2").latest_rating, 4
        )

    def test_watch_charges_use_a_sliding_24_hour_inactivity_window(self):
        self.seed_catalog()
        start = datetime(2026, 1, 1, tzinfo=UTC)

        first_event = stream_events.create(
            user_id="user-1",
            movie_id="movie-1",
            event_type="watch",
            event_time=start,
        )
        stream_events.create(
            user_id="user-1",
            movie_id="movie-1",
            event_type="watch",
            event_time=start + timedelta(hours=12),
        )
        stream_events.create(
            user_id="user-1",
            movie_id="movie-1",
            event_type="watch",
            event_time=start + timedelta(hours=24),
        )
        boundary_event = stream_events.create(
            user_id="user-1",
            movie_id="movie-1",
            event_type="watch",
            event_time=start + timedelta(hours=48),
        )
        stream_events.create(
            user_id="user-1",
            movie_id="movie-1",
            event_type="watch",
            event_time=start + timedelta(hours=47),
        )

        charges = licensing_costs.list_records()
        self.assertEqual(len(charges), 2)
        self.assertEqual(
            {charge.source_event_id for charge in charges},
            {first_event.event_id, boundary_event.event_id},
        )
        self.assertTrue(
            all(charge.license_cost == Decimal("0.125") for charge in charges)
        )

    def test_charges_snapshot_cost_and_preserve_missing_cost(self):
        self.seed_catalog()
        movies.create(movie_id="movie-2", title="No cost")
        start = datetime(2026, 1, 1, tzinfo=UTC)

        stream_events.create(
            user_id="user-1",
            movie_id="movie-1",
            event_type="watch",
            event_time=start,
        )
        movies.update("movie-1", license_cost=Decimal("0.5"))
        stream_events.create(
            user_id="user-1",
            movie_id="movie-1",
            event_type="watch",
            event_time=start + timedelta(hours=24),
        )
        stream_events.create(
            user_id="user-1",
            movie_id="movie-2",
            event_type="watch",
            event_time=start,
        )

        movie_one_charges = licensing_costs.search(
            user_id="user-1", movie_id="movie-1"
        ).records
        movie_two_charge = licensing_costs.search(movie_id="movie-2").records[0]
        self.assertEqual(
            [charge.license_cost for charge in movie_one_charges],
            [Decimal("0.5"), Decimal("0.125")],
        )
        self.assertIsNone(movie_two_charge.license_cost)

    def test_only_new_watch_events_create_charges(self):
        self.seed_catalog()
        start = datetime(2026, 1, 1, tzinfo=UTC)
        watch_id = uuid4()
        events = [
            {
                "event_id": watch_id,
                "user_id": "user-1",
                "movie_id": "movie-1",
                "event_type": "watch",
                "event_time": start,
            },
            {
                "event_id": uuid4(),
                "user_id": "user-1",
                "movie_id": "movie-1",
                "event_type": "rating",
                "event_time": start + timedelta(hours=1),
                "rating": 8,
            },
        ]

        self.assertEqual(stream_events.create_many(reversed(events)), 2)
        self.assertEqual(stream_events.create_many(events), 0)
        charges = licensing_costs.list_records()
        self.assertEqual(len(charges), 1)
        self.assertEqual(charges[0].source_event_id, watch_id)

    def test_stream_event_and_history_update_roll_back_together(self):
        self.seed_catalog()
        event_id = uuid4()
        with patch(
            "src.database.interactions.user_movie_history._apply_event_to_history",
            side_effect=RuntimeError("history unavailable"),
        ):
            with self.assertRaises(RuntimeError):
                stream_events.create(
                    event_id=event_id,
                    user_id="user-1",
                    movie_id="movie-1",
                    event_type="watch",
                    event_time=self.now,
                )

        self.assertIsNone(stream_events.get(event_id))
        self.assertIsNone(user_movie_history.get("user-1", "movie-1"))

    def test_charge_failure_rolls_back_event_and_history(self):
        self.seed_catalog()
        event_id = uuid4()
        with patch(
            "src.database.interactions.licensing_costs._record_watch_charge",
            side_effect=RuntimeError("charge unavailable"),
        ):
            with self.assertRaises(RuntimeError):
                stream_events.create(
                    event_id=event_id,
                    user_id="user-1",
                    movie_id="movie-1",
                    event_type="watch",
                    event_time=self.now,
                )

        self.assertIsNone(stream_events.get(event_id))
        self.assertIsNone(user_movie_history.get("user-1", "movie-1"))
        self.assertEqual(licensing_costs.list_records(), [])

    def test_history_database_constraints_reject_empty_activity(self):
        with Session(self.engine) as session:
            session.add(UserMovieHistory(user_id="user-1", movie_id="movie-1"))
            with self.assertRaises(IntegrityError):
                session.commit()

    def test_history_postgres_ddl_uses_event_timestamps(self):
        ddl = str(
            CreateTable(UserMovieHistory.__table__).compile(
                dialect=postgresql.dialect()
            )
        )

        self.assertIn("last_recorded_watch_at TIMESTAMP WITH TIME ZONE", ddl)
        self.assertIn("last_recorded_rating_at TIMESTAMP WITH TIME ZONE", ddl)
        self.assertIn("ck_history_rating_state", ddl)
        self.assertNotIn("stored_at", ddl)

    def test_licensing_cost_postgres_ddl_uses_charge_timestamp(self):
        ddl = str(
            CreateTable(LicensingCost.__table__).compile(
                dialect=postgresql.dialect()
            )
        )

        self.assertIn("charged_at TIMESTAMP WITH TIME ZONE", ddl)
        self.assertIn("UNIQUE (source_event_id)", ddl)
        self.assertIn("ck_licensing_cost_non_negative", ddl)

    def test_raw_csv_event_normalization_and_validation(self):
        event = StreamEventCreate.model_validate(
            {
                "timestamp": "2025-01-01T00:00:00",
                "user_id": 1,
                "event_type": "watch",
                "movie_id": "jumanji+1995",
                "rating": "",
            }
        )
        self.assertEqual(event.user_id, "1")
        self.assertEqual(event.event_time.tzinfo, UTC)
        with self.assertRaises(ValueError):
            StreamEventCreate(
                user_id="1",
                event_type="rating",
                movie_id="movie-1",
                event_time=self.now,
                rating=None,
            )

    def test_audit_searches_filter_time_and_fields(self):
        self.seed_catalog()
        start = datetime(2026, 1, 1, tzinfo=UTC)

        matching_usage = llm_usage.create(
            user_id="user-1",
            occurred_at=start,
            provider="openai",
            model_name="model-a",
            status="succeeded",
        )
        llm_usage.create(
            user_id="user-1",
            occurred_at=start + timedelta(hours=1),
            provider="openai",
            model_name="model-a",
            status="failed",
        )
        llm_usage.create(
            occurred_at=start + timedelta(hours=2),
            provider="other",
            model_name="model-b",
            status="succeeded",
        )

        matching_serve = recommendation_serves.create(
            user_id="user-1",
            request_received_at=start,
            served_at=start + timedelta(seconds=1),
            serve_duration_ms=1000,
            model_route="llm_content",
            model_version="m1",
            recommendations=[{"movie_id": "movie-1"}],
        )
        recommendation_serves.create(
            user_id="user-1",
            request_received_at=start + timedelta(hours=1),
            served_at=start + timedelta(hours=1, seconds=1),
            serve_duration_ms=1000,
            model_route="collaborative_svd",
            model_version="m2",
            recommendations=[{"movie_id": "movie-1"}],
        )

        usage_page = llm_usage.search(
            start=start.astimezone(timezone(timedelta(hours=-6))),
            end=start + timedelta(hours=2),
            user_id="user-1",
            provider="openai",
            model_name="model-a",
            status="succeeded",
        )
        serve_page = recommendation_serves.search(
            start=start,
            end=start + timedelta(hours=2),
            user_id="user-1",
            model_route="llm_content",
            model_version="m1",
        )

        self.assertEqual(
            [record.request_id for record in usage_page.records],
            [matching_usage.request_id],
        )
        self.assertIsNone(usage_page.next_cursor)
        self.assertEqual(
            [record.request_id for record in serve_page.records],
            [matching_serve.request_id],
        )

    def test_stream_event_search_uses_cursor_and_selected_time_basis(self):
        self.seed_catalog()
        event_day = datetime(2026, 1, 1, tzinfo=UTC)
        storage_day = event_day + timedelta(days=1)
        events = [
            stream_events.create(
                user_id="user-1",
                movie_id="movie-1",
                event_type="watch",
                event_time=event_day,
            ),
            stream_events.create(
                user_id="user-1",
                movie_id="movie-1",
                event_type="rating",
                event_time=event_day + timedelta(hours=1),
                rating=8,
            ),
            stream_events.create(
                user_id="user-1",
                event_type="account_created",
                event_time=event_day + timedelta(hours=2),
            ),
        ]
        with Session(self.engine) as session:
            for index, event in enumerate(events):
                stored_event = session.get(StreamEvent, event.event_id)
                stored_event.stored_at = storage_day + timedelta(hours=index)
                session.add(stored_event)
            session.commit()

        seen_ids = []
        cursor = None
        while True:
            page = stream_events.search(
                start=event_day,
                end=event_day + timedelta(days=1),
                user_id="user-1",
                cursor=cursor,
                limit=1,
            )
            seen_ids.extend(record.event_id for record in page.records)
            cursor = page.next_cursor
            if cursor is None:
                break

        watch_page = stream_events.search(
            start=event_day,
            end=event_day + timedelta(days=1),
            event_type="watch",
            movie_id="movie-1",
        )
        stored_page = stream_events.search(
            start=storage_day,
            end=storage_day + timedelta(days=1),
            time_basis="stored_at",
        )
        prior_storage_page = stream_events.search(
            start=event_day,
            end=storage_day,
            time_basis="stored_at",
        )

        self.assertEqual(seen_ids, [event.event_id for event in reversed(events)])
        self.assertEqual(
            [record.event_id for record in watch_page.records], [events[0].event_id]
        )
        self.assertEqual(
            [record.event_id for record in stored_page.records],
            [event.event_id for event in reversed(events)],
        )
        self.assertEqual(prior_storage_page.records, [])

    def test_audit_searches_reject_invalid_windows_limits_and_cursors(self):
        self.seed_catalog()
        start = datetime(2026, 1, 1, tzinfo=UTC)
        for hour in range(2):
            stream_events.create(
                user_id="user-1",
                movie_id="movie-1",
                event_type="watch",
                event_time=start + timedelta(hours=hour),
            )

        event_page = stream_events.search(limit=1)
        self.assertIsNotNone(event_page.next_cursor)
        with self.assertRaisesRegex(ValueError, "start must include a timezone"):
            llm_usage.search(start=start.replace(tzinfo=None))
        with self.assertRaisesRegex(ValueError, "end must be later than start"):
            recommendation_serves.search(start=start, end=start)
        with self.assertRaisesRegex(ValueError, "limit must be between"):
            stream_events.search(limit=0)
        with self.assertRaisesRegex(ValueError, "limit must be between"):
            stream_events.search(limit=1001)
        with self.assertRaisesRegex(ValueError, "time_basis must be"):
            stream_events.search(time_basis="invalid")
        with self.assertRaisesRegex(ValueError, "Cursor scope must be"):
            llm_usage.search(cursor=event_page.next_cursor)

    def test_create_missing_batches_preserve_existing_records(self):
        self.seed_catalog()

        skipped_user_ids = users.create_missing_many(
            [
                {"user_id": "user-1", "age": 99},
                {"user_id": "user-2", "age": 20},
            ]
        )
        skipped_movie_ids = movies.create_missing_many(
            [
                {"movie_id": "movie-1", "title": "Replacement title"},
                {"movie_id": "movie-2", "title": "New title"},
            ]
        )

        self.assertEqual(skipped_user_ids, ["user-1"])
        self.assertEqual(skipped_movie_ids, ["movie-1"])
        self.assertEqual(users.get("user-1").age, 34)
        self.assertEqual(users.get("user-2").age, 20)
        self.assertEqual(movies.get("movie-1").title, "Initial title")
        self.assertEqual(movies.get("movie-2").title, "New title")
        self.assertEqual(users.create_missing_many([]), [])

    def test_course_requests_are_deduplicated_by_kafka_offset(self):
        record = {
            "topic": "course-logs",
            "partition_id": 2,
            "offset_id": 2_147_483_648,
            "raw_line": "2026-10-11T12:00:00Z,501,GET /create_account",
        }

        self.assertIsInstance(CourseRequest.__table__.c.offset_id.type, BigInteger)
        self.assertTrue(course_requests.create(**record))
        self.assertFalse(course_requests.create(**record))
        self.assertEqual(
            len(course_requests.list_records(limit=10)),
            1,
        )

    def test_database_summary_counts_all_tables(self):
        self.seed_catalog()
        counts = database_summary.counts()

        self.assertEqual(counts["users"], 1)
        self.assertEqual(counts["movies"], 1)
        self.assertEqual(set(counts), set(SQLModel.metadata.tables))

    def test_create_missing_batches_reject_invalid_or_duplicate_ids_atomically(self):
        with self.assertRaisesRegex(ValueError, "Duplicate user_id values: duplicate"):
            users.create_missing_many(
                [
                    {"user_id": "new-user"},
                    {"user_id": "duplicate"},
                    {"user_id": "duplicate"},
                ]
            )
        self.assertIsNone(users.get("new-user"))

        with self.assertRaises(ValueError):
            users.create_missing_many(
                [{"user_id": "valid-user"}, {"user_id": "", "age": -1}]
            )
        self.assertIsNone(users.get("valid-user"))

    def test_create_missing_batches_insert_in_chunks(self):
        records = [{"user_id": f"bulk-{index}"} for index in range(1001)]

        self.assertEqual(users.create_missing_many(records), [])
        self.assertEqual(
            users.create_missing_many(records),
            [record["user_id"] for record in records],
        )

    def test_audits_and_cold_start_lifecycle(self):
        self.seed_catalog()
        served = recommendation_serves.create(
            user_id="user-1",
            request_received_at=self.now,
            served_at=self.now + timedelta(milliseconds=12),
            serve_duration_ms=12,
            model_route="collaborative_svd",
            model_version="m1",
            recommendations=[{"movie_id": "movie-1", "model_score": 0.42}],
        )
        usage = llm_usage.create(
            user_id="user-1",
            provider="openai",
            model_name="test-model",
            status="succeeded",
        )
        cold_start_jobs.enqueue(user_id="user-1")
        self.assertIsNotNone(cold_start_jobs.get("user-1"))
        claimed = cold_start_jobs.claim(lease_seconds=30)
        cold_start_profiles.complete_generation(
            user_id="user-1",
            positive="space",
            llm_request_id=usage.request_id,
        )
        cold_start_profiles.replace(user_id="user-1", positive="mystery")
        self.assertFalse(cold_start_jobs.complete("user-1"))
        self.assertEqual(served.recommendations[0]["movie_id"], "movie-1")
        self.assertEqual(claimed.attempts, 1)
        self.assertEqual(cold_start_profiles.get("user-1").positive, "mystery")

    def test_recommendation_duration_can_include_audit_persistence(self):
        users.create(user_id="timed-user")

        with patch(
            "src.database.interactions.recommendation_serves.time.perf_counter",
            side_effect=[10.75],
        ):
            record = recommendation_serves.create(
                user_id="timed-user",
                served_at=self.now,
                model_route="popularity_fallback",
                model_version="test",
                recommendations=[{"movie_id": "movie-1"}],
                request_received_at=self.now,
                serve_duration_ms=0,
                request_started_at=10.0,
            )

        self.assertAlmostEqual(record.serve_duration_ms, 750)

    def test_low_level_session_supports_atomic_rollback(self):
        with Session(self.engine) as session:
            users._create(session, UserCreate(user_id="rolled-back"))
            session.rollback()
        with Session(self.engine) as session:
            self.assertIsNone(session.get(User, "rolled-back"))

    def test_public_functions_are_documented_and_hide_sessions(self):
        modules = (
            users, movies, stream_events, user_movie_history, licensing_costs,
            recommendation_serves, llm_usage, model_logs, cold_start_profiles,
            cold_start_jobs, course_requests, database_summary,
        )
        functions = [getattr(module, name) for module in modules for name in module.__all__]
        for function in functions:
            with self.subTest(function=function.__qualname__):
                self.assertTrue(inspect.getdoc(function))
                self.assertNotIn("session", inspect.signature(function).parameters)

    def test_roll_up_is_idempotent_and_derives_history(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "users.csv").write_text(
                "user_id,age,occupation,gender,self_description_likes,self_description_dislikes\n"
                "1,34,programmer,M,space,\n",
                encoding="utf-8",
            )
            with gzip.open(data_dir / "movies.csv.gz", "wt", encoding="utf-8") as stream:
                stream.write(
                    "movie_id,title,genres,release_date,runtime,original_language,overview,"
                    "popularity,vote_average,vote_count,imdb_id,tmdb_id,license_cost\n"
                    "movie-1,Movie,Drama,2020-01-01,90,en,Story,1.2,7.0,10,tt1,1,0.0068199066\n"
                )
            (data_dir / "events.csv").write_text(
                "timestamp,user_id,event_type,movie_id,rating\n"
                "2025-01-01T00:00:00,1,watch,movie-1,\n"
                "2025-01-01T00:01:00,1,rating,movie-1,8\n",
                encoding="utf-8",
            )
            first = roll_up(data_dir, chunk_size=1)
            second = roll_up(data_dir, chunk_size=1)

        self.assertEqual(first["users"], {"created": 1, "skipped_ids": []})
        self.assertEqual(first["movies"], {"created": 1, "skipped_ids": []})
        self.assertEqual(
            second["users"], {"created": 0, "skipped_ids": ["1"]}
        )
        self.assertEqual(
            second["movies"], {"created": 0, "skipped_ids": ["movie-1"]}
        )
        self.assertEqual(first["events_inserted"], 2)
        self.assertEqual(second["events_inserted"], 0)
        history = user_movie_history.get("1", "movie-1")
        self.assertEqual(history.latest_rating, 8)
        self.assertFalse(history.rating_changed)
        self.assertEqual(
            history.last_recorded_watch_at, datetime(2025, 1, 1, 0, 0)
        )
        self.assertEqual(
            history.last_recorded_rating_at, datetime(2025, 1, 1, 0, 1)
        )
        with Session(self.engine) as session:
            self.assertEqual(len(session.exec(select(StreamEvent)).all()), 2)
            charges = list(session.exec(select(LicensingCost)).all())
            self.assertEqual(len(charges), 1)
            self.assertEqual(charges[0].license_cost, Decimal("0.0068199066"))


if __name__ == "__main__":
    unittest.main()
