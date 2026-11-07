import gzip
import inspect
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from src.database.database import get_database_url
from src.database.interactions import (
    RecordNotFoundError,
    cold_start_jobs,
    cold_start_profiles,
    llm_usage,
    movies,
    recommendation_serves,
    stream_events,
    user_movie_history,
    users,
)
from src.database.models import StreamEvent, User
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
        )

    def test_schema_and_database_url_configuration(self):
        inspector = sqlalchemy_inspect(self.engine)
        self.assertEqual(len(inspector.get_table_names()), 9)
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
        user_movie_history.upsert(
            user_id="user-1", movie_id="movie-1", latest_rating=8
        )

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
        user_movie_history.upsert(
            user_id="user-1", movie_id="movie-2", latest_rating=7
        )
        user_movie_history.upsert(
            user_id="user-1", movie_id="movie-1", latest_rating=9
        )
        user_movie_history.upsert(
            user_id="user-2", movie_id="movie-3", latest_rating=5
        )

        history = user_movie_history.get_user_history("user-1")

        self.assertEqual([row.movie_id for row in history], ["movie-1", "movie-2"])
        self.assertEqual([row.latest_rating for row in history], [9, 7])
        self.assertTrue(all(row.user_id == "user-1" for row in history))
        self.assertEqual(user_movie_history.get_user_history("missing"), [])

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

    def test_low_level_session_supports_atomic_rollback(self):
        with Session(self.engine) as session:
            users._create(session, UserCreate(user_id="rolled-back"))
            session.rollback()
        with Session(self.engine) as session:
            self.assertIsNone(session.get(User, "rolled-back"))

    def test_public_functions_are_documented_and_hide_sessions(self):
        modules = (
            users, movies, stream_events, user_movie_history,
            recommendation_serves, llm_usage, cold_start_profiles,
            cold_start_jobs,
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
                    "movie-1,Movie,Drama,2020-01-01,90,en,Story,1.2,7.0,10,tt1,1,0.1\n"
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
        self.assertEqual(user_movie_history.get("1", "movie-1").latest_rating, 8)
        with Session(self.engine) as session:
            self.assertEqual(len(session.exec(select(StreamEvent)).all()), 2)


if __name__ == "__main__":
    unittest.main()
