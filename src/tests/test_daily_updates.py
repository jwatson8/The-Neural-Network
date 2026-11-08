"""Daily pipeline checks use only local synthetic data and in-memory databases."""
import copy
import csv
from datetime import UTC, date, datetime, timedelta
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import joblib
import numpy as np
from sqlmodel import SQLModel, Session, create_engine

from src.database.models import User, Movie, StreamEvent
from src.model.recommender import fit
from src.model_updates.daily import run, digest, model_cutoff
from src.model_updates.evaluation import scores, compare, production_report, rank, FrozenProfiles
from src.model_updates.promotion import stage, finish
from src.model_updates.scheduler import due_day
from src.model_updates.snapshot import window, normalize_events, capture
from src.tests.test_service import make_model


class EvaluationTests(unittest.TestCase):
    def test_dst_windows_and_schedule(self):
        a, b = window(date(2026, 3, 8))
        self.assertEqual((b-a).total_seconds(), 23*3600)
        a, b = window(date(2026, 11, 1))
        self.assertEqual((b-a).total_seconds(), 25*3600)
        self.assertIsNone(due_day(datetime(2026, 11, 8, 6, 59, tzinfo=UTC)))
        self.assertEqual(due_day(datetime(2026, 11, 8, 7, tzinfo=UTC)), date(2026, 11, 7))

    def test_normalization_orders_offsets_deduplicates_and_rejects_bad_ratings(self):
        row = dict(timestamp='2026-01-01T03:00:00-05:00', user_id='1', movie_id='m', event_type='rating', rating=8)
        early = dict(row, timestamp='2026-01-01T07:00:00Z', rating=4)
        result = normalize_events([row, early, row])
        self.assertEqual([e['rating'] for e in result], [4, 8])
        with self.assertRaises(ValueError):
            normalize_events([dict(row, rating=float('nan'))])

    def test_metrics_and_common_exclusion_before_top20(self):
        self.assertEqual(scores(['a', 'b'], {'a'})['ndcg_at_20'], 1)
        self.assertAlmostEqual(scores(['b', 'a'], {'a'})['ndcg_at_20'], 1/np.log2(3))
        model = make_model()
        ranking = rank(model, 'new', FrozenProfiles([]), {'movie-21'})
        self.assertEqual(len(ranking), 20)
        self.assertNotIn('movie-21', ranking)
        self.assertIn('movie-1', ranking)

    def test_paired_comparison_and_cold_cohort(self):
        old, new = make_model(), make_model()
        new['popularity'] = np.arange(22, 0, -1, dtype=float)
        heldout = [dict(user_id=u, movie_id='movie-0', event_type='rating', rating=9)
                   for u in ['new-1', 'new-2']]
        report = compare(old, new, [], heldout, [], min_users=2)
        self.assertEqual(report['groups']['cold']['users'], 2)
        self.assertGreater(report['paired_delta_lower_95'], 0)
        same = compare(old, old, [], heldout, [], min_users=2)
        self.assertEqual(same['paired_delta_lower_95'], 0)
        insufficient = compare(old, new, [], heldout, [], min_users=100)
        self.assertIsNone(insufficient['paired_delta_lower_95'])

    def test_latest_rating_defines_relevance(self):
        heldout = [dict(user_id='new', movie_id='movie-0', event_type='rating', rating=r) for r in [10, 1]]
        report = compare(make_model(), make_model(), [], heldout, [], min_users=2)
        self.assertEqual(report['groups']['all']['users'], 0)

    def test_production_attribution_deduplicates_minutes_and_uses_latest_request(self):
        start, end = window(date(2026, 1, 1))
        first = dict(request_id='1', user_id='u', served_at=start.isoformat(),
                     recommendations=[{'movie_id': 'm'}], model_route='llm_content', serve_duration_ms=3)
        second = dict(first, request_id='2', served_at=(start+timedelta(minutes=10)).isoformat())
        event = dict(user_id='u', movie_id='m', event_type='watch', minute_index=0,
                     event_time=(start+timedelta(minutes=20)).isoformat())
        snapshot = dict(serves=[first, second], events=[event, event, dict(event, minute_index=1)],
                        usage=[], movies=[{'movie_id': 'm', 'license_cost': '.25'}])
        report = production_report(snapshot, start, end)
        self.assertEqual(report['converted_requests'], 1)
        self.assertEqual(report['recommendation_to_watch_rate'], .5)
        self.assertEqual(report['known_license_cost_usd'], .25)
        self.assertIsNone(report['failure_rate'])

    def test_database_snapshot_has_end_boundary_and_row_limit(self):
        engine = create_engine('sqlite://')
        self.addCleanup(engine.dispose)
        SQLModel.metadata.create_all(engine)
        start, end = window(date(2026, 1, 1))
        with Session(engine) as session:
            session.add(User(user_id='u'))
            session.add(Movie(movie_id='m', title='Space adventure'))
            session.add(StreamEvent(user_id='u', movie_id='m', event_type='rating', rating=8, event_time=start))
            session.add(StreamEvent(user_id='u', movie_id='m', event_type='rating', rating=9, event_time=end+timedelta(hours=1)))
            session.commit()
        snapshot = capture(engine, start, end)
        self.assertEqual(len(snapshot['events']), 1)
        with self.assertRaises(ValueError):
            capture(engine, start, end, limit=0)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'data'
        self.data.mkdir()
        self.users = [{'user_id': '1'}, {'user_id': '2'}]
        self.movies = [{'movie_id': str(i), 'title': title} for i, title in enumerate(
            ['Space adventure', 'Romantic comedy', 'Family drama'])]
        self.events = [dict(timestamp='2026-01-01T00:00:00Z', user_id=u, movie_id=m,
                            event_type='rating', rating=8) for u, m in [('1', '0'), ('2', '1')]]
        for name, rows in [('users', self.users), ('movies', self.movies), ('events', self.events)]:
            with gzip.open(self.data / (name + '.csv.gz'), 'wt', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader(); writer.writerows(rows)
        model = fit(self.events, self.users, self.movies)
        model['update_metadata'] = dict(version='baseline', training_end_exclusive='2026-01-02T00:00:00Z')
        joblib.dump(model, self.root / 'model.joblib')
        self.day = date(2026, 1, 3)
        self.snapshot = dict(users=self.users, movies=self.movies, events=[], serves=[], usage=[], profiles=[])

    def test_empty_live_data_reports_and_preserves_champion_idempotently(self):
        before = digest(self.root / 'model.joblib')
        report = run(self.day, self.root, self.data, self.snapshot)
        self.assertEqual(report['status'], 'insufficient_data')
        self.assertEqual(digest(self.root / 'model.joblib'), before)
        self.assertEqual(run(self.day, self.root, self.data, {'invalid': True}), report)

    def test_unknown_provenance_and_overlap_are_blocked(self):
        with self.assertRaises(ValueError):
            model_cutoff({}, self.root / 'model.joblib', self.events)
        model = joblib.load(self.root / 'model.joblib')
        model['update_metadata']['training_end_exclusive'] = '2026-01-04T00:00:00Z'
        joblib.dump(model, self.root / 'model.joblib')
        with self.assertRaises(ValueError):
            run(self.day, self.root, self.data, self.snapshot)
        report = json.loads((self.root / 'daily' / str(self.day) / 'report.json').read_text())
        self.assertFalse(report['eligible_for_manual_promotion'])

    def test_heldout_is_excluded_from_fit_and_refit_is_separate(self):
        event = dict(event_time='2026-01-03T12:00:00Z', stored_at='2026-01-03T12:00:01Z', user_id='1', movie_id='2', event_type='rating', rating=9)
        self.snapshot['events'] = [event]
        comparison = dict(groups={'all': {'users': 100}}, paired_delta_lower_95=.1, candidate_p95_ms=1)
        with patch('src.model_updates.daily.compare', return_value=comparison), \
             patch('src.model_updates.daily.detect_event_type_drift', return_value={'status': 'no_drift'}), \
             patch('src.model_updates.daily.detect_rating_drift', return_value={'status': 'no_drift'}), \
             patch('src.model_updates.daily.fit', wraps=fit) as training:
            report = run(self.day, self.root, self.data, self.snapshot)
        self.assertEqual(report['status'], 'awaiting_approval')
        self.assertEqual(len(training.call_args_list[0].args[0]), 2)
        self.assertEqual(len(training.call_args_list[1].args[0]), 3)
        self.assertEqual(joblib.load(self.root/'model.joblib')['update_metadata']['version'], 'baseline')
        stage(self.root, self.day)
        self.assertEqual(joblib.load(self.root/'model.joblib')['update_metadata']['version'], 'daily-2026-01-03')
        finish(self.root, self.day, rollback=True)
        self.assertEqual(joblib.load(self.root/'model.joblib')['update_metadata']['version'], 'baseline')

    def test_insufficient_ratings_never_creates_promotable_artifact(self):
        self.snapshot['events'] = [dict(event_time='2026-01-03T12:00:00Z', stored_at='2026-01-03T12:00:01Z', user_id='1', movie_id='2', event_type='watch')]
        report = run(self.day, self.root, self.data, self.snapshot)
        self.assertEqual(report['status'], 'retain_champion')
        with self.assertRaises(ValueError):
            stage(self.root, self.day)

    def test_api_reports_embedded_model_version(self):
        from src.service.api import create_app
        from src.service.store import ProfileStore
        app = create_app(model=joblib.load(self.root/'model.joblib'),
                         profile_store=ProfileStore())
        self.assertEqual(app.test_client().get('/health').json['model_version'], 'baseline')

    def test_late_arriving_history_is_not_used_to_train_evaluation_candidate(self):
        self.snapshot['events'] = [dict(event_time='2026-01-02T12:00:00Z', stored_at='2026-01-03T12:00:00Z',
                                       user_id='1', movie_id='2', event_type='rating', rating=9),
                                  dict(event_time='2026-01-03T12:00:00Z', stored_at='2026-01-03T12:00:00Z',
                                       user_id='2', movie_id='2', event_type='rating', rating=8)]
        with patch('src.model_updates.daily.fit', wraps=fit) as training:
            run(self.day, self.root, self.data, self.snapshot)
        self.assertEqual(len(training.call_args_list[0].args[0]), 2)

    def promotion_fixture(self):
        folder = self.root / 'daily' / str(self.day)
        folder.mkdir(parents=True)
        model = joblib.load(self.root/'model.joblib')
        model['update_metadata']['version'] = 'candidate'
        joblib.dump(model, folder/'candidate.joblib')
        (folder/'report.json').write_text(json.dumps(dict(
            status='awaiting_approval', eligible_for_manual_promotion=True, model_version='candidate',
            champion_sha256=digest(self.root/'model.joblib'), candidate_sha256=digest(folder/'candidate.joblib'))))
        return folder

    def test_changed_champion_or_candidate_prevents_promotion(self):
        folder = self.promotion_fixture()
        with (folder/'candidate.joblib').open('ab') as stream:
            stream.write(b'changed')
        with self.assertRaises(ValueError):
            stage(self.root, self.day)
        with (self.root/'model.joblib').open('ab') as stream:
            stream.write(b'changed')
        with self.assertRaises(ValueError):
            stage(self.root, self.day)
        self.assertFalse((self.root/'promotion-pending.json').exists())

    def test_completed_promotion_can_be_rolled_back_and_pending_blocks_second_stage(self):
        self.promotion_fixture()
        old = digest(self.root/'model.joblib')
        stage(self.root, self.day)
        with self.assertRaises(ValueError):
            stage(self.root, self.day)
        finish(self.root, self.day)
        finish(self.root, self.day, rollback=True)
        self.assertEqual(digest(self.root/'model.joblib'), old)
