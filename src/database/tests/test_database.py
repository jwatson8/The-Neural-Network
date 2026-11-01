import gzip
import inspect
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
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
        self.assertEqual(len(inspector.get_table_names()), 8)
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

    def test_audits_and_cold_start_lifecycle(self):
        self.seed_catalog()
        served = recommendation_serves.create(
            user_id="user-1",
            request_received_at=self.now,
            served_at=self.now + timedelta(milliseconds=12),
            serve_duration_ms=12,
            model_route="collaborative_svd",
            model_version="m1",
            recommendations=[{"movie_id": "movie-1", "model_score": 4.2}],
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
        self.assertFalse(cold_start_jobs.complete("user-1"))
        self.assertEqual(served.recommendations[0]["movie_id"], "movie-1")
        self.assertEqual(claimed.attempts, 1)
        self.assertEqual(cold_start_profiles.get("user-1").positive, "space")

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

        self.assertEqual(first["events_inserted"], 2)
        self.assertEqual(second["events_inserted"], 0)
        self.assertEqual(user_movie_history.get("1", "movie-1").latest_rating, 8)
        with Session(self.engine) as session:
            self.assertEqual(len(session.exec(select(StreamEvent)).all()), 2)


if __name__ == "__main__":
    unittest.main()
