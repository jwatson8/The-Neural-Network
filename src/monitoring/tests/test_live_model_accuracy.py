"""Tests for live recommendation accuracy calculations."""

import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import call, patch

from src.monitoring.calculations import live_model_accuracy
from src.monitoring.schemas import EvaluationWindow, MonitoringConfig


def page(records=(), next_cursor=None):
    return SimpleNamespace(records=records, next_cursor=next_cursor)


def serve(user_id, served_at, *movie_ids):
    return SimpleNamespace(
        user_id=user_id,
        served_at=served_at,
        recommendations=[{"movie_id": movie_id} for movie_id in movie_ids],
    )


def rating(user_id, movie_id, event_time, value):
    return SimpleNamespace(
        user_id=user_id,
        movie_id=movie_id,
        event_time=event_time,
        rating=value,
    )


class LiveModelAccuracyTests(unittest.TestCase):
    def setUp(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        self.window = EvaluationWindow(start=start, end=start + timedelta(hours=4))
        self.config = MonitoringConfig(
            window_hours=4,
            attribution_hours=2,
            positive_rating_threshold=6,
            page_size=2,
        )

    def test_attributes_ratings_across_all_pages(self):
        recommendation_cursor = object()
        rating_cursor = object()
        rating_time = self.window.start + timedelta(hours=2)
        recommendation_pages = (
            page(
                (
                    serve("user-1", rating_time - timedelta(hours=2), "movie-1"),
                    serve("user-2", rating_time - timedelta(minutes=30), "movie-2"),
                ),
                recommendation_cursor,
            ),
            page(
                (
                    serve("user-2", rating_time, "movie-2"),
                    serve("user-3", rating_time + timedelta(minutes=1), "movie-3"),
                )
            ),
        )
        rating_pages = (
            page(
                (
                    rating("user-1", "movie-1", rating_time, 8),
                    rating("user-2", "movie-2", rating_time, 4),
                ),
                rating_cursor,
            ),
            page(
                (
                    rating("user-3", "movie-3", rating_time, 9),
                    rating("user-4", "movie-4", rating_time, 10),
                )
            ),
        )

        with patch.object(
            live_model_accuracy.recommendation_serves,
            "search",
            side_effect=recommendation_pages,
        ) as recommendation_search, patch.object(
            live_model_accuracy.stream_events,
            "search",
            side_effect=rating_pages,
        ) as rating_search:
            summary = live_model_accuracy.calculate_live_model_accuracy(
                self.window, self.config
            )

        self.assertEqual(summary.evaluated_rating_count, 4)
        self.assertEqual(summary.attributed_rating_count, 2)
        self.assertEqual(summary.positive_rating_count, 1)
        self.assertEqual(summary.accuracy, 0.5)
        self.assertEqual(
            recommendation_search.call_args_list,
            [
                call(
                    start=self.window.start - timedelta(hours=2),
                    end=self.window.end,
                    cursor=None,
                    limit=2,
                ),
                call(
                    start=self.window.start - timedelta(hours=2),
                    end=self.window.end,
                    cursor=recommendation_cursor,
                    limit=2,
                ),
            ],
        )
        self.assertEqual(
            rating_search.call_args_list,
            [
                call(
                    start=self.window.start,
                    end=self.window.end,
                    time_basis="event_time",
                    event_type="rating",
                    cursor=None,
                    limit=2,
                ),
                call(
                    start=self.window.start,
                    end=self.window.end,
                    time_basis="event_time",
                    event_type="rating",
                    cursor=rating_cursor,
                    limit=2,
                ),
            ],
        )

    def test_unattributed_rating_returns_no_accuracy(self):
        event = rating(
            "user-1",
            "movie-1",
            self.window.start + timedelta(hours=1),
            10,
        )

        with patch.object(
            live_model_accuracy.recommendation_serves,
            "search",
            return_value=page(),
        ), patch.object(
            live_model_accuracy.stream_events,
            "search",
            return_value=page((event,)),
        ):
            summary = live_model_accuracy.calculate_live_model_accuracy(
                self.window, self.config
            )

        self.assertEqual(summary.evaluated_rating_count, 1)
        self.assertEqual(summary.attributed_rating_count, 0)
        self.assertEqual(summary.positive_rating_count, 0)
        self.assertIsNone(summary.accuracy)


if __name__ == "__main__":
    unittest.main()
