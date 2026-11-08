import unittest
from unittest.mock import patch

import numpy as np
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

from src.database.interactions import (
    cold_start_jobs,
    movies,
    users,
)
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
    content = np.zeros((len(movies), 3))
    content[1, 0] = 1
    content[2, 1] = 1
    content[3, 2] = 1
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
        self.store = ProfileStore()
        audit_patch = patch('src.service.api.recommendation_serves.create')
        self.create_recommendation_serve = audit_patch.start()
        self.addCleanup(audit_patch.stop)
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

    def test_recommendation_is_written_to_database_audit(self):
        response = self.client.get('/recommend/warm')

        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(users.get('warm'))
        audit = self.create_recommendation_serve.call_args.kwargs
        self.assertEqual(audit['user_id'], 'warm')
        self.assertEqual(audit['model_route'], 'collaborative_svd')
        self.assertEqual(audit['model_version'], 'test')
        self.assertEqual(
            [item['movie_id'] for item in audit['recommendations']],
            response.get_data(as_text=True).split(','),
        )
        self.assertGreaterEqual(audit['serve_duration_ms'], 0)
        self.assertLessEqual(len(audit['recommendations']), 20)

    def test_database_audit_failure_does_not_fail_recommendation(self):
        self.create_recommendation_serve.side_effect = RuntimeError('database unavailable')

        response = self.client.get('/recommend/warm')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.get_data(as_text=True).split(',')), 20)

    def test_blank_user_id_returns_bad_request_json(self):
        response = self.client.get('/recommend/%20%20')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'Bad Request')
        self.assertEqual(response.get_json()['message'], 'user_id must not be blank')

    def test_unknown_route_returns_not_found_json(self):
        response = self.client.get('/not-a-route')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()['error'], 'Not Found')

    def test_public_database_summary_returns_table_counts(self):
        users.create(user_id='summary-user')

        response = self.client.get('/database/summary')

        self.assertEqual(response.status_code, 200)
        counts = response.get_json()['tables']
        self.assertEqual(counts['users'], 1)
        self.assertIn('cold_start_jobs', counts)
        self.assertNotIn('summary-user', response.get_data(as_text=True))

    def test_public_cold_start_jobs_are_paginated_and_sanitized(self):
        self.store.enqueue('private-user-a')
        self.store.enqueue('private-user-b')

        response = self.client.get('/database/cold-start-jobs?limit=1')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(len(payload['jobs']), 1)
        self.assertTrue(payload['has_more'])
        self.assertEqual(payload['jobs'][0]['status'], 'pending')
        self.assertNotIn('user_id', payload['jobs'][0])
        self.assertNotIn('last_error', payload['jobs'][0])
        self.assertNotIn('private-user', response.get_data(as_text=True))

    def test_public_cold_start_jobs_filter_status_and_validate_query(self):
        self.store.enqueue('pending-user')
        users.create(user_id='processing-user')
        cold_start_jobs.enqueue(user_id='processing-user', status='processing')
        pending = self.client.get('/database/cold-start-jobs?status=pending').get_json()
        self.assertTrue(all(job['status'] == 'pending' for job in pending['jobs']))

        for query in ('?limit=abc', '?offset=-1', '?limit=101', '?status=unknown'):
            with self.subTest(query=query):
                response = self.client.get('/database/cold-start-jobs' + query)
                self.assertEqual(response.status_code, 400)

    def test_unexpected_error_returns_generic_internal_server_error(self):
        with patch('src.service.api.recommend', side_effect=RuntimeError('private detail')):
            response = self.client.get('/recommend/warm')
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()['error'], 'Internal Server Error')
        self.assertNotIn('private detail', response.get_data(as_text=True))

    def test_unknown_id_gets_fast_fallback_and_is_queued(self):
        response = self.client.get('/recommend/new-user')
        ids = response.get_data(as_text=True).split(',')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(ids), 20)
        job = self.store.claim_next()
        self.assertEqual(job['user_id'], 'new-user')

    def test_cold_start_database_failure_falls_back_to_recommendation(self):
        with patch.object(self.store, 'get_profile', side_effect=RuntimeError('database unavailable')):
            response = self.client.get('/recommend/contended-user')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.get_data(as_text=True).split(',')), 20)

    def test_raw_description_does_not_skip_llm_queue(self):
        users.create(user_id='cold-user', self_description_likes='unique-tag')
        movies.create_missing_many([
            {'movie_id': 'action-db', 'title': 'Unique pick', 'genres': 'unique-tag', 'popularity': 1},
            {'movie_id': 'drama-db', 'title': 'Drama pick', 'genres': 'Drama', 'popularity': 100},
        ])

        response = self.client.get('/recommend/cold-user')
        ids = response.get_data(as_text=True).split(',')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('action-db', ids)
        self.assertEqual(self.store.claim_next()['user_id'], 'cold-user')

    def test_saved_profile_personalizes_user_without_database_account(self):
        movies.create(movie_id='profile-db', title='Profile pick', genres='unique-tag', popularity=1)
        self.store.save_profile('profile-user', 'unique-tag', '')

        response = self.client.get('/recommend/profile-user')
        ids = response.get_data(as_text=True).split(',')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ids[0], 'movie-3')
        self.assertNotIn('profile-db', ids)
        self.assertIsNone(self.store.claim_next())

    def test_health_requires_model_and_profile_store(self):
        response = self.client.get('/health')
        self.assertEqual(response.status_code, 200)

if __name__ == '__main__':
    unittest.main()
