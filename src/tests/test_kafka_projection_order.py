import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

from src.database.interactions import (
    licensing_costs,
    movies,
    stream_events,
    user_movie_history,
    users,
)
from src.service.store import ProfileStore


class KafkaProjectionOrderTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 1, 1, tzinfo=UTC)
        self.earlier_id = uuid4()
        self.later_id = uuid4()
        self.earlier = self.watch(self.earlier_id, self.start)
        self.later = self.watch(self.later_id, self.start + timedelta(hours=48))

    @staticmethod
    def watch(event_id, event_time):
        return {
            "event_id": event_id,
            "user_id": "user-1",
            "movie_id": "movie-1",
            "event_type": "watch",
            "event_time": event_time,
        }

    def database(self):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        event.listen(
            engine,
            "connect",
            lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"),
        )
        SQLModel.metadata.create_all(engine)
        return engine

    def kafka_projection(self, polls):
        engine = self.database()
        try:
            with patch("src.database.database.get_engine", return_value=engine):
                movies.create(
                    movie_id="movie-1",
                    title="Movie",
                    license_cost=Decimal("0.125"),
                )
                store = ProfileStore()
                for poll in polls:
                    store.record_batch([{"event": watch} for watch in poll], [])
                charges = licensing_costs.list_records()
                history = user_movie_history.get("user-1", "movie-1")
                return {
                    "charge_event_ids": {charge.source_event_id for charge in charges},
                    "last_watch_at": history.last_recorded_watch_at.replace(tzinfo=UTC),
                }
        finally:
            engine.dispose()

    def test_combined_split_and_replayed_polls_have_the_same_projection(self):
        combined = self.kafka_projection([[self.later, self.earlier]])
        split = self.kafka_projection([[self.later], [self.earlier]])
        replayed = self.kafka_projection(
            [[self.later, self.earlier], [self.later, self.earlier]]
        )

        expected = {
            "charge_event_ids": {self.later_id},
            "last_watch_at": self.start + timedelta(hours=48),
        }
        self.assertEqual(combined, expected)
        self.assertEqual(split, expected)
        self.assertEqual(replayed, expected)

    def test_default_projection_remains_chronological(self):
        engine = self.database()
        try:
            with patch("src.database.database.get_engine", return_value=engine):
                users.create(user_id="user-1")
                movies.create(movie_id="movie-1", title="Movie")

                stream_events.create_many([self.later, self.earlier])

                self.assertEqual(
                    {charge.source_event_id for charge in licensing_costs.list_records()},
                    {self.earlier_id, self.later_id},
                )
                with self.assertRaisesRegex(ValueError, "projection_order"):
                    stream_events.create_many([], projection_order="unknown")
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
