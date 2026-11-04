"""Deterministic drift tests without production data or connections."""

import json
import unittest
from decimal import Decimal

from src.data_quality.drift import (
    detect_event_type_drift,
    detect_missing_value_drift,
    detect_rating_drift,
)
from src.database.schemas import StreamEventCreate


def ratings(*values: object) -> list[dict]:
    return [{"rating": value} for value in values]


def events(*values: object) -> list[dict]:
    return [{"event_type": value} for value in values]


class DriftTests(unittest.TestCase):
    def assert_result(self, result: dict, status: str, score: float | None) -> None:
        self.assertEqual(result["status"], status)
        self.assertEqual(result["drift_score"], score)
        self.assertEqual(set(result), {
            "metric", "baseline_size", "recent_size", "drift_score", "threshold",
            "min_samples", "status", "errors",
        })
        json.dumps(result, allow_nan=False)
        for error in result["errors"]:
            self.assertEqual(set(error), {"loc", "type", "msg"})
            self.assertTrue(error["msg"])

    def test_identical_rating_distributions_with_different_sizes(self) -> None:
        result = detect_rating_drift(ratings(1, 10) * 50, ratings(10, 1) * 100)
        self.assert_result(result, "no_drift", 0.0)
        self.assertEqual((result["baseline_size"], result["recent_size"]), (100, 200))
        self.assertEqual(result["metric"], "rating_distribution_tvd")

    def test_small_rating_difference(self) -> None:
        result = detect_rating_drift(ratings(1) * 50 + ratings(10) * 50,
                                     ratings(1) * 40 + ratings(10) * 60)
        self.assert_result(result, "no_drift", 0.1)

    def test_strong_rating_drift(self) -> None:
        self.assert_result(detect_rating_drift(ratings(1) * 100, ratings(10) * 100),
                           "drift_detected", 1.0)

    def test_fractional_ratings_are_not_rounded(self) -> None:
        self.assert_result(detect_rating_drift(ratings(1.5), ratings(1.6), min_samples=1),
                           "drift_detected", 1.0)
        self.assert_result(detect_rating_drift(ratings(Decimal("1.5")), ratings(1.5), min_samples=1),
                           "no_drift", 0.0)

    def test_identical_event_types(self) -> None:
        rows = events("watch", "rating", "account_created") * 40
        self.assert_result(detect_event_type_drift(rows, list(reversed(rows))), "no_drift", 0.0)

    def test_small_event_type_difference(self) -> None:
        self.assert_result(detect_event_type_drift(
            events("watch") * 50 + events("rating") * 50,
            events("watch") * 40 + events("rating") * 60,
        ), "no_drift", 0.1)

    def test_strong_event_type_drift(self) -> None:
        result = detect_event_type_drift(events("watch") * 100, events("account_created") * 100)
        self.assert_result(result, "drift_detected", 1.0)
        self.assertEqual(result["metric"], "event_type_distribution_tvd")

    def test_missing_value_drift(self) -> None:
        baseline = [{"movie_id": "test-movie"}] * 100
        recent = [{"movie_id": "test-movie"}] * 20 + [{"movie_id": None}] * 40 + [{}] * 40
        result = detect_missing_value_drift(baseline, recent, ["movie_id"])[0]
        self.assert_result(result, "drift_detected", 0.8)
        self.assertEqual(result["metric"], "missing_value_rate:movie_id")

    def test_identical_and_small_missingness(self) -> None:
        baseline = [{"value": 1}] * 100
        for count, score in ((0, 0.0), (5, 0.05)):
            with self.subTest(count=count):
                recent = [{}] * count + [{"value": 1}] * (100 - count)
                self.assert_result(detect_missing_value_drift(baseline, recent, ["value"])[0],
                                   "no_drift", score)

    def test_missingness_does_not_treat_falsy_values_as_null(self) -> None:
        rows = [{"value": value} for value in ("", " ", 0, False, [], {})]
        self.assert_result(detect_missing_value_drift(rows, [{}] * 6, ["value"], min_samples=1)[0],
                           "drift_detected", 1.0)

    def test_all_missing_and_multiple_fields(self) -> None:
        results = detect_missing_value_drift([{}] * 100, [{"age": 1}] * 100, ["age", "movie_id"])
        self.assert_result(results[0], "drift_detected", 1.0)
        self.assert_result(results[1], "no_drift", 0.0)

    def test_empty_snapshots(self) -> None:
        for baseline, recent in (([], []), ([], ratings(1) * 100), (ratings(1) * 100, [])):
            self.assert_result(detect_rating_drift(baseline, recent), "insufficient_data", None)
        self.assert_result(detect_event_type_drift([], []), "insufficient_data", None)
        self.assert_result(detect_missing_value_drift([], [], ["rating"])[0], "insufficient_data", None)

    def test_insufficient_samples_on_either_side(self) -> None:
        for first, second in ((99, 100), (100, 99)):
            self.assert_result(detect_rating_drift(ratings(1) * first, ratings(10) * second),
                               "insufficient_data", None)
            self.assert_result(detect_event_type_drift(events("watch") * first, events("rating") * second),
                               "insufficient_data", None)
            self.assert_result(detect_missing_value_drift([{}] * first, [{}] * second, ["x"])[0],
                               "insufficient_data", None)

    def test_invalid_rating_values_invalidate_whole_comparison(self) -> None:
        for value in (0, 11, -1, None, "5", True, [], {}, float("nan"),
                      float("inf"), -float("inf"), Decimal("NaN"), Decimal("sNaN")):
            with self.subTest(value=value):
                result = detect_rating_drift(ratings(5) * 100, ratings(5) * 100 + ratings(value))
                self.assert_result(result, "invalid_data", None)
                self.assertEqual(result["recent_size"], 101)
                self.assertEqual(result["errors"][0]["loc"], ("recent", 100, "rating"))

    def test_invalid_event_types(self) -> None:
        for value in ("unknown", " watch ", None, 1, True, [], float("nan")):
            with self.subTest(value=value):
                self.assert_result(detect_event_type_drift(events(value), events("watch")),
                                   "invalid_data", None)

    def test_missing_measurements_are_invalid_not_categories(self) -> None:
        for detector in (detect_rating_drift, detect_event_type_drift):
            self.assert_result(detector([{}], [{}], min_samples=1), "invalid_data", None)

    def test_missingness_rejects_nonfinite_values(self) -> None:
        for value in (float("nan"), float("inf"), -float("inf"), Decimal("NaN")):
            result = detect_missing_value_drift([{"x": value}], [{"x": 1}], ["x"], min_samples=1)[0]
            self.assert_result(result, "invalid_data", None)

    def test_malformed_snapshots_and_records(self) -> None:
        for raw in (None, "not-records", {"rating": 5}, [None], [5]):
            with self.subTest(raw=raw):
                self.assert_result(detect_rating_drift(raw, ratings(5)), "invalid_data", None)
                self.assert_result(detect_missing_value_drift(raw, [{}], ["x"])[0], "invalid_data", None)

    def test_threshold_boundaries_are_inclusive(self) -> None:
        baseline = ratings(1) * 50 + ratings(10) * 50
        recent = ratings(1) * 30 + ratings(10) * 70
        self.assert_result(detect_rating_drift(baseline, recent), "drift_detected", 0.2)
        self.assert_result(detect_rating_drift(baseline, recent, threshold=0.21), "no_drift", 0.2)
        self.assert_result(detect_rating_drift(ratings(1), ratings(10), min_samples=1, threshold=1),
                           "drift_detected", 1.0)
        self.assert_result(detect_rating_drift(ratings(1), ratings(1), min_samples=1, threshold=0),
                           "drift_detected", 0.0)
        # Exact rational rate arithmetic avoids 0.3 - 0.1 falling below 0.2.
        result = detect_missing_value_drift(
            [{}] * 10 + [{"x": 1}] * 90, [{}] * 30 + [{"x": 1}] * 70,
            ["x"], threshold=0.2,
        )[0]
        self.assert_result(result, "drift_detected", 0.2)

    def test_event_and_missing_threshold_defaults(self) -> None:
        self.assert_result(detect_event_type_drift(
            events("watch") * 100, events("watch") * 80 + events("rating") * 20,
        ), "drift_detected", 0.2)
        self.assert_result(detect_missing_value_drift(
            [{"x": 1}] * 100, [{}] * 10 + [{"x": 1}] * 90, ["x"],
        )[0], "drift_detected", 0.1)

    def test_order_independence_and_generator_inputs(self) -> None:
        baseline = ratings(1, 2, 3, 10) * 30
        recent = ratings(1, 5, 7, 10) * 30
        expected = detect_rating_drift(baseline, recent)
        self.assertEqual(expected, detect_rating_drift(reversed(baseline), iter(recent)))
        old, new = [{"x": 1}, {}] * 60, [{"x": 1}, {}, {}] * 40
        self.assertEqual(detect_missing_value_drift(old, new, ["x"]),
                         detect_missing_value_drift(reversed(old), reversed(new), ["x"]))

    def test_configuration_errors(self) -> None:
        for threshold in (-0.1, 1.1, True, None, "0.2", float("nan"), float("inf")):
            with self.subTest(threshold=threshold), self.assertRaises(ValueError):
                detect_rating_drift([], [], threshold=threshold)
        for minimum in (0, -1, True, 1.5, None):
            with self.subTest(minimum=minimum), self.assertRaises(ValueError):
                detect_event_type_drift([], [], min_samples=minimum)
        for fields in ([], "rating", ["x", "x"], [None], [""]):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                detect_missing_value_drift([], [], fields)

    def test_schema_objects_and_mapping_adapter(self) -> None:
        event = StreamEventCreate.model_validate({
            "user_id": "test-user", "movie_id": "test-movie", "event_type": "rating",
            "rating": 7, "timestamp": "2026-01-01T00:00:00Z",
        })
        before = event.model_dump()
        self.assert_result(detect_rating_drift([event], [before], min_samples=1), "no_drift", 0.0)
        self.assert_result(detect_event_type_drift([event], [before], min_samples=1), "no_drift", 0.0)
        self.assert_result(detect_missing_value_drift([event], [before], ["rating"], min_samples=1)[0],
                           "no_drift", 0.0)
        self.assertEqual(event.model_dump(), before)


if __name__ == "__main__":
    unittest.main()
