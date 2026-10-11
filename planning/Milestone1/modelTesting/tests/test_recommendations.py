import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("SURPRISE_DATA_FOLDER", "/tmp/milestoneOne-surprise-data")

import numpy as np

from Alvajoy.train import recommend as recommend_alvajoy
from Frank.src.recommend import recommend_existing_user
from Jace.recommender import recommend as recommend_jace
from Nathan.trainModelv1 import recommend as recommend_nathan


class _ArrayResult:
    def __init__(self, values):
        self.values = values

    def numpy(self):
        return self.values


class _NathanModel:
    def __call__(self, inputs, training=False):
        del inputs, training
        return _ArrayResult(np.asarray([[1.0], [3.0], [2.0]]))


class _SurpriseAlgorithm:
    scores = {"a": 1.0, "b": 3.0, "c": 2.0}

    def predict(self, user_id, movie_id):
        del user_id
        return SimpleNamespace(est=self.scores[movie_id])


class RecommendationTests(unittest.TestCase):
    def test_nathan_ranks_and_excludes_seen_movies(self):
        recommendations = recommend_nathan(
            _NathanModel(), 1, ["a", "b", "c"], {"b"}, k=2
        )
        self.assertEqual(["c", "a"], [row["movie_id"] for row in recommendations])

    def test_alvajoy_ranks_and_excludes_seen_movies(self):
        recommendations = recommend_alvajoy(
            {"algorithm": _SurpriseAlgorithm()},
            1,
            ["a", "b", "c"],
            {"b"},
            k=2,
        )
        self.assertEqual(["c", "a"], [row["movie_id"] for row in recommendations])

    def test_frank_preserves_centered_rating_scoring(self):
        model = {
            "movie_ids": ["a", "b", "c", "d"],
            "similarity_matrix": np.asarray(
                [
                    [1.0, 0.0, 0.8, 0.1],
                    [0.0, 1.0, 0.2, 0.9],
                    [0.8, 0.2, 1.0, 0.0],
                    [0.1, 0.9, 0.0, 1.0],
                ]
            ),
        }
        recommendations = recommend_existing_user(
            model=model,
            user_id=1,
            user_ratings={"a": 10.0, "b": 1.0},
            seen_movie_ids={"a", "b"},
            movies_by_id={},
            top_k=2,
        )
        self.assertEqual(["c", "d"], [row["movie_id"] for row in recommendations])
        self.assertAlmostEqual(2.7, recommendations[0]["score"])
        self.assertAlmostEqual(-3.6, recommendations[1]["score"])

    def test_jace_allowlist_does_not_change_default_catalog_behavior(self):
        model = {
            "users": [{"user_id": "1"}],
            "movies": [
                {"movie_id": "a", "title": "A"},
                {"movie_id": "b", "title": "B"},
                {"movie_id": "c", "title": "C"},
            ],
            "ui": {"1": 0},
            "mi": {"a": 0, "b": 1, "c": 2},
            "seen": {"1": {0}},
            "user_factors": np.asarray([[1.0]]),
            "movie_factors": np.asarray([[3.0, 2.0, 1.0]]),
        }
        default = recommend_jace(model, "1", {}, k=2)
        restricted = recommend_jace(
            model, "1", {}, k=2, candidate_movie_ids=["a", "c"]
        )
        self.assertEqual(["b", "c"], [r["movie_id"] for r in default["recommendations"]])
        self.assertEqual(["c"], [r["movie_id"] for r in restricted["recommendations"]])


if __name__ == "__main__":
    unittest.main()
