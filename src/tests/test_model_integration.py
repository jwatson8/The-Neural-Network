"""Train the real M0 snapshot and verify the Flask adapter preserves its rankings."""
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

from src.database.interactions import (
    movies,
    recommendation_serves,
    stream_events,
    users,
)
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
        self.engine = create_engine(
            'sqlite://',
            connect_args={'check_same_thread': False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.engine_patch = patch('src.database.database.get_engine', return_value=self.engine)
        self.engine_patch.start()
        self.addCleanup(self.engine_patch.stop)
        self.addCleanup(self.engine.dispose)
        warm_users = ('1', '2', '3')
        users.create_missing_many([{"user_id": user_id} for user_id in warm_users])
        seen_indices = {
            movie_index
            for user_id in warm_users
            for movie_index in self.model['seen'].get(user_id, set())
        }
        seen_movies = {
            str(self.model['movies'][movie_index]['movie_id']): self.model['movies'][movie_index]
            for movie_index in seen_indices
        }
        movies.create_missing_many([
            {"movie_id": movie_id, "title": movie["title"]}
            for movie_id, movie in seen_movies.items()
        ])
        event_time = datetime.now(UTC)
        stream_events.create_many(
            {
                "user_id": user_id,
                "movie_id": str(self.model['movies'][movie_index]['movie_id']),
                "event_type": "watch",
                "event_time": event_time,
            }
            for user_id in warm_users
            for movie_index in self.model['seen'].get(user_id, set())
        )
        self.store = ProfileStore()
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
             patch('src.service.profile_worker.generate_profile', return_value=(PreferenceProfile(positive='space adventures science fiction', negative='horror'), None)):
            process_one(self.store, object(), 'fake-model')
        after = self.client.get('/recommend/' + uid).get_data(as_text=True)
        self.assertNotEqual(before, after)
        serves = recommendation_serves.search(user_id=uid, limit=10).records
        self.assertEqual(serves[0].model_route, 'llm_content')
        reopened = ProfileStore()
        self.assertIsNotNone(reopened.get_profile(uid))

    def test_missing_model_is_unhealthy(self):
        app = create_app(profile_store=self.store, config={'MODEL_PATH': str(Path(self.directory.name)/'missing.joblib')})
        self.assertEqual(app.test_client().get('/health').status_code, 503)


if __name__ == '__main__':
    unittest.main()
