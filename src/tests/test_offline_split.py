import unittest
from src.model_updates.offline import split
from src.model_updates.snapshot import utc


class OfflineSplitTests(unittest.TestCase):
    def test_boundary_ties_stay_together_and_repeats_are_deduplicated(self):
        rows = [dict(timestamp=f'2026-01-01T00:00:0{i}Z', user_id='u', movie_id=str(i),
                     event_type='watch') for i in range(5)]
        rows += [dict(rows[-1], movie_id='extra'), rows[0]]
        train, test, cutoff = split(rows)
        self.assertEqual(len(train) + len(test), 6)
        self.assertTrue(all(utc(e['timestamp']) < cutoff for e in train))
        self.assertTrue(all(utc(e['timestamp']) >= cutoff for e in test))
        self.assertEqual(len(test), 2)

    def test_unsplittable_data_rejected(self):
        with self.assertRaises(ValueError):
            split([])
        rows = [dict(timestamp='2026-01-01', user_id='u', movie_id=str(i), event_type='watch') for i in range(2)]
        with self.assertRaises(ValueError):
            split(rows)
