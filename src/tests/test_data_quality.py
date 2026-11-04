"""Offline tests for stream events at the data quality boundary."""

import json
import unittest
from datetime import datetime, timezone

from src.data_quality.boundary import validate_stream_event
from src.database.schemas import StreamEventCreate


class StreamEventValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.event = {
            "user_id": "test-user",
            "event_type": "rating",
            "movie_id": "test-movie",
            "rating": 7,
            "timestamp": "2026-01-02T03:04:05Z",
        }

    def assert_valid(self, raw: dict, **expected: object) -> StreamEventCreate:
        event, errors = validate_stream_event(raw)
        self.assertEqual(errors, [])
        self.assertIsInstance(event, StreamEventCreate)
        self.assertEqual(event.user_id, "test-user")
        self.assertEqual(
            event.event_time, datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        )
        for field, value in expected.items():
            self.assertEqual(getattr(event, field), value)
        return event

    def assert_invalid(self, raw: dict, location: tuple, error_type: str) -> None:
        event, errors = validate_stream_event(raw)
        self.assertIsNone(event)
        self.assertIsInstance(errors, list)
        self.assertTrue(errors)
        self.assertIn(
            (location, error_type), [(error["loc"], error["type"]) for error in errors]
        )
        for error in errors:
            self.assertEqual(set(error), {"loc", "type", "msg"})
            self.assertIsInstance(error["msg"], str)
            self.assertTrue(error["msg"])
        json.dumps(errors)  # Consumer logging must not encounter exception objects.

    def test_valid_ratings_include_both_bounds(self) -> None:
        for rating in (1, 7, 10):
            with self.subTest(rating=rating):
                self.assert_valid(
                    dict(self.event, rating=rating),
                    event_type="rating",
                    movie_id="test-movie",
                    rating=rating,
                )

    def test_rating_out_of_range(self) -> None:
        self.assert_invalid(dict(self.event, rating=99), ("rating",), "less_than_equal")

    def test_rating_requires_movie_id(self) -> None:
        del self.event["movie_id"]
        self.assert_invalid(self.event, (), "value_error")

    def test_user_id_is_required(self) -> None:
        del self.event["user_id"]
        self.assert_invalid(self.event, ("user_id",), "missing")

    def test_unknown_event_type(self) -> None:
        self.assert_invalid(
            dict(self.event, event_type="unknown"), ("event_type",), "literal_error"
        )

    def test_valid_watch_without_rating(self) -> None:
        del self.event["rating"]
        self.assert_valid(
            dict(self.event, event_type="watch"),
            event_type="watch",
            movie_id="test-movie",
            rating=None,
        )

    def test_watch_rejects_rating(self) -> None:
        self.assert_invalid(dict(self.event, event_type="watch"), (), "value_error")

    def test_valid_account_without_movie_or_rating(self) -> None:
        del self.event["movie_id"]
        del self.event["rating"]
        self.assert_valid(
            dict(self.event, event_type="account_created"),
            event_type="account_created",
            movie_id=None,
            rating=None,
        )

    def test_account_rejects_movie_or_rating(self) -> None:
        for fields in (
            {"movie_id": "test-movie"},
            {"rating": 7},
            {"movie_id": "test-movie", "rating": 7},
        ):
            with self.subTest(fields=fields):
                raw = {
                    "user_id": "test-user",
                    "event_type": "account_created",
                    "timestamp": self.event["timestamp"],
                    **fields,
                }
                self.assert_invalid(raw, (), "value_error")

    def test_timestamp_is_required(self) -> None:
        del self.event["timestamp"]
        self.assert_invalid(self.event, ("event_time",), "missing")

    def test_invalid_timestamps(self) -> None:
        for timestamp, error_type in (
            ("not-a-timestamp", "datetime_from_date_parsing"),
            (None, "datetime_type"),
        ):
            with self.subTest(timestamp=timestamp):
                self.assert_invalid(
                    dict(self.event, timestamp=timestamp), ("event_time",), error_type
                )

    def test_extra_fields_are_rejected(self) -> None:
        self.assert_invalid(
            dict(self.event, unexpected="sanitized"), ("unexpected",), "extra_forbidden"
        )

    def test_whitespace_is_stripped_from_identifiers(self) -> None:
        raw = dict(self.event, user_id="  test-user \t", movie_id="\t test-movie  ")
        self.assert_valid(raw, movie_id="test-movie", rating=7)
        self.assertEqual(raw["user_id"], "  test-user \t")

    def test_whitespace_only_identifiers_are_rejected(self) -> None:
        self.assert_invalid(
            dict(self.event, user_id=" \t "), ("user_id",), "string_too_short"
        )
        self.assert_invalid(dict(self.event, movie_id=" \t "), (), "value_error")

    def test_event_type_whitespace_is_not_normalized(self) -> None:
        self.assert_invalid(
            dict(self.event, event_type=" rating "), ("event_type",), "literal_error"
        )

    def test_event_time_field_and_utc_normalization(self) -> None:
        del self.event["timestamp"]
        for timestamp in ("2026-01-02T03:04:05", "2026-01-02T05:04:05+02:00"):
            with self.subTest(timestamp=timestamp):
                self.assert_valid(dict(self.event, event_time=timestamp), rating=7)

    def test_conflicting_timestamp_fields_are_rejected(self) -> None:
        self.assert_invalid(
            dict(self.event, event_time="2026-01-03T03:04:05Z"), (), "value_error"
        )

    def test_null_user_id_is_rejected(self) -> None:
        self.assert_invalid(
            dict(self.event, user_id=None),
            (),
            "value_error",
        )


if __name__ == "__main__":
    unittest.main()
