import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import inspect
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from src.database.interaction import (
    claim_cold_start_job,
    complete_cold_start_job,
    create_llm_usage,
    create_movie,
    create_recommendation_serve,
    create_stream_event,
    create_user,
    enqueue_cold_start_job,
    get_cold_start_profile,
    get_user_movie_history,
    retry_cold_start_job,
    update_movie,
    upsert_cold_start_profile,
    upsert_user_movie_history,
)
from src.database.schemas import (
    ColdStartJobCreate,
    ColdStartProfileUpsert,
    LLMUsageCreate,
    MovieCreate,
    MovieUpdate,
    RecommendationItem,
    RecommendationServeCreate,
    StreamEventCreate,
    UserCreate,
    UserMovieHistoryUpsert,
)


class DatabaseInteractionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.now = datetime.now(UTC)

    def tearDown(self):
        self.engine.dispose()

    def seed_user_and_movie(self, session):
        create_user(session, UserCreate(user_id="user-1", age=34))
        return create_movie(
            session,
            MovieCreate(
                movie_id="movie-1",
                title="Initial title",
                genres="Drama|Romance",
            ),
        )

    def test_schema_has_eight_tables_and_composite_history_key(self):
        inspector = inspect(self.engine)
        self.assertEqual(
            set(inspector.get_table_names()),
            {
                "cold_start_jobs",
                "cold_start_profiles",
                "llm_usage",
                "movies",
                "recommendation_serves",
                "stream_events",
                "user_movie_history",
                "users",
            },
        )
        history_pk = inspector.get_pk_constraint("user_movie_history")
        self.assertEqual(history_pk["constrained_columns"], ["user_id", "movie_id"])

    def test_create_update_and_history_upsert(self):
        with Session(self.engine) as session:
            movie = self.seed_user_and_movie(session)
            event = create_stream_event(
                session,
                StreamEventCreate(
                    user_id="user-1",
                    movie_id="movie-1",
                    event_type="rating",
                    event_time=self.now,
                    minute_index=42,
                    rating=8,
                ),
            )
            update_movie(session, movie, MovieUpdate(title="Updated title"))
            upsert_user_movie_history(
                session,
                UserMovieHistoryUpsert(
                    user_id="user-1",
                    movie_id="movie-1",
                    latest_rating=8,
                ),
            )
            upsert_user_movie_history(
                session,
                UserMovieHistoryUpsert(
                    user_id="user-1",
                    movie_id="movie-1",
                    latest_rating=9,
                ),
            )
            session.commit()

            history = get_user_movie_history(session, "user-1", "movie-1")
            self.assertEqual(movie.title, "Updated title")
            self.assertEqual(event.rating, 8)
            self.assertEqual(history.latest_rating, 9)

    def test_raw_csv_events_normalize_to_canonical_shapes(self):
        watch = StreamEventCreate.model_validate(
            {
                "timestamp": "2025-01-01T00:00:00",
                "user_id": 1,
                "event_type": "watch",
                "movie_id": "jumanji+1995",
                "rating": "",
            }
        )
        rating = StreamEventCreate.model_validate(
            {
                "timestamp": "2025-01-01T00:01:00",
                "user_id": "1",
                "event_type": "rating",
                "movie_id": "jumanji+1995",
                "rating": "7",
            }
        )
        account = StreamEventCreate.model_validate(
            {
                "timestamp": "2025-02-07T23:44:00",
                "user_id": "1070",
                "event_type": "account_created",
                "movie_id": "",
                "rating": float("nan"),
            }
        )

        self.assertEqual(watch.user_id, "1")
        self.assertEqual(watch.event_time.tzinfo, UTC)
        self.assertIsNone(watch.rating)
        self.assertEqual(rating.rating, 7)
        self.assertIsNone(account.movie_id)
        self.assertIsNone(account.rating)

    def test_timezone_aware_event_and_supplied_id_are_preserved(self):
        event_id = uuid4()
        event = StreamEventCreate(
            event_id=event_id,
            user_id="user-1",
            event_type="watch",
            movie_id="movie-1",
            event_time="2025-01-01T01:00:00+01:00",
        )
        self.assertEqual(event.event_id, event_id)
        self.assertEqual(event.event_time, datetime(2025, 1, 1, tzinfo=UTC))

    def test_recommendations_preserve_order_and_optional_scores(self):
        with Session(self.engine) as session:
            self.seed_user_and_movie(session)
            served = create_recommendation_serve(
                session,
                RecommendationServeCreate(
                    user_id="user-1",
                    request_received_at=self.now,
                    served_at=self.now + timedelta(milliseconds=12),
                    serve_duration_ms=12,
                    model_route="collaborative_svd",
                    model_version="m1",
                    recommendations=[
                        RecommendationItem(movie_id="movie-1", model_score=4.2),
                        RecommendationItem(movie_id="movie-2"),
                    ],
                ),
            )
            session.commit()

            self.assertEqual(
                served.recommendations,
                [
                    {"movie_id": "movie-1", "model_score": 4.2},
                    {"movie_id": "movie-2"},
                ],
            )

    def test_cold_start_profile_and_job_lifecycle(self):
        with Session(self.engine) as session:
            create_user(session, UserCreate(user_id="cold-user"))
            request_id = uuid4()
            create_llm_usage(
                session,
                LLMUsageCreate(
                    request_id=request_id,
                    user_id="cold-user",
                    provider="openai",
                    model_name="test-model",
                    status="succeeded",
                ),
            )
            enqueue_cold_start_job(session, ColdStartJobCreate(user_id="cold-user"))
            enqueue_cold_start_job(session, ColdStartJobCreate(user_id="cold-user"))
            job = claim_cold_start_job(session, lease_seconds=30)
            self.assertEqual(job.attempts, 1)
            self.assertEqual(job.status, "processing")

            retry_cold_start_job(session, job, "temporary failure")
            job.next_attempt_at = self.now - timedelta(seconds=1)
            session.flush()
            job = claim_cold_start_job(session, lease_seconds=30)
            self.assertEqual(job.attempts, 2)

            upsert_cold_start_profile(
                session,
                ColdStartProfileUpsert(
                    user_id="cold-user",
                    positive="space adventure",
                    negative="gore",
                    source_description_hash="a" * 64,
                    llm_request_id=request_id,
                ),
            )
            self.assertTrue(complete_cold_start_job(session, "cold-user"))
            session.commit()

            profile = get_cold_start_profile(session, "cold-user")
            self.assertEqual(profile.positive, "space adventure")
            self.assertIsNone(claim_cold_start_job(session))

    def test_validation_rejects_invalid_values(self):
        with self.assertRaises(ValueError):
            StreamEventCreate(
                user_id="user-1",
                event_type="rating",
                event_time=self.now,
                rating=11,
            )
        invalid_events = [
            {"event_type": "unknown", "movie_id": "movie-1"},
            {"event_type": "watch", "movie_id": None},
            {"event_type": "watch", "movie_id": "movie-1", "rating": 8},
            {"event_type": "rating", "movie_id": "movie-1", "rating": None},
            {"event_type": "account_created", "movie_id": "movie-1"},
        ]
        for values in invalid_events:
            with self.subTest(values=values), self.assertRaises(ValueError):
                StreamEventCreate(
                    user_id="user-1",
                    event_time=self.now,
                    **values,
                )
        with self.assertRaises(ValueError):
            StreamEventCreate.model_validate(
                {
                    "timestamp": "2025-01-01T00:00:00",
                    "event_time": "2025-01-01T00:01:00",
                    "user_id": "user-1",
                    "event_type": "watch",
                    "movie_id": "movie-1",
                }
            )
        with self.assertRaises(ValueError):
            RecommendationServeCreate(
                user_id="user-1",
                request_received_at=self.now,
                served_at=self.now,
                serve_duration_ms=0,
                model_route="fallback",
                model_version="m1",
                recommendations=[],
            )


if __name__ == "__main__":
    unittest.main()
