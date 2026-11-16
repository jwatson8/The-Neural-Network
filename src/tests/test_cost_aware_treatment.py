"""Real candidate-path and Flask cost-aware tests using isolated synthetic fixtures."""

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

import numpy as np
from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

from src.database.interactions import experiments, experiment_assignments, movies, recommendation_serves, stream_events, users
from src.database.schemas import ExperimentCreate
from src.experimentation.cost_reranking import POLICY, CostAwareConfig, treatment_version
from src.service.api import create_app
from src.service.cold_start import recommend_cold_start
from src.service.cost_aware_treatment import CostAwareTreatmentServer
from src.service.experiment_routing import ServingRequest
from src.service.model_adapter import recommend
from src.service.recommendation_candidates import LoadedModelCandidates
from src.service.store import ProfileStore
from src.tests.test_service import make_model, ContentMatrix


def scoring_model(count=60):
    model = make_model()
    model['movies'] = [{'movie_id': f'movie-{i}'} for i in range(count)]
    model['movie_factors'] = np.array([np.linspace(1, 0, count), np.linspace(0, 1, count)])
    model['popularity'] = np.linspace(1, 0, count)
    model['content'] = ContentMatrix(np.array([[1 if i % 3 == j else 0 for j in range(3)] for i in range(count)], dtype=float))
    model['seen']['warm'] = set(range(10))
    model['update_metadata'] = {'version': 'trained-model-v1'}
    return model


class CostAwareServingTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        event.listen(self.engine, 'connect', lambda connection, _: connection.execute('PRAGMA foreign_keys=ON'))
        SQLModel.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        engine_patch = patch('src.database.database.get_engine', return_value=self.engine)
        engine_patch.start()
        self.addCleanup(engine_patch.stop)
        self.model = scoring_model()
        self.store = ProfileStore()
        movies.create_missing_many([dict(movie_id=movie['movie_id'], title=movie['movie_id'],
            license_cost=Decimal('10' if i % 3 == 0 else '.1')) for i, movie in enumerate(self.model['movies'])])
        self.config = CostAwareConfig()
        self.version = treatment_version('trained-model-v1', self.config)
        now = datetime.now(UTC)
        self.experiment = experiments.create(ExperimentCreate(name='cost-rerank', status='running',
            starts_at=now-timedelta(hours=1), ends_at=now+timedelta(hours=1), traffic_allocation='1', treatment_allocation='1',
            control_policy='legacy', control_version='trained-model-v1', treatment_policy=POLICY, treatment_version=self.version))

    def app(self, **config):
        settings = dict(TESTING=True, EXPERIMENTS_ENABLED=True,
                        EXPERIMENT_ID=str(self.experiment.experiment_id), COST_AWARE_TREATMENT_ENABLED=True)
        settings.update(config)
        return create_app(model=self.model, profile_store=self.store, config=settings)

    def history(self, user='warm'):
        users.create_missing_many([{'user_id': user}])
        stream_events.create_many([dict(user_id=user, event_type='watch', movie_id=f'movie-{i}', event_time=datetime.now(UTC)) for i in range(10)])

    def serve(self, user, app=None):
        response = (app or self.app()).test_client().get('/recommend/' + user)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'text/plain')
        ids = response.get_data(as_text=True).split(',')
        self.assertEqual(len(ids), 20)
        self.assertEqual(len(set(ids)), 20)
        records = recommendation_serves.search(user_id=user).records
        self.assertEqual(len(records), 1)
        self.assertEqual([item['movie_id'] for item in records[0].recommendations], ids)
        return ids, records[0]

    def test_svd_pipeline_retains_personalization_and_history_exclusions(self):
        self.history()
        ids, record = self.serve('warm')
        self.assertEqual(record.model_route, 'cost_aware:collaborative_svd')
        self.assertEqual(record.model_version, 'trained-model-v1')
        self.assertIsNotNone(record.experiment_assignment_id)
        self.assertFalse(set(ids).intersection({f'movie-{i}' for i in range(10)}))
        source = LoadedModelCandidates(self.model, self.store)
        request = ServingRequest('warm', frozenset({'movie-10'}), k=2, has_history=True)
        before = source(request, 10)
        np.testing.assert_allclose([item.score for item in before.candidates],
                                  [self.model['movie_factors'][0, int(item.movie_id.split('-')[1])] for item in before.candidates])
        self.assertNotIn('movie-10', [item.movie_id for item in before.candidates])
        self.model['user_factors'][0] = [0, 1]
        after = source(request, 10)
        self.assertNotEqual(before.candidates[0].movie_id, after.candidates[0].movie_id)

    def test_saved_profile_uses_actual_content_scores_and_cost_treatment(self):
        self.store.save_profile('cold', 'action', 'drama')
        ids, record = self.serve('cold')
        self.assertEqual(record.model_route, 'cost_aware:llm_content')
        self.assertIsNotNone(record.experiment_assignment_id)
        pool = LoadedModelCandidates(self.model, self.store)(ServingRequest('cold', frozenset(), k=2), 30)
        for item in pool.candidates:
            index = int(item.movie_id.split('-')[1])
            expected = (1 if index % 3 == 0 else 0) - .7 * (1 if index % 3 == 1 else 0) + .02 * self.model['popularity'][index]
            self.assertAlmostEqual(item.score, expected)
        self.assertEqual(len(ids), 20)

    def test_popularity_uses_actual_bundle_scores_and_queues_missing_profile(self):
        _, record = self.serve('new-user')
        self.assertEqual(record.model_route, 'cost_aware:popularity_fallback')
        self.assertIsNotNone(record.experiment_assignment_id)
        self.assertEqual(self.store.claim_next()['user_id'], 'new-user')
        pool = LoadedModelCandidates(self.model, self.store)(ServingRequest('other', frozenset()), 100)
        self.assertEqual(len(pool.candidates), 60)
        self.assertEqual(pool.candidates[0].score, self.model['popularity'][0])

    def test_zero_weight_same_candidates_preserves_all_control_path_orderings(self):
        self.history()
        self.store.save_profile('cold', 'action', 'drama')
        for user, has_history in (('warm', True), ('cold', False), ('new', False)):
            with self.subTest(user=user):
                context = ServingRequest(user, frozenset(), k=20, has_history=has_history)
                source = LoadedModelCandidates(self.model, self.store)
                pool = source(context, 100)
                from src.experimentation.cost_reranking import rerank
                ranked = rerank(pool.candidates, movies.get_license_costs(item.movie_id for item in pool.candidates), k=20, config=CostAwareConfig(0))
                if has_history:
                    control, _ = recommend(self.model, user)
                else:
                    result = recommend_cold_start(self.model, self.store, user, allow_svd=False)
                    control, _ = result if result is not None else recommend(self.model, user, allow_svd=False)
                self.assertEqual([item.movie_id for item in ranked.selected], control)

    def test_reference_cost_lookup_is_one_batch_query_and_not_observed_charges(self):
        queries = []
        def observe(conn, cursor, statement, parameters, context, many):
            if 'FROM movies' in statement:
                queries.append(statement)
        event.listen(self.engine, 'before_cursor_execute', observe)
        self.addCleanup(event.remove, self.engine, 'before_cursor_execute', observe)
        with patch('src.database.interactions.licensing_costs.search', side_effect=AssertionError('ledger is not ranking metadata')):
            _, record = self.serve('new')
        self.assertIsNotNone(record.experiment_assignment_id)
        self.assertEqual(len(queries), 1)
        self.assertIn(' IN ', queries[0])

    def test_batch_catalog_lookup_distinguishes_missing_null_and_zero(self):
        movies.update('movie-0', license_cost=None)
        movies.update('movie-1', license_cost=Decimal(0))
        self.assertEqual(movies.get_license_costs(['movie-0', 'movie-1', 'absent', 'movie-1']),
                         {'movie-0': None, 'movie-1': Decimal(0)})
        with patch('src.database.interactions.movies.run', side_effect=AssertionError('empty lookup must not connect')):
            self.assertEqual(movies.get_license_costs([]), {})
        with self.assertRaises(ValueError):
            movies.get_license_costs([str(i) for i in range(501)])

    def test_cost_database_failure_falls_back_without_exposure(self):
        with patch('src.database.interactions.movies.get_license_costs', side_effect=SQLAlchemyError('lookup failed')):
            _, record = self.serve('new')
        self.assertEqual(record.model_route, 'popularity_fallback')
        self.assertEqual(record.model_version, 'trained-model-v1')
        self.assertIsNone(record.experiment_assignment_id)
        self.assertEqual(len(experiment_assignments.list_records()), 1)

    def test_unknown_costs_or_nonexistent_catalog_rows_cannot_fulfill(self):
        for user, result in (('unknown', {f'movie-{i}': None for i in range(60)}), ('missing', {})):
            with patch('src.database.interactions.movies.get_license_costs', return_value=result):
                _, record = self.serve(user)
            self.assertIsNone(record.experiment_assignment_id)
            self.assertEqual(record.model_route, 'popularity_fallback')

    def test_sparse_candidate_pool_and_source_failure_fall_back(self):
        from src.service.recommendation_candidates import CandidatePool
        for user, kwargs in (('empty', {'return_value': CandidatePool((), 'popularity_fallback')}),
                             ('error', {'side_effect': RuntimeError('source failure')})):
            with patch.object(LoadedModelCandidates, '__call__', **kwargs):
                _, record = self.serve(user)
            self.assertIsNone(record.experiment_assignment_id)
            self.assertEqual(record.model_route, 'popularity_fallback')

    def test_policy_version_configuration_or_model_mismatch_cannot_expose(self):
        app = self.app(COST_AWARE_WEIGHT='.2')
        _, record = self.serve('changed-weight', app)
        self.assertIsNone(record.experiment_assignment_id)
        self.assertEqual(record.model_version, 'trained-model-v1')
        self.model['update_metadata']['version'] = 'trained-model-v2'
        _, record = self.serve('changed-model')
        self.assertIsNone(record.experiment_assignment_id)
        self.assertEqual(record.model_version, 'trained-model-v2')

    def test_policy_version_and_underlying_model_are_distinct_and_truthful(self):
        app = self.app()
        adapter = app.extensions['recommendation_router'].treatment
        self.assertIsInstance(adapter, CostAwareTreatmentServer)
        self.assertEqual(adapter.policy_version, self.version)
        self.assertNotEqual(adapter.policy_version, adapter.model_version)
        with self.assertLogs('src.service.api', level='INFO') as logs:
            _, record = self.serve('new', app)
        self.assertEqual(record.model_version, 'trained-model-v1')
        self.assertTrue(any(f'policy_version={self.version}' in message for message in logs.output))
        self.assertTrue(any('underlying_route=popularity_fallback' in message for message in logs.output))

    def test_disabled_adapter_and_control_remain_unaffected(self):
        for user, settings in (('disabled', {'EXPERIMENTS_ENABLED': False}),
                               ('not-registered', {'COST_AWARE_TREATMENT_ENABLED': False})):
            app = create_app(model=self.model, profile_store=self.store, config=dict(TESTING=True,
                EXPERIMENTS_ENABLED=settings.get('EXPERIMENTS_ENABLED', True),
                EXPERIMENT_ID=str(self.experiment.experiment_id),
                COST_AWARE_TREATMENT_ENABLED=settings.get('COST_AWARE_TREATMENT_ENABLED', True)))
            with patch('src.database.interactions.movies.get_license_costs', side_effect=AssertionError('no ranking lookup')):
                ids, record = self.serve(user, app)
            self.assertEqual(ids[0], 'movie-0')
            self.assertEqual(record.model_route, 'popularity_fallback')
            self.assertIsNone(record.experiment_assignment_id)


    def test_control_assignment_never_invokes_cost_adapter(self):
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE experiments SET treatment_allocation=0"))
        with patch('src.database.interactions.movies.get_license_costs', side_effect=AssertionError('Control must not rerank')):
            ids, record = self.serve('control')
        self.assertEqual(ids[0], 'movie-0')
        self.assertEqual(record.model_route, 'popularity_fallback')
        self.assertIsNotNone(record.experiment_assignment_id)

    def test_invalid_model_scores_duplicate_ids_and_nonexistent_rows_are_not_served(self):
        self.model['movies'][1]['movie_id'] = 'not-in-database'
        self.model['movies'][2]['movie_id'] = 'movie-3'
        self.model['popularity'][4] = float('nan')
        self.model['movies'][5]['movie_id'] = 'invalid\nmovie'
        ids, record = self.serve('new')
        self.assertIsNotNone(record.experiment_assignment_id)
        self.assertNotIn('not-in-database', ids)
        self.assertNotIn('movie-4', ids)
        self.assertNotIn('invalid\nmovie', ids)
        self.assertEqual(len(ids), len(set(ids)))

    def test_legacy_candidate_k_limit_and_expanded_source_bounds(self):
        with self.assertRaises(ValueError):
            recommend(self.model, 'warm', k=21)
        source = LoadedModelCandidates(self.model, self.store)
        request = ServingRequest('new', frozenset())
        for limit in (0, 201):
            with self.assertRaises(ValueError):
                source(request, limit)
        with self.assertRaises(ValueError):
            LoadedModelCandidates({}, self.store)(request, 20)

    def test_missing_loaded_model_preserves_existing_503(self):
        app = self.app()
        app.extensions['recommendation_model'] = None
        response = app.test_client().get('/recommend/new')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(recommendation_serves.list_records(), [])
        self.assertEqual(experiment_assignments.list_records(), [])

    def test_empty_saved_profile_remains_popularity_and_profile_errors_do_not_expose(self):
        self.store.save_profile('blank', '', '')
        _, record = self.serve('blank')
        self.assertEqual(record.model_route, 'cost_aware:popularity_fallback')
        with patch.object(self.store, 'get_profile', side_effect=RuntimeError('profile source unavailable')):
            _, record = self.serve('unavailable')
        self.assertIsNone(record.experiment_assignment_id)
        self.assertEqual(record.model_route, 'popularity_fallback')

    def test_custom_hosting_adapter_remains_injectable_and_registration_is_explicit(self):
        with self.assertRaises(ValueError):
            create_app(model=self.model, profile_store=self.store, treatment_server=Mock(), config={
                'EXPERIMENTS_ENABLED': True, 'EXPERIMENT_ID': str(self.experiment.experiment_id),
                'COST_AWARE_TREATMENT_ENABLED': True})
        with self.assertRaises(ValueError):
            self.app(COST_AWARE_TREATMENT_ENABLED='true')
        with self.assertRaises(ValueError):
            self.app(COST_AWARE_WEIGHT='NaN')


    def test_reranking_exception_and_unknown_policy_fall_back_without_exposure(self):
        with patch('src.service.cost_aware_treatment.rerank', side_effect=RuntimeError('ranking failed')):
            _, record = self.serve('ranking-error')
        self.assertIsNone(record.experiment_assignment_id)
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE experiments SET treatment_policy='unsupported-policy'"))
        with patch.object(LoadedModelCandidates, '__call__', side_effect=AssertionError('policy must be checked first')):
            _, record = self.serve('bad-policy')
        self.assertIsNone(record.experiment_assignment_id)

    def test_insufficient_nonempty_pool_does_not_claim_treatment(self):
        from src.experimentation.cost_reranking import ScoredCandidate
        from src.service.recommendation_candidates import CandidatePool
        pool = CandidatePool((ScoredCandidate('movie-0', 1.0),), 'popularity_fallback')
        with patch.object(LoadedModelCandidates, '__call__', return_value=pool):
            _, record = self.serve('insufficient')
        self.assertIsNone(record.experiment_assignment_id)
        self.assertEqual(record.model_route, 'popularity_fallback')
