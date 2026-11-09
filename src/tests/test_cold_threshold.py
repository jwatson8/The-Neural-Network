"""The routing boundary counts trained distinct movies, not raw event volume."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

from src.database.interactions import movies, stream_events, users
from src.model.routing import uses_svd
from src.model.recommender import fit
from src.model_updates.evaluation import rank, FrozenProfiles
from src.service.api import create_app
from src.service.model_adapter import recommend
from src.service.store import ProfileStore
from src.tests.test_service import make_model


class ThresholdTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.engine_patch = patch(
            "src.database.database.get_engine", return_value=self.engine
        )
        self.engine_patch.start()
        self.addCleanup(self.engine_patch.stop)
        self.addCleanup(self.engine.dispose)
        self.store = ProfileStore()
        self.model = make_model()

    def record_history(self, movie_indices):
        users.create_missing_many([{"user_id": "warm"}])
        records = [
            {
                "movie_id": str(self.model["movies"][index]["movie_id"]),
                "title": str(self.model["movies"][index]["movie_id"]),
            }
            for index in movie_indices
        ]
        movies.create_missing_many(records)
        return stream_events.create_many(
            {
                "user_id": "warm",
                "movie_id": record["movie_id"],
                "event_type": "watch",
                "event_time": "2026-01-01T00:00:00Z",
            }
            for record in records
        )

    def test_nine_uses_content_ten_uses_svd_and_excludes_seen(self):
        self.store.save_profile('warm', 'action', '')
        self.model['seen']['warm'] = set(range(10, 19))
        self.record_history(range(10, 19))
        client = create_app(model=self.model, profile_store=self.store).test_client()
        with patch('src.service.api.recommend', wraps=recommend) as svd:
            response = client.get('/recommend/warm')
            svd.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text.split(',')[0], 'movie-1')
        self.assertFalse(set(response.text.split(',')) & {f'movie-{i}' for i in range(10, 19)})
        self.model['seen']['warm'].add(19)
        self.record_history([19])
        with patch('src.service.api.recommend', wraps=recommend) as svd:
            response = client.get('/recommend/warm')
            svd.assert_called_once()
        self.assertEqual(response.text.split(',')[0], 'movie-0')
        self.assertEqual(recommend(self.model, 'warm')[1], 'collaborative_svd')

    def test_low_history_pending_or_empty_profile_uses_popularity(self):
        self.model['seen']['warm'] = set(range(9))
        client = create_app(model=self.model, profile_store=self.store).test_client()
        self.assertEqual(client.get('/recommend/warm').text.split(',')[0], 'movie-21')
        self.assertEqual(self.store.claim_next()['user_id'], 'warm')
        self.store.save_profile('warm', '', '')
        self.assertEqual(client.get('/recommend/warm').text.split(',')[0], 'movie-21')
        self.assertEqual(recommend(self.model, 'warm')[1], 'popularity_fallback')

    def test_evaluation_and_serving_share_boundary(self):
        profiles = FrozenProfiles([dict(user_id='warm', positive='action', negative='')])
        self.model['seen']['warm'] = set(range(10, 19))
        self.assertEqual(rank(self.model, 'warm', profiles)[0], 'movie-1')
        self.model['seen']['warm'].add(19)
        self.assertEqual(rank(self.model, 'warm', profiles)[0], 'movie-0')
        self.assertFalse(uses_svd(self.model, 'not-trained'))

    def test_repeated_minutes_and_ratings_count_one_distinct_movie(self):
        users = [dict(user_id='u'), dict(user_id='v')]
        movies = [dict(movie_id=str(i), title=f'action movie {i}') for i in range(12)]
        events = [dict(timestamp='2026-01-01', user_id='u', movie_id='0', event_type='watch') for _ in range(20)]
        events.append(dict(timestamp='2026-01-02', user_id='u', movie_id='0', event_type='rating', rating=8))
        model = fit(events, users, movies)
        self.assertEqual(len(model['seen']['u']), 1)
        self.assertFalse(uses_svd(model, 'u'))
        events.extend(dict(timestamp='2026-01-03', user_id='u', movie_id=str(i), event_type='watch') for i in range(1, 10))
        self.assertFalse(uses_svd(model, 'u'))  # Incoming events do not mutate a deployed bundle.
        self.assertTrue(uses_svd(fit(events, users, movies), 'u'))
