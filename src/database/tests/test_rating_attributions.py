"""Tests for bounded monitoring read models."""

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from src.database.interactions import rating_attributions
from src.database.models import Movie, RecommendationServe, StreamEvent, User


class RatingAttributionReadTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.engine_patch = patch.object(
            rating_attributions, "get_engine", return_value=self.engine
        )
        self.engine_patch.start()
        self.addCleanup(self.engine_patch.stop)
        self.start = datetime(2026, 1, 2, tzinfo=UTC)

    def test_matches_user_time_and_movie_inside_recommendation_json(self):
        with Session(self.engine) as session:
            session.add(User(user_id="user-1"))
            session.add(Movie(movie_id="movie-1", title="Movie"))
            session.add(
                RecommendationServe(
                    user_id="user-1",
                    served_at=self.start - timedelta(hours=24),
                    request_received_at=self.start - timedelta(hours=24),
                    serve_duration_ms=0,
                    model_route="test",
                    model_version="m1",
                    recommendations=[{"movie_id": "movie-1", "model_score": 0.5}],
                )
            )
            session.add(
                StreamEvent(
                    user_id="user-1",
                    event_type="rating",
                    movie_id="movie-1",
                    event_time=self.start,
                    rating=8,
                )
            )
            session.commit()

        rows = rating_attributions.search(
            start=self.start,
            end=self.start + timedelta(minutes=5),
            attribution_hours=24,
        )

        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].attributed)

    def test_rejects_wrong_movie_and_future_serve(self):
        with Session(self.engine) as session:
            session.add(User(user_id="user-1"))
            session.add_all(
                [
                    Movie(movie_id="movie-1", title="One"),
                    Movie(movie_id="movie-2", title="Two"),
                ]
            )
            session.add(
                RecommendationServe(
                    user_id="user-1",
                    served_at=self.start + timedelta(seconds=1),
                    request_received_at=self.start,
                    serve_duration_ms=1000,
                    model_route="test",
                    model_version="m1",
                    recommendations=[{"movie_id": "movie-2"}],
                )
            )
            session.add(
                StreamEvent(
                    user_id="user-1",
                    event_type="rating",
                    movie_id="movie-1",
                    event_time=self.start,
                    rating=8,
                )
            )
            session.commit()

        rows = rating_attributions.search(
            start=self.start,
            end=self.start + timedelta(minutes=5),
            attribution_hours=24,
        )

        self.assertFalse(rows[0].attributed)


if __name__ == "__main__":
    unittest.main()
