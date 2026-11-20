"""Behavior tests for rolling monitoring drift calculations."""

import unittest
from datetime import UTC, datetime, timedelta

from src.monitoring.calculations.data_drift import calculate_data_drift_buckets
from src.monitoring.schemas import EvaluationWindow, MonitoringConfig


class FakeEventReader:
    def __init__(self, events):
        self.events = events
        self.calls = []

    def read_events(self, **kwargs):
        self.calls.append(kwargs)
        return list(self.events)


class DataDriftCalculationTests(unittest.TestCase):
    def setUp(self):
        self.end = datetime(2026, 1, 1, 12, tzinfo=UTC)
        self.config = MonitoringConfig(
            backfill_hours=1,
            drift_min_samples=1,
        )

    def window(self, end=None):
        end = end or self.end
        return EvaluationWindow(start=end - timedelta(minutes=5), end=end)

    @staticmethod
    def event(event_time, event_type, rating=None):
        return {
            "event_time": event_time,
            "event_type": event_type,
            "rating": rating,
        }

    def test_half_open_boundaries_and_complete_rating_shift(self):
        baseline_start = self.end - timedelta(hours=2)
        recent_start = self.end - timedelta(hours=1)
        reader = FakeEventReader(
            [
                self.event(baseline_start, "rating", 1),
                self.event(recent_start, "rating", 10),
                self.event(self.end, "watch"),
            ]
        )

        summary = calculate_data_drift_buckets(
            [self.window()], self.config, reader=reader
        )[self.end - timedelta(minutes=5)]

        self.assertEqual(summary.rating_drift_tvd, 1)
        self.assertEqual(summary.event_type_drift_tvd, 0)
        self.assertEqual(summary.rating_drift_sample_count, 1)
        self.assertEqual(
            summary.event_type_baseline_counts,
            {"watch": 0, "rating": 1, "account_created": 0},
        )
        self.assertEqual(
            summary.event_type_recent_counts,
            summary.event_type_baseline_counts,
        )

    def test_empty_windows_have_null_scores_and_complete_zero_maps(self):
        summary = calculate_data_drift_buckets(
            [self.window()], self.config, reader=FakeEventReader([])
        )[self.end - timedelta(minutes=5)]

        self.assertIsNone(summary.rating_drift_tvd)
        self.assertIsNone(summary.event_type_drift_tvd)
        self.assertEqual(summary.rating_drift_sample_count, 0)
        self.assertEqual(
            summary.event_type_recent_counts,
            {"watch": 0, "rating": 0, "account_created": 0},
        )

    def test_event_type_shift_from_watch_to_rating_is_detected(self):
        reader = FakeEventReader(
            [
                self.event(self.end - timedelta(minutes=90), "watch"),
                self.event(self.end - timedelta(minutes=30), "rating", 5),
            ]
        )

        summary = calculate_data_drift_buckets(
            [self.window()], self.config, reader=reader
        )[self.end - timedelta(minutes=5)]

        self.assertEqual(summary.event_type_drift_tvd, 1)
        self.assertIsNone(summary.rating_drift_tvd)

    def test_multiple_buckets_share_one_read_and_keep_distinct_snapshots(self):
        later_end = self.end + timedelta(minutes=5)
        reader = FakeEventReader(
            [
                self.event(self.end - timedelta(minutes=60), "watch"),
                self.event(self.end, "rating", 7),
            ]
        )

        summaries = calculate_data_drift_buckets(
            [self.window(), self.window(later_end)], self.config, reader=reader
        )

        self.assertEqual(len(reader.calls), 1)
        self.assertEqual(reader.calls[0]["time_basis"], "event_time")
        self.assertEqual(
            reader.calls[0]["start"], self.end - timedelta(hours=2)
        )
        self.assertEqual(reader.calls[0]["end"], later_end)
        first = summaries[self.end - timedelta(minutes=5)]
        second = summaries[self.end]
        self.assertEqual(first.event_type_recent_counts["rating"], 0)
        self.assertEqual(second.event_type_recent_counts["rating"], 1)


if __name__ == "__main__":
    unittest.main()
