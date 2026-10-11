import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from deployment.traffic_report import summarize
from src.service.course_stream import handle_line
from src.service.profile_worker import PreferenceProfile, process_one
from src.service.store import ProfileStore
from src.service.model_adapter import recommend
from src.tests.test_service import make_model


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = ProfileStore(Path(self.directory.name) / 'state.sqlite3')

    def test_account_event_queues_once_and_profile_survives_reopen(self):
        line = '2026-10-11T12:00:00Z,501,GET /create_account'
        for _ in range(2):
            self.assertEqual(handle_line(self.store, 'topic', 0, 1, line), 'account')
        with patch('src.service.profile_worker.fetch_descriptions', return_value=('space', 'horror')), \
             patch('src.service.profile_worker.generate_profile', return_value=PreferenceProfile(positive='space', negative='horror')) as generate:
            self.assertTrue(process_one(self.store, object(), 'test-model'))
            self.assertFalse(process_one(self.store, object(), 'test-model'))
            generate.assert_called_once()
        reopened = ProfileStore(self.store.path)
        self.assertEqual(reopened.get_profile('501')['positive'], 'space')
        handle_line(reopened, 'topic', 0, 1, line)
        self.assertIsNone(reopened.claim_next())

    def test_lookup_failure_schedules_retry_without_profile(self):
        self.store.enqueue('502')
        with patch('src.service.profile_worker.fetch_descriptions', side_effect=TimeoutError):
            self.assertTrue(process_one(self.store, object(), 'test-model'))
        self.assertIsNone(self.store.get_profile('502'))
        self.assertIsNone(self.store.claim_next())

    def test_empty_preferences_do_not_call_llm_or_count_as_personalized(self):
        self.store.enqueue('503')
        with patch('src.service.profile_worker.fetch_descriptions', return_value=('', '')), \
             patch('src.service.profile_worker.generate_profile') as generate:
            process_one(self.store, object(), 'test-model')
            generate.assert_not_called()
        _, route = recommend(make_model(), '503', self.store.get_profile('503'))
        self.assertEqual(route, 'popularity_fallback')

    def test_report_denominator_includes_failure_and_deduplicates_offsets(self):
        now = time.time()
        timestamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
        self.store.record_response('501', ['movie-a', 'movie-b'], 'llm_content')
        lines = [
            f'{timestamp},501,recommendation request team-vm:8082, status 200, result: movie-a,movie-b, 4ms',
            f'{timestamp},502,recommendation request team-vm:8082, status 500, result: error, 600ms',
            f'{timestamp},503,recommendation request team-vm:8082, status 200, result: movie-c, 4ms',
            f'{timestamp},501,recommendation request other-vm:8082, status 200, result: movie-a,movie-b, 4ms',
        ]
        for _ in range(2):
            for offset, line in enumerate(lines):
                handle_line(self.store, 'topic', 0, offset, line)
        with self.store.connect() as connection:
            report = summarize(connection, 'team-vm:8082', now - 10, now + 10, 'UTC')
        self.assertEqual(report['all_requests'], 3)
        self.assertEqual(report['successful_status_200'], 2)
        self.assertEqual(report['matched_personalized_successes'], 1)
        self.assertAlmostEqual(report['matched_personalized_fraction_of_ALL_requests'], 1 / 3)


if __name__ == '__main__':
    unittest.main()
