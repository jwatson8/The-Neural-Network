import unittest
from pathlib import Path

import pandas as pd

from utils.dataProvider import DATA_PATH, loadFullData, loadPreparedData
from utils.dataSplitter import pairFingerprint, splitDataForML


class DataSplitterTests(unittest.TestCase):
    def test_pairs_stay_together_and_split_chronologically(self):
        rows = [
            {
                "timestamp": "2025-01-01T00:00:00",
                "user_id": 1,
                "event_type": "account_created",
                "movie_id": None,
                "rating": None,
            }
        ]
        for index, movie_id in enumerate(["a", "b", "c", "d", "e"], start=1):
            rows.extend(
                [
                    {
                        "timestamp": f"2025-01-01T00:{index:02d}:00",
                        "user_id": 1,
                        "event_type": "watch",
                        "movie_id": movie_id,
                        "rating": None,
                    },
                    {
                        "timestamp": f"2025-01-01T00:{index:02d}:30",
                        "user_id": 1,
                        "event_type": "rating",
                        "movie_id": movie_id,
                        "rating": index,
                    },
                ]
            )
        rows.extend(
            [
                {
                    "timestamp": "2025-01-01T01:00:00",
                    "user_id": 2,
                    "event_type": "watch",
                    "movie_id": "only",
                    "rating": None,
                },
                {
                    "timestamp": "2025-01-01T01:00:30",
                    "user_id": 2,
                    "event_type": "rating",
                    "movie_id": "only",
                    "rating": 7,
                },
            ]
        )

        train, test = splitDataForML(pd.DataFrame(rows), 0.8)
        train_pairs = set(
            train.dropna(subset=["movie_id"])[["user_id", "movie_id"]].itertuples(
                index=False, name=None
            )
        )
        test_pairs = set(
            test[["user_id", "movie_id"]].itertuples(index=False, name=None)
        )

        self.assertFalse(train_pairs & test_pairs)
        self.assertEqual({(1, "e")}, test_pairs)
        self.assertIn((2, "only"), train_pairs)
        self.assertEqual(2, len(test[test["movie_id"] == "e"]))
        self.assertEqual(1, len(train[train["event_type"] == "account_created"]))
        user_train = train[(train["user_id"] == 1) & train["movie_id"].notna()]
        user_test = test[test["user_id"] == 1]
        self.assertLess(user_train["timestamp"].max(), user_test["timestamp"].min())
        train_again, test_again = splitDataForML(pd.DataFrame(rows), 0.8)
        self.assertEqual(pairFingerprint(train), pairFingerprint(train_again))
        self.assertEqual(pairFingerprint(test), pairFingerprint(test_again))

    def test_supplied_data_counts(self):
        if not Path(DATA_PATH / "events.csv.gz").is_file():
            self.skipTest("supplied data is unavailable")
        data = loadPreparedData(DATA_PATH)
        self.assertEqual(21766, data.split_metadata["train_pair_count"])
        self.assertEqual(5561, data.split_metadata["test_pair_count"])
        self.assertEqual(43582, data.split_metadata["train_event_count"])
        self.assertEqual(11122, data.split_metadata["test_event_count"])

    def test_full_data_uses_every_event(self):
        if not Path(DATA_PATH / "events.csv.gz").is_file():
            self.skipTest("supplied data is unavailable")
        data = loadFullData(DATA_PATH)
        self.assertEqual("full_event_history", data.split_metadata["strategy"])
        self.assertEqual(27327, data.split_metadata["train_pair_count"])
        self.assertEqual(54704, data.split_metadata["train_event_count"])
        self.assertEqual(0, data.split_metadata["test_event_count"])
        self.assertTrue(data.test_events.empty)


if __name__ == "__main__":
    unittest.main()
