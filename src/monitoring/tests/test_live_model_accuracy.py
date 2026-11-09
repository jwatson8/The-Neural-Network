"""Tests for bucketed live recommendation accuracy calculations."""

import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from src.database.interactions.rating_attributions import ModelIdentity, ModelServe
from src.monitoring.calculations import live_model_accuracy
from src.monitoring.schemas import EvaluationWindow, MonitoringConfig


class LiveModelAccuracyTests(unittest.TestCase):
    def setUp(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        self.windows = [
            EvaluationWindow(start=start, end=start + timedelta(minutes=5)),
            EvaluationWindow(
                start=start + timedelta(minutes=5),
                end=start + timedelta(minutes=10),
            ),
        ]
        self.config = MonitoringConfig(
            backfill_hours=1,
            attribution_hours=2,
            positive_rating_threshold=6,
        )

    def test_groups_attributed_ratings_and_uses_weighted_counts(self):
        cold_start = ModelIdentity("llm_content", "m1")
        collaborative = ModelIdentity("collaborative_svd", "m1")
        popularity = ModelIdentity("popularity_fallback", "m1")
        rows = (
            SimpleNamespace(
                event_time=self.windows[0].start,
                rating=8,
                attributed=True,
                models=(cold_start, collaborative),
            ),
            SimpleNamespace(
                event_time=self.windows[0].start,
                rating=4,
                attributed=True,
                models=(cold_start,),
            ),
            SimpleNamespace(
                event_time=self.windows[0].start,
                rating=10,
                attributed=False,
                models=(),
            ),
            SimpleNamespace(
                event_time=self.windows[1].start,
                rating=9,
                attributed=True,
                models=(collaborative,),
            ),
        )
        serves = [
            ModelServe(self.windows[0].start - timedelta(minutes=5), cold_start),
            ModelServe(
                self.windows[0].start - timedelta(minutes=5), collaborative
            ),
            ModelServe(self.windows[0].start, popularity),
        ]
        with patch.object(
            live_model_accuracy.rating_attributions,
            "search",
            return_value=rows,
        ) as attributions, patch.object(
            live_model_accuracy.rating_attributions,
            "search_model_serves",
            return_value=serves,
        ):
            summaries = live_model_accuracy.calculate_live_model_accuracy_buckets(
                self.windows, self.config
            )

        first = summaries[self.windows[0].start]
        second = summaries[self.windows[1].start]
        self.assertEqual(first.evaluated_rating_count, 3)
        self.assertEqual(first.attributed_rating_count, 2)
        self.assertEqual(first.positive_rating_count, 1)
        self.assertEqual(first.accuracy, 0.5)
        self.assertEqual(second.accuracy, 1)
        first_models = {
            (summary.model_route, summary.model_version): summary
            for summary in first.models
        }
        self.assertEqual(
            first_models[("llm_content", "m1")].attributed_rating_count, 2
        )
        self.assertEqual(first_models[("llm_content", "m1")].accuracy, 0.5)
        self.assertEqual(
            first_models[("collaborative_svd", "m1")].accuracy, 1
        )
        self.assertEqual(
            first_models[("popularity_fallback", "m1")].attributed_rating_count,
            0,
        )
        self.assertIsNone(
            first_models[("popularity_fallback", "m1")].accuracy
        )
        attributions.assert_called_once_with(
            start=self.windows[0].start,
            end=self.windows[1].end,
            attribution_hours=2,
        )

    def test_empty_and_unattributed_buckets_have_no_accuracy(self):
        row = SimpleNamespace(
            event_time=self.windows[0].start,
            rating=10,
            attributed=False,
            models=(),
        )
        with patch.object(
            live_model_accuracy.rating_attributions,
            "search",
            return_value=(row,),
        ), patch.object(
            live_model_accuracy.rating_attributions,
            "search_model_serves",
            return_value=(),
        ):
            summaries = live_model_accuracy.calculate_live_model_accuracy_buckets(
                self.windows, self.config
            )

        self.assertEqual(summaries[self.windows[0].start].evaluated_rating_count, 1)
        self.assertIsNone(summaries[self.windows[0].start].accuracy)
        self.assertEqual(summaries[self.windows[1].start].evaluated_rating_count, 0)
        self.assertIsNone(summaries[self.windows[1].start].accuracy)


if __name__ == "__main__":
    unittest.main()
