import os
import unittest

os.environ.setdefault("SURPRISE_DATA_FOLDER", "/tmp/milestoneOne-surprise-data")

import numpy as np

from Frank.src.recommend import predict_rating as predict_frank_rating
from Jace.recommender import predict_rating as predict_jace_rating
from inference_tests import (
    _baseline_recommendations,
    _binary_ndcg_at_k,
    _point_metrics,
    _ranking_summary,
    _rating_partitions,
)
from throughput import _training_catalog
from utils.dataProvider import DATA_PATH, loadPreparedData


class InferenceEvaluationTests(unittest.TestCase):
    def test_point_metrics_match_known_confusion_matrix(self):
        result = _point_metrics(
            actual=[8.0, 4.0, 9.0, 2.0],
            predicted=[8.0, 8.0, 6.0, 1.0],
            threshold=7.5,
        )
        self.assertAlmostEqual(2.0, result["mae"])
        self.assertAlmostEqual(np.sqrt(6.5), result["rmse"])
        self.assertEqual((1, 1, 1, 1), (
            result["tp"], result["fp"], result["tn"], result["fn"]
        ))
        self.assertAlmostEqual(0.5, result["precision"])
        self.assertAlmostEqual(0.5, result["recall"])
        self.assertAlmostEqual(0.5, result["false_positive_rate"])
        self.assertAlmostEqual(0.5, result["false_negative_rate"])

    def test_ranking_summary_is_macro_averaged(self):
        result = _ranking_summary(
            [
                {"hits": 2, "relevant": 4, "ndcg": 0.5},
                {"hits": 0, "relevant": 1, "ndcg": 0.0},
            ],
            top_k=5,
        )
        self.assertAlmostEqual(0.2, result["precision_at_k"])
        self.assertAlmostEqual(0.25, result["recall_at_k"])
        self.assertAlmostEqual(0.5, result["hit_rate_at_k"])
        self.assertAlmostEqual(0.25, result["ndcg_at_k"])
        self.assertEqual(2, result["total_hits"])
        self.assertEqual(5, result["total_relevant"])

    def test_binary_ndcg_rewards_hits_near_the_top(self):
        ndcg = _binary_ndcg_at_k(
            ["relevant-1", "miss", "relevant-2", "miss-2"],
            {"relevant-1", "relevant-2", "relevant-3"},
            top_k=4,
        )
        expected = (1.0 + 0.5) / (1.0 + 1 / np.log2(3) + 0.5)
        self.assertAlmostEqual(expected, ndcg)
        self.assertEqual(0.0, _binary_ndcg_at_k(["miss"], set(), top_k=1))

    def test_frank_rating_conversion_and_zero_similarity_fallback(self):
        model = {
            "movie_ids": ["rated", "target"],
            "similarity_matrix": np.asarray([[1.0, 0.5], [0.5, 1.0]]),
        }
        self.assertAlmostEqual(
            9.0,
            predict_frank_rating(model, {"rated": 9.0}, "target"),
        )
        model["similarity_matrix"][1, 0] = 0.0
        self.assertAlmostEqual(
            5.5,
            predict_frank_rating(model, {"rated": 9.0}, "target"),
        )

    def test_jace_inverse_rating_conversion(self):
        model = {
            "ui": {"1": 0},
            "mi": {"movie": 0},
            "seen": {"1": {0}},
            "user_factors": np.asarray([[1.0]]),
            "movie_factors": np.asarray([[0.5]]),
        }
        self.assertAlmostEqual(7.75, predict_jace_rating(model, 1, "movie"))

    def test_baseline_ranks_by_training_average_and_excludes_seen(self):
        recommendations = _baseline_recommendations(
            {"a": 7.0, "b": 9.0, "c": 8.0},
            ["a", "b", "c"],
            {"b"},
            2,
        )
        self.assertEqual(["c", "a"], [row["movie_id"] for row in recommendations])

    def test_supplied_warm_ratings_are_pair_disjoint(self):
        data = loadPreparedData(DATA_PATH)
        training, warm, cold = _rating_partitions(data, _training_catalog(data))
        self.assertEqual(21766, len(training))
        self.assertEqual(4893, len(warm))
        self.assertEqual(668, len(cold))


if __name__ == "__main__":
    unittest.main()
