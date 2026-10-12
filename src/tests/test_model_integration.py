"""Train the real M0 snapshot and verify the Flask adapter preserves its rankings."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from src.model.prepare import prepare, DEFAULT_DATA
from src.model.recommender import load_profiles, recommend, ROOT
from src.service.api import create_app
from src.service.store import ProfileStore


class ModelIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.model = prepare(DEFAULT_DATA, Path(cls.temp.name) / 'model.joblib')
        cls.profiles = load_profiles(ROOT / 'src/model/llm_profiles.json')

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = ProfileStore(Path(self.directory.name) / 'profiles.sqlite3')
        self.store.seed_profiles(ROOT / 'src/model/llm_profiles.json')
        self.app = create_app(model=self.model, profile_store=self.store, config={'TESTING': True})
        self.client = self.app.test_client()

    def test_real_model_matches_original_m0_warm_and_all_seeded_profiles(self):
        for uid in ['1', '2', '3', *self.profiles]:
            with self.subTest(user=uid):
                expected = [r['movie_id'] for r in recommend(self.model, uid, self.profiles, k=20)['recommendations']]
                response = self.client.get('/recommend/' + uid)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.mimetype, 'text/plain')
                self.assertEqual(response.get_data(as_text=True).split(','), expected)

    def test_profile_created_after_request_changes_next_request_without_restart(self):
        from src.service.profile_worker import process_one, PreferenceProfile
        uid = 'integration-new-user'
        before = self.client.get('/recommend/' + uid).get_data(as_text=True)
        with patch('src.service.profile_worker.fetch_descriptions', return_value=('space adventures', 'horror')), \
             patch('src.service.profile_worker.generate_profile', return_value=PreferenceProfile(positive='space adventures science fiction', negative='horror')):
            process_one(self.store, object(), 'fake-model')
        after = self.client.get('/recommend/' + uid).get_data(as_text=True)
        self.assertNotEqual(before, after)
        with self.store.connect() as connection:
            route = connection.execute('SELECT route FROM response_audit WHERE user_id=? ORDER BY id DESC', (uid,)).fetchone()[0]
        self.assertEqual(route, 'llm_content')
        reopened = ProfileStore(self.store.path)
        self.assertIsNotNone(reopened.get_profile(uid))

    def test_missing_model_is_unhealthy(self):
        app = create_app(profile_store=self.store, config={'MODEL_PATH': str(Path(self.directory.name)/'missing.joblib')})
        self.assertEqual(app.test_client().get('/health').status_code, 503)


if __name__ == '__main__':
    unittest.main()
