"""Offline contract tests; importing the Flask app would initialize a store."""

import json
import unittest

from src.data_quality.boundary import (
    validate_recommendation_item,
    validate_recommendation_request,
    validate_recommendation_response,
)
from src.database.schemas import RecommendationItem
from pydantic import ValidationError


class RecommendationValidationTests(unittest.TestCase):
    def assert_invalid(self, validator, raw: object, location: tuple, **kwargs) -> None:
        result, errors = validator(raw, **kwargs)
        self.assertIsNone(result)
        self.assertIsInstance(errors, list)
        self.assertTrue(errors)
        self.assertIn(location, [error["loc"] for error in errors])
        for error in errors:
            self.assertEqual(set(error), {"loc", "type", "msg"})
            self.assertTrue(error["type"])
            self.assertTrue(error["msg"])
        json.dumps(errors, allow_nan=False)

    def test_valid_requests(self) -> None:
        for user_id in ("test-user", "42", "unknown-user", " test-user "):
            with self.subTest(user_id=user_id):
                raw = {"user_id": user_id}
                request, errors = validate_recommendation_request(raw, query_params={})
                self.assertEqual(errors, [])
                self.assertEqual(request, raw)
                self.assertIsNot(request, raw)

    def test_missing_user_id(self) -> None:
        self.assert_invalid(validate_recommendation_request, {}, ("user_id",))

    def test_invalid_user_ids(self) -> None:
        for user_id in (None, "", " \t\n", 42, True, [], {}, "user/other",
                        float("nan"), float("inf")):
            with self.subTest(user_id=user_id):
                self.assert_invalid(
                    validate_recommendation_request, {"user_id": user_id}, ("user_id",)
                )

    def test_malformed_requests(self) -> None:
        for raw in (None, [], "test-user", True, float("nan")):
            with self.subTest(raw=raw):
                self.assert_invalid(validate_recommendation_request, raw, ())

    def test_unsupported_request_parameters(self) -> None:
        for value in (1, 20, 0, 21, True, None, "20", float("nan"), float("inf")):
            with self.subTest(value=value):
                self.assert_invalid(
                    validate_recommendation_request,
                    {"user_id": "test-user", "k": value}, ("k",),
                )
                self.assert_invalid(
                    validate_recommendation_request, {"user_id": "test-user"},
                    ("query_params", "k"), query_params={"k": value},
                )

    def test_other_query_parameters_and_malformed_query_mapping(self) -> None:
        self.assert_invalid(
            validate_recommendation_request, {"user_id": "test-user"},
            ("query_params", "limit"), query_params={"limit": "10"},
        )
        self.assert_invalid(
            validate_recommendation_request, {"user_id": "test-user"},
            ("query_params",), query_params=[],
        )

    def test_valid_response_preserves_order(self) -> None:
        ids = ["test-z", "test-a", "test-m"]
        items, errors = validate_recommendation_response(",".join(ids))
        self.assertEqual(errors, [])
        self.assertEqual([item.movie_id for item in items], ids)
        self.assertTrue(all(isinstance(item, RecommendationItem) for item in items))
        self.assertTrue(all(item.model_score is None for item in items))

    def test_response_count_bounds(self) -> None:
        for count in (1, 20):
            with self.subTest(count=count):
                ids = [f"test-{index}" for index in range(count)]
                items, errors = validate_recommendation_response(",".join(ids))
                self.assertEqual(errors, [])
                self.assertEqual([item.movie_id for item in items], ids)

    def test_empty_and_excessive_responses(self) -> None:
        for body in ("", ",".join(f"test-{index}" for index in range(21))):
            with self.subTest(body=body):
                self.assert_invalid(validate_recommendation_response, body, ("recommendations",))

    def test_empty_or_blank_movie_ids(self) -> None:
        for body, index in ((",test-a", 0), ("test-a,", 1),
                            ("test-a,,test-b", 1), ("test-a, \t", 1)):
            with self.subTest(body=body):
                self.assert_invalid(
                    validate_recommendation_response, body,
                    ("recommendations", index, "movie_id"),
                )

    def test_response_rejects_line_breaks(self) -> None:
        for body in ("test-a\n", "test-a\rtest-b"):
            with self.subTest(body=body):
                self.assert_invalid(
                    validate_recommendation_response, body,
                    ("recommendations", 0, "movie_id"),
                )

    def test_duplicate_recommendations(self) -> None:
        self.assert_invalid(
            validate_recommendation_response, "test-a,test-b,test-a",
            ("recommendations", 2, "movie_id"),
        )

    def test_malformed_public_response_structures(self) -> None:
        for raw in (None, [], {}, {"recommendations": ["test-a"]},
                    ["test-a"], b"test-a", 42, True, float("nan"), float("inf")):
            with self.subTest(raw=raw):
                self.assert_invalid(validate_recommendation_response, raw, ())

    def test_public_response_does_not_accept_telemetry_or_scores(self) -> None:
        for raw in ({"recommendations": [{"movie_id": "test-a", "model_score": 1}]},
                    {"user_id": "test-user", "served_at": "2026-01-01T00:00:00Z"}):
            with self.subTest(raw=raw):
                self.assert_invalid(validate_recommendation_response, raw, ())

    def test_valid_optional_model_scores(self) -> None:
        for fields in ({}, {"model_score": None}, {"model_score": -1},
                       {"model_score": 0}, {"model_score": 1}, {"model_score": "0.5"}):
            with self.subTest(fields=fields):
                raw = {"movie_id": "test-a", **fields}
                item, errors = validate_recommendation_item(raw)
                self.assertEqual(errors, [])
                self.assertIsInstance(item, RecommendationItem)
                self.assertEqual(item.movie_id, "test-a")
                expected = fields.get("model_score")
                self.assertEqual(item.model_score, None if expected is None else float(expected))
                self.assertEqual(raw, {"movie_id": "test-a", **fields})

    def test_invalid_model_scores(self) -> None:
        for score in (True, False, [], {}, "invalid", float("nan"), float("inf"),
                      -float("inf"), "NaN", "Infinity", "-Infinity"):
            with self.subTest(score=score):
                self.assert_invalid(
                    validate_recommendation_item,
                    {"movie_id": "test-a", "model_score": score}, ("model_score",),
                )

    def test_model_scores_outside_inclusive_range_are_rejected_not_clipped(self) -> None:
        for score, error_type in ((-1.000001, "greater_than_equal"),
                                  (1.000001, "less_than_equal"),
                                  (-5.5, "greater_than_equal"), (1.02, "less_than_equal")):
            with self.subTest(score=score):
                raw = {"movie_id": "test-a", "model_score": score}
                item, errors = validate_recommendation_item(raw)
                self.assertIsNone(item)
                self.assertEqual(errors[0]["loc"], ("model_score",))
                self.assertEqual(errors[0]["type"], error_type)
                self.assertIn("1", errors[0]["msg"])
                self.assertEqual(raw["model_score"], score)

    def test_direct_recommendation_item_schema_enforces_score_contract(self) -> None:
        for score in (-1, -0.5, 0, 0.5, 1, None):
            with self.subTest(valid_score=score):
                item = RecommendationItem(movie_id="test-a", model_score=score)
                self.assertEqual(item.model_score, score)
        for score in (-1.000001, 1.000001, 1.02, True, False, float("nan"),
                      float("inf"), -float("inf"), "NaN", "Infinity", [], {}, "bad", 1j):
            with self.subTest(invalid_score=score), self.assertRaises(ValidationError):
                RecommendationItem(movie_id="test-a", model_score=score)

    def test_item_requires_movie_id(self) -> None:
        self.assert_invalid(validate_recommendation_item, {}, ("movie_id",))

    def test_invalid_item_movie_ids(self) -> None:
        for movie_id in (None, "", " \t", 42, True, [], {}, "test,a", "test\na", "x" * 256):
            with self.subTest(movie_id=movie_id):
                self.assert_invalid(validate_recommendation_item, {"movie_id": movie_id}, ("movie_id",))

    def test_item_rejects_unknown_fields(self) -> None:
        self.assert_invalid(
            validate_recommendation_item, {"movie_id": "test-a", "score": 1}, ("score",)
        )

    def test_malformed_items(self) -> None:
        for raw in (None, [], "test-a", 1):
            with self.subTest(raw=raw):
                self.assert_invalid(validate_recommendation_item, raw, ())


if __name__ == "__main__":
    unittest.main()
