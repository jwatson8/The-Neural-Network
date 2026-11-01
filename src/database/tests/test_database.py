import unittest

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from src.database.interaction import (
    create_movie,
    create_stream_event,
    create_user,
    get_stream_event,
    update_movie,
)
from src.database.schemas import MovieCreate, MovieUpdate, StreamEventCreate, UserCreate


class DatabaseInteractionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_create_read_and_update_records(self):
        with Session(self.engine) as session:
            create_user(session, UserCreate(user_id="user-1"))
            movie = create_movie(
                session,
                MovieCreate(movie_id="movie-1", title="Initial title", genres=["Drama"]),
            )
            event = create_stream_event(
                session,
                StreamEventCreate(
                    user_id="user-1",
                    movie_id="movie-1",
                    event_type="watch",
                ),
            )
            session.commit()

            update_movie(session, movie, MovieUpdate(title="Updated title"))
            session.commit()

            self.assertEqual(movie.title, "Updated title")
            self.assertEqual(get_stream_event(session, event.event_id).event_type, "watch")

    def test_schemas_reject_out_of_range_values(self):
        with self.assertRaises(ValueError):
            StreamEventCreate(user_id="user-1", event_type="rating", rating=6)


if __name__ == "__main__":
    unittest.main()
