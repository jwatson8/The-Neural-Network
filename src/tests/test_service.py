import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from src.service.api import create_app
from src.service.model_adapter import recommend
from src.service.store import ProfileStore


class DenseResult:
    def __init__(self, values):
        self.values = values

    def toarray(self):
        return self.values


class ContentMatrix:
    def __init__(self, values):
        self.values = values

    def __matmul__(self, other):
        return DenseResult(self.values @ other)


class Vectorizer:
    def transform(self, phrases):
        return np.array([
            [1.0, 0.0, 0.0] if 'action' in phrase.lower()
            else [0.0, 1.0, 0.0] if 'drama' in phrase.lower()
            else [0.0, 0.0, 1.0] if 'unique-tag' in phrase.lower()
            else [0.0, 0.0, 0.0]
            for phrase in phrases
        ])


def make_model():
    movies = [{'movie_id': f'movie-{index}'} for index in range(22)]
    content = np.zeros((len(movies), 2))
    content[1, 0] = 1
    content[2, 1] = 1
    factors = np.zeros((2, len(movies)))
    factors[0] = np.arange(len(movies), 0, -1)
    popularity = np.arange(len(movies), dtype=float)
    return {
        'movies': movies,
        'mi': {movie['movie_id']: index for index, movie in enumerate(movies)},
        'ui': {'warm': 0},
        'seen': {'warm': {0}},
        'user_factors': np.array([[1.0, 0.0]]),
        'movie_factors': factors,
        'vectorizer': Vectorizer(),
        'content': ContentMatrix(content),
        'popularity': popularity,
    }


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = ProfileStore(Path(self.temp_dir.name) / 'profiles.sqlite3')
        self.app = create_app(
            model=make_model(),
            profile_store=self.store,
            config={'TESTING': True, 'MODEL_VERSION': 'test'},
        )
        self.client = self.app.test_client()

    def test_recommendation_is_plain_text_up_to_twenty_ids(self):
        response = self.client.get('/recommend/warm')
        ids = response.get_data(as_text=True).split(',')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.mimetype.startswith('text/plain'))
        self.assertEqual(len(ids), 20)
        self.assertNotIn('movie-0', ids)
        self.assertNotIn('\n', response.get_data(as_text=True))

    def test_unknown_id_gets_fast_fallback_and_is_queued(self):
        response = self.client.get('/recommend/new-user')
        ids = response.get_data(as_text=True).split(',')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(ids), 20)
        job = self.store.claim_next()
        self.assertEqual(job['user_id'], 'new-user')

    def test_database_contention_does_not_block_recommendation(self):
        with self.store.connect() as connection:
            connection.execute('BEGIN EXCLUSIVE')
            started = time.perf_counter()
            response = self.client.get('/recommend/contended-user')
            elapsed = time.perf_counter() - started
        self.assertEqual(response.status_code, 200)
        self.assertLess(elapsed, 0.6)

    def test_new_database_user_gets_database_backed_cold_start(self):
        with self.store.connect() as connection:
            connection.execute('''
                INSERT INTO users (user_id, self_description_likes, self_description_dislikes)
                VALUES (?, ?, ?)
            ''', ('cold-user', 'unique-tag', ''))
            connection.executemany('''
                INSERT INTO movies (movie_id, title, genres, popularity)
                VALUES (?, ?, ?, ?)
            ''', [
                ('action-db', 'Unique pick', 'unique-tag', 1),
                ('drama-db', 'Drama pick', 'Drama', 100),
            ])

        response = self.client.get('/recommend/cold-user')
        ids = response.get_data(as_text=True).split(',')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ids[0], 'action-db')
        self.assertIn('drama-db', ids)

    def test_saved_profile_personalizes_user_without_database_account(self):
        with self.store.connect() as connection:
            connection.execute('''
                INSERT INTO movies (movie_id, title, genres, popularity)
                VALUES (?, ?, ?, ?)
            ''', ('profile-db', 'Profile pick', 'unique-tag', 1))
        self.store.save_profile('profile-user', 'unique-tag', '')

        response = self.client.get('/recommend/profile-user')
        ids = response.get_data(as_text=True).split(',')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ids[0], 'profile-db')

    def test_health_requires_model_and_profile_store(self):
        response = self.client.get('/health')
        self.assertEqual(response.status_code, 200)

    def test_initialization_populates_dataset_tables_idempotently(self):
        expected_counts = {'events': 54704, 'movies': 2569, 'users': 1050}
        self.store.initialize()
        with self.store.connect() as connection:
            counts = {
                table: connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                for table in expected_counts
            }
            first_movie = connection.execute(
                'SELECT title FROM movies WHERE movie_id = ?',
                ('...and+justice+for+all+1979',),
            ).fetchone()
        self.assertEqual(counts, expected_counts)
        self.assertEqual(first_movie['title'], '...And Justice for All')


if __name__ == '__main__':
    unittest.main()