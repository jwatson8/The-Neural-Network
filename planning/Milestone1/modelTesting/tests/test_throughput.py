import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("SURPRISE_DATA_FOLDER", "/tmp/milestoneOne-surprise-data")

from throughput import (
    _append_report,
    _cohort_counts,
    _select_users,
    _summaries,
    _training_catalog,
)
from utils.dataProvider import DATA_PATH, loadPreparedData


class ThroughputTests(unittest.TestCase):
    def test_supplied_data_warm_and_cold_cohorts(self):
        data = loadPreparedData(DATA_PATH)
        candidates = _training_catalog(data)
        cohorts, _ = _cohort_counts(data, candidates)
        self.assertEqual(2023, len(candidates))
        self.assertEqual(
            {"pairs": 4893, "users": 971, "movies": 1148}, cohorts["warm"]
        )
        self.assertEqual(
            {"pairs": 668, "users": 314, "movies": 546}, cohorts["cold"]
        )

    def test_user_sample_is_deterministic_and_eligible(self):
        ratings = {user_id: {"movie": 8.0} for user_id in range(1, 8)}
        warm_users = {2, 3, 4, 5, 6, 7, 8}
        first = _select_users(ratings, warm_users, tests=4, seed=42)
        second = _select_users(ratings, warm_users, tests=4, seed=42)
        self.assertEqual(first, second)
        self.assertTrue(set(first) <= {2, 3, 4, 5, 6, 7})

    def test_summaries_and_markdown_report(self):
        measurements = {
            "Example": [
                {"test": 1, "user_id": 1, "returned": 1, "seconds": 0.1},
                {"test": 2, "user_id": 2, "returned": 1, "seconds": 0.3},
            ]
        }
        summaries = _summaries(measurements)
        self.assertAlmostEqual(0.2, summaries["Example"]["average_seconds"])
        self.assertAlmostEqual(5.0, summaries["Example"]["requests_per_second"])

        cohorts = {
            "warm": {"movies": 1, "pairs": 2, "users": 2},
            "cold": {"movies": 1, "pairs": 1, "users": 1},
        }
        with TemporaryDirectory() as temporary:
            report = Path(temporary) / "training_times.md"
            _append_report(
                report,
                "2026-01-01T00:00:00+00:00",
                measurements,
                summaries,
                cohorts,
                ["movie"],
                2,
                1,
                42,
                Path("data"),
                Path("models"),
            )
            contents = report.read_text(encoding="utf-8")
            self.assertIn("# Model benchmark results", contents)
            self.assertIn("| Example | 1 | 2 | 0.200000 | 200.000 | 5.000 |", contents)
            self.assertIn("| Cold | 1 | 1 | 1 | No |", contents)


if __name__ == "__main__":
    unittest.main()
