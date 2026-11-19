"""Offline Flask integration with real assignments/audits and injected serving."""

import unittest
from dataclasses import replace
from decimal import Decimal
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

from src.database.interactions import (
    experiment_assignments, experiments, movies, recommendation_serves,
    stream_events, users,
)
from src.database.schemas import ExperimentCreate
from src.experimentation.assignment import assigned_variant
from src.experimentation.cost_reranking import POLICY, CostAwareConfig, treatment_version
from src.experimentation.decisions import DecisionReason, ExperimentDecision
from src.experimentation.manager import ExperimentManager
from src.service.api import create_app
from src.service.experiment_routing import ServingResult
from src.service.model_adapter import recommend
from src.service.store import ProfileStore
from src.tests.test_model_registry import activate, install, write_bundle
from src.tests.test_service import make_model


class ExperimentApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        event.listen(self.engine, 'connect', lambda connection, _: connection.execute('PRAGMA foreign_keys=ON'))
        SQLModel.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        engine_patch = patch('src.database.database.get_engine', return_value=self.engine)
        engine_patch.start()
        self.addCleanup(engine_patch.stop)
        self.store = ProfileStore()
        self.model = make_model()
        movies.create_missing_many([{'movie_id': movie['movie_id'], 'title': movie['movie_id']}
                                    for movie in self.model['movies']])
        self.now = datetime.now(UTC)

    def experiment(self, **overrides):
        return experiments.create(ExperimentCreate(**(dict(
            name=str(uuid4()), status='running', starts_at=self.now - timedelta(hours=1),
            ends_at=self.now + timedelta(hours=1), traffic_allocation='1', treatment_allocation='0',
            control_policy='legacy', control_version='baseline-v1',
            treatment_policy='stub-treatment', treatment_version='treatment-v1',
        ) | overrides)))

    def app(self, experiment=None, *, enabled=True, provider=None, eligibility=None, manager=None, config=None):
        settings = dict(TESTING=True, MODEL_VERSION='baseline-v1', EXPERIMENTS_ENABLED=enabled,
                        EXPERIMENT_ID=str(experiment.experiment_id if experiment else uuid4()))
        settings.update(config or {})
        return create_app(model=self.model, profile_store=self.store, config=settings,
                          treatment_server=provider, experiment_eligibility=eligibility, experiment_manager=manager)

    def treatment_result(self, **overrides):
        return replace(ServingResult(('movie-1', 'movie-2'), 'treatment-v1', 'stub_route',
                                     'stub-treatment', fulfilled=True), **overrides)

    def audit(self):
        records = recommendation_serves.list_records()
        self.assertEqual(len(records), 1)
        return records[0]

    def assert_plain(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'text/plain')
        body = response.get_data(as_text=True)
        self.assertNotIn('\n', body)
        self.assertNotIn('\r', body)
        self.assertTrue(1 <= len(body.split(',')) <= 20)
        self.assertEqual(len(body.split(',')), len(set(body.split(','))))
        self.assertEqual([item['movie_id'] for item in self.audit().recommendations], body.split(','))

    def test_disabled_has_no_experiment_queries_or_manager_or_provider_calls(self):
        provider = Mock(side_effect=AssertionError('Treatment must not run'))
        with patch.object(ExperimentManager, 'decide', side_effect=AssertionError('Manager must not run')):
            with patch('src.database.interactions.experiments.list_records', side_effect=AssertionError('No discovery')):
                response = self.app(enabled=False, provider=provider,
                                    config={'EXPERIMENT_ID': 'ignored-when-disabled'}).test_client().get('/recommend/warm')
        self.assert_plain(response)
        self.assertEqual(response.get_data(as_text=True).split(',')[0], 'movie-21')
        self.assertEqual(self.audit().model_route, 'popularity_fallback')
        self.assertEqual(self.audit().model_version, 'baseline-v1')
        self.assertIsNone(self.audit().experiment_assignment_id)
        self.assertEqual(experiment_assignments.list_records(), [])
        provider.assert_not_called()

    def test_no_active_experiment_uses_normal_serving(self):
        provider = Mock()
        response = self.app(provider=provider).test_client().get('/recommend/warm')
        self.assert_plain(response)
        self.assertIsNone(self.audit().experiment_assignment_id)
        provider.assert_not_called()

    def test_control_decision_uses_existing_pipeline_and_persistent_attribution(self):
        experiment = self.experiment()
        original = ExperimentManager.decide
        with patch.object(ExperimentManager, 'decide', autospec=True, side_effect=original) as decide:
            response = self.app(experiment).test_client().get('/recommend/warm')
        self.assert_plain(response)
        decide.assert_called_once()
        self.assertTrue(decide.call_args.kwargs['eligible'])
        assignment = experiment_assignments.get_for_user(experiment_id=experiment.experiment_id, user_id='warm')
        self.assertEqual(assignment.variant, 'control')
        self.assertEqual(self.audit().experiment_assignment_id, assignment.assignment_id)
        self.assertEqual(self.audit().model_version, 'baseline-v1')
        self.assertEqual(self.audit().model_route, 'popularity_fallback')

    def test_treatment_injection_returns_actual_output_and_metadata(self):
        experiment = self.experiment(treatment_allocation='1')
        provider = Mock(return_value=self.treatment_result())
        response = self.app(experiment, provider=provider).test_client().get('/recommend/warm')
        self.assert_plain(response)
        self.assertEqual(response.get_data(as_text=True), 'movie-1,movie-2')
        context, decision = provider.call_args.args
        self.assertEqual(context.user_id, 'warm')
        self.assertEqual(context.k, 20)
        self.assertIn('movie-0', context.exclude_movie_ids)
        self.assertEqual((decision.variant, decision.policy, decision.version),
                         ('treatment', 'stub-treatment', 'treatment-v1'))
        self.assertEqual(self.audit().experiment_assignment_id, decision.assignment_id)
        self.assertEqual(self.audit().model_route, 'stub_route')
        self.assertEqual(self.audit().model_version, 'treatment-v1')
        provider.assert_called_once()

    def test_repeated_requests_are_sticky_and_log_once_per_request(self):
        experiment = self.experiment(treatment_allocation='1')
        provider = Mock(return_value=self.treatment_result())
        client = self.app(experiment, provider=provider).test_client()
        self.assertEqual(client.get('/recommend/warm').status_code, 200)
        with self.engine.begin() as connection:
            connection.execute(text('UPDATE experiments SET traffic_allocation=0, treatment_allocation=0'))
        self.assertEqual(client.get('/recommend/warm').status_code, 200)
        assignments = experiment_assignments.list_records()
        records = recommendation_serves.list_records()
        self.assertEqual(len(assignments), 1)
        self.assertEqual(assignments[0].variant, 'treatment')
        self.assertEqual(len(records), 2)
        self.assertEqual({row.experiment_assignment_id for row in records}, {assignments[0].assignment_id})
        self.assertEqual(len({row.request_id for row in records}), 2)
        self.assertEqual(provider.call_count, 2)

    def test_traffic_exclusion_has_no_assignment_or_exposure(self):
        provider = Mock()
        response = self.app(self.experiment(traffic_allocation='0'), provider=provider).test_client().get('/recommend/warm')
        self.assert_plain(response)
        self.assertEqual(experiment_assignments.list_records(), [])
        self.assertIsNone(self.audit().experiment_assignment_id)
        provider.assert_not_called()

    def test_ineligible_user_has_no_assignment_or_exposure(self):
        eligibility, provider = Mock(return_value=False), Mock()
        response = self.app(self.experiment(), provider=provider, eligibility=eligibility).test_client().get('/recommend/warm')
        self.assert_plain(response)
        eligibility.assert_called_once()
        self.assertEqual(experiment_assignments.list_records(), [])
        self.assertIsNone(self.audit().experiment_assignment_id)
        provider.assert_not_called()

    def test_lifecycle_and_time_windows_never_expose_or_invoke_treatment(self):
        settings = [dict(status=status) for status in ('draft', 'paused', 'completed', 'aborted')]
        settings += [dict(starts_at=self.now + timedelta(minutes=30)),
                     dict(starts_at=self.now - timedelta(days=1), ends_at=self.now - timedelta(seconds=1)),
                     dict(starts_at=None, ends_at=None)]
        for overrides in settings:
            with self.subTest(overrides=overrides):
                provider = Mock()
                response = self.app(self.experiment(treatment_allocation='1', **overrides), provider=provider).test_client().get('/recommend/warm')
                self.assertEqual(response.status_code, 200)
                provider.assert_not_called()
        self.assertEqual(experiment_assignments.list_records(), [])
        self.assertTrue(all(row.experiment_assignment_id is None for row in recommendation_serves.list_records()))
        self.assertEqual(len(recommendation_serves.list_records()), len(settings))

    def test_unsupported_treatment_falls_back_with_intent_but_no_exposure(self):
        experiment = self.experiment(treatment_allocation='1')
        with self.assertLogs('src.service.api', level='INFO') as logs:
            response = self.app(experiment).test_client().get('/recommend/warm')
        self.assert_plain(response)
        self.assertIsNone(self.audit().experiment_assignment_id)
        self.assertEqual((self.audit().model_route, self.audit().model_version), ('popularity_fallback', 'baseline-v1'))
        assignment = experiment_assignments.list_records()[0]
        self.assertEqual(assignment.variant, 'treatment')
        self.assertTrue(any('fallback_path=unsupported_treatment:popularity_fallback' in line for line in logs.output))
        self.assertTrue(any(f'intended_assignment_id={assignment.assignment_id}' in line for line in logs.output))

    def test_treatment_prediction_exception_uses_baseline_without_exposure(self):
        provider = Mock(side_effect=RuntimeError('private hosting detail'))
        response = self.app(self.experiment(treatment_allocation='1'), provider=provider).test_client().get('/recommend/warm')
        self.assert_plain(response)
        self.assertIsNone(self.audit().experiment_assignment_id)
        self.assertEqual(self.audit().model_version, 'baseline-v1')
        self.assertNotIn('private hosting detail', response.get_data(as_text=True))

    def test_unfulfilled_or_invalid_treatment_never_becomes_exposure(self):
        results = [self.treatment_result(model_version='different-version'),
                   self.treatment_result(policy='different-policy'),
                   self.treatment_result(fulfilled=False),
                   self.treatment_result(fallback_path='host_popularity_fallback'),
                   self.treatment_result(recommendations=()),
                   self.treatment_result(recommendations=('movie-1', 'movie-1')),
                   self.treatment_result(recommendations=('bad\nmovie',)),
                   self.treatment_result(recommendations=('movie-1,movie-2',)),
                   self.treatment_result(recommendations=('movie-0',)),
                   self.treatment_result(model_route=''), None]
        experiment = self.experiment(treatment_allocation='1')
        for result in results:
            with self.subTest(result=result):
                response = self.app(experiment, provider=Mock(return_value=result)).test_client().get('/recommend/warm')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_data(as_text=True).split(',')[0], 'movie-21')
        records = recommendation_serves.list_records()
        self.assertEqual(len(records), len(results))
        self.assertTrue(all(row.experiment_assignment_id is None and row.model_version == 'baseline-v1' for row in records))

    def test_control_version_or_policy_mismatch_does_not_fabricate_exposure(self):
        for overrides in (dict(control_version='not-loaded'), dict(control_policy='not-legacy')):
            response = self.app(self.experiment(**overrides)).test_client().get('/recommend/warm')
            self.assertEqual(response.status_code, 200)
        records = recommendation_serves.list_records()
        self.assertEqual(len(records), 2)
        self.assertTrue(all(row.experiment_assignment_id is None and row.model_version == 'baseline-v1' for row in records))

    def test_control_inference_failure_preserves_500_without_serve_or_exposure(self):
        with patch('src.service.api.recommend', side_effect=RuntimeError('private inference failure')):
            response = self.app(self.experiment()).test_client().get('/recommend/warm')
        self.assertEqual(response.status_code, 500)
        self.assertNotIn('private inference failure', response.get_data(as_text=True))
        self.assertEqual(recommendation_serves.list_records(), [])
        self.assertEqual(len(experiment_assignments.list_records()), 1)

    def test_invalid_control_output_returns_500_without_exposure(self):
        with patch('src.service.api.recommend', return_value=([], 'popularity_fallback')):
            response = self.app(self.experiment()).test_client().get('/recommend/warm')
        self.assertEqual(response.status_code, 500)
        self.assertEqual(recommendation_serves.list_records(), [])

    def test_logging_failure_is_503_and_never_retries_or_logs_success(self):
        with patch('src.service.api.recommendation_serves.create', side_effect=SQLAlchemyError('audit unavailable')) as audit:
            response = self.app(self.experiment()).test_client().get('/recommend/warm')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('audit unavailable', response.get_data(as_text=True))
        audit.assert_called_once()
        self.assertEqual(recommendation_serves.list_records(), [])
        self.assertEqual(len(experiment_assignments.list_records()), 1)

    def test_experiment_database_errors_fall_back_without_treatment(self):
        provider = Mock()
        manager = Mock()
        manager.decide.side_effect = SQLAlchemyError('assignment unavailable')
        response = self.app(self.experiment(treatment_allocation='1'), provider=provider, manager=manager).test_client().get('/recommend/warm')
        self.assert_plain(response)
        self.assertIsNone(self.audit().experiment_assignment_id)
        provider.assert_not_called()
        self.assertEqual(experiment_assignments.list_records(), [])

    def test_eligibility_error_or_invalid_result_does_not_run_treatment(self):
        experiment = self.experiment(treatment_allocation='1')
        for callback in (Mock(side_effect=RuntimeError('eligibility failed')), Mock(return_value='yes')):
            provider = Mock()
            response = self.app(experiment, provider=provider, eligibility=callback).test_client().get('/recommend/warm')
            self.assertEqual(response.status_code, 200)
            provider.assert_not_called()
        self.assertEqual(experiment_assignments.list_records(), [])
        self.assertTrue(all(row.experiment_assignment_id is None for row in recommendation_serves.list_records()))

    def test_nonexistent_assignment_is_never_referenced_by_a_serve(self):
        experiment = self.experiment()
        manager = Mock()
        manager.decide.return_value = ExperimentDecision(experiment.experiment_id, DecisionReason.ASSIGNED,
            assignment_id=uuid4(), variant='control', policy='legacy', version='baseline-v1')
        response = self.app(experiment, manager=manager).test_client().get('/recommend/warm')
        self.assertEqual(response.status_code, 500)
        self.assertEqual(recommendation_serves.list_records(), [])

    def test_explicit_experiment_selection_ignores_other_active_experiments(self):
        selected = self.experiment()
        other = self.experiment(treatment_allocation='1')
        provider = Mock()
        response = self.app(selected, provider=provider).test_client().get('/recommend/warm')
        self.assert_plain(response)
        provider.assert_not_called()
        assignment = experiment_assignments.list_records()[0]
        self.assertEqual(assignment.experiment_id, selected.experiment_id)
        self.assertNotEqual(assignment.experiment_id, other.experiment_id)
        with self.assertRaisesRegex(ValueError, 'one explicit EXPERIMENT_ID'):
            self.app(config={'EXPERIMENT_ID': None})
        with self.assertRaises(ValueError):
            self.app(config={'EXPERIMENT_ID': [selected.experiment_id, other.experiment_id]})
        with self.assertRaises(ValueError):
            self.app(config={'EXPERIMENTS_ENABLED': 'false'})

    def test_control_keeps_svd_history_and_profile_routes(self):
        self.model['seen']['warm'] = set(range(10))
        users.create(user_id='warm')
        stream_events.create_many([dict(user_id='warm', event_type='watch', movie_id=f'movie-{index}',
                                        event_time=self.now) for index in range(10)])
        response = self.app(self.experiment()).test_client().get('/recommend/warm')
        self.assert_plain(response)
        self.assertEqual(self.audit().model_route, 'collaborative_svd')
        self.assertIsNotNone(self.audit().experiment_assignment_id)
        self.assertFalse({f'movie-{index}' for index in range(10)}.intersection(response.get_data(as_text=True).split(',')))

    def test_control_keeps_saved_profile_personalization(self):
        self.store.save_profile('profile-user', 'unique-tag', '')
        response = self.app(self.experiment()).test_client().get('/recommend/profile-user')
        self.assert_plain(response)
        self.assertEqual(response.get_data(as_text=True).split(',')[0], 'movie-3')
        self.assertEqual(self.audit().model_route, 'llm_content')
        self.assertIsNotNone(self.audit().experiment_assignment_id)

    def test_cold_store_failure_preserves_popularity_fallback_attribution(self):
        with patch.object(self.store, 'get_profile', side_effect=RuntimeError('profile unavailable')):
            response = self.app(self.experiment()).test_client().get('/recommend/warm')
        self.assert_plain(response)
        self.assertEqual(self.audit().model_route, 'popularity_fallback')
        self.assertIsNotNone(self.audit().experiment_assignment_id)

    def test_default_app_and_kill_switch_do_not_use_an_injected_enabled_manager(self):
        manager = Mock(side_effect=AssertionError('must not call'))
        manager.decide.side_effect = AssertionError('must not call')
        app = self.app(self.experiment(), manager=manager)
        app.config['EXPERIMENTS_ENABLED'] = False
        self.assertEqual(app.test_client().get('/recommend/warm').status_code, 200)
        self.assertIsNone(self.audit().experiment_assignment_id)
        manager.decide.assert_not_called()


    def test_real_audit_transaction_rolls_back_after_insert_failure(self):
        original = recommendation_serves._create

        def fail_after_insert(session, data, request_started_at):
            original(session, data, request_started_at)
            raise SQLAlchemyError('failure after audit insert')

        with patch('src.database.interactions.recommendation_serves._create', side_effect=fail_after_insert):
            response = self.app(self.experiment()).test_client().get('/recommend/warm')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(recommendation_serves.list_records(), [])
        self.assertEqual(len(experiment_assignments.list_records()), 1)

    def test_embedded_model_version_is_actual_even_when_config_requests_another(self):
        self.model['update_metadata'] = {'version': 'embedded-v2'}
        experiment = self.experiment(control_version='baseline-v1')
        response = self.app(experiment).test_client().get('/recommend/warm')
        self.assert_plain(response)
        self.assertEqual(self.audit().model_version, 'embedded-v2')
        self.assertIsNone(self.audit().experiment_assignment_id)

    def test_request_validation_and_model_unavailability_do_not_assign(self):
        app = self.app(self.experiment(treatment_allocation='1'), provider=Mock())
        client = app.test_client()
        self.assertEqual(client.get('/recommend/warm?variant=treatment').status_code, 400)
        app.extensions['recommendation_model'] = None
        self.assertEqual(client.get('/recommend/warm').status_code, 503)
        self.assertEqual(experiment_assignments.list_records(), [])
        self.assertEqual(recommendation_serves.list_records(), [])

    def test_baseline_history_database_error_preserves_503_and_does_not_route(self):
        provider = Mock()
        app = self.app(self.experiment(treatment_allocation='1'), provider=provider)
        with patch('src.service.api.user_movie_history.get_user_history', side_effect=SQLAlchemyError('history failure')):
            response = app.test_client().get('/recommend/warm')
        self.assertEqual(response.status_code, 503)
        provider.assert_not_called()
        self.assertEqual(experiment_assignments.list_records(), [])
        self.assertEqual(recommendation_serves.list_records(), [])

    def test_live_history_exclusions_are_passed_to_treatment_and_enforced(self):
        users.create(user_id='warm')
        stream_events.create(user_id='warm', event_type='watch', event_time=self.now, movie_id='movie-1')
        provider = Mock(return_value=self.treatment_result())
        response = self.app(self.experiment(treatment_allocation='1'), provider=provider).test_client().get('/recommend/warm')
        self.assert_plain(response)
        self.assertIn('movie-1', provider.call_args.args[0].exclude_movie_ids)
        self.assertNotIn('movie-1', response.get_data(as_text=True).split(','))
        self.assertIsNone(self.audit().experiment_assignment_id)

    def test_unknown_actual_version_cannot_create_a_control_exposure(self):
        response = self.app(self.experiment(control_version='unknown'), config={'MODEL_VERSION': 'unknown'}).test_client().get('/recommend/warm')
        self.assert_plain(response)
        self.assertEqual(self.audit().model_version, 'unknown')
        self.assertIsNone(self.audit().experiment_assignment_id)

    def test_pinned_bundles_and_default_switch_keep_request_provenance(self):
        experiment = self.experiment(
            experiment_id=UUID(int=4), control_version='A', treatment_allocation='0.5',
            treatment_policy=POLICY, treatment_version=treatment_version('B', CostAwareConfig()),
        )
        self.assertEqual(assigned_variant(experiment.experiment_id, 'warm', Decimal('.5')), 'treatment')
        self.assertEqual(assigned_variant(experiment.experiment_id, 'other', Decimal('.5')), 'control')

        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            live = root / 'model.joblib'
            write_bundle(live, 'A')
            install(root, 'A')
            install(root, 'B', 1)
            candidate = install(root, 'C', 2)
            settings = dict(
                MODEL_PATH=str(live), EXPERIMENT_ID=str(experiment.experiment_id),
                EXPERIMENTS_ENABLED=True, COST_AWARE_TREATMENT_ENABLED=True,
                CONTROL_MODEL_VERSION='A', TREATMENT_MODEL_VERSION='B',
            )
            app = create_app(profile_store=self.store, config=settings,
                             experiment_eligibility=lambda context: context.user_id != 'outside')
            registry = app.extensions['model_registry']
            entered, release = Event(), Event()

            def hold(bundle, user_id, **kwargs):
                if user_id == 'outside' and not release.is_set():
                    entered.set()
                    assert release.wait(5)
                return recommend(bundle, user_id, **kwargs)

            statuses = []

            def request_default():
                statuses.append(app.test_client().get('/recommend/outside').status_code)

            with patch('src.service.api.recommend', side_effect=hold), patch.object(
                    movies, 'get_license_costs', side_effect=lambda ids: dict.fromkeys(ids, '1.00')):
                before_control = app.test_client().get('/recommend/other')
                before_treatment = app.test_client().get('/recommend/warm')
                self.assertNotIn('movie-1', before_treatment.get_data(as_text=True).split(','))
                self.assertIn('movie-0', before_treatment.get_data(as_text=True).split(','))
                worker = Thread(target=request_default)
                worker.start()
                try:
                    self.assertTrue(entered.wait(5))
                    request = activate(root, registry, 'C', candidate)
                    health = app.test_client().get('/health').json
                    self.assertEqual(health['activation_id'], request['activation_id'])
                    self.assertEqual(health['model_sha256'], candidate)
                finally:
                    release.set()
                    worker.join(5)
                self.assertEqual(statuses, [200])
                self.assertEqual(app.test_client().get('/recommend/outside').status_code, 200)
                after_control = app.test_client().get('/recommend/other')
                after_treatment = app.test_client().get('/recommend/warm')
                self.assertEqual(before_control.data, after_control.data)
                self.assertEqual(before_treatment.data, after_treatment.data)

            records = recommendation_serves.list_records()
            self.assertEqual(len(records), 6)
            for user, version in (('other', 'A'), ('warm', 'B')):
                arm = [row for row in records if row.user_id == user]
                self.assertEqual(len(arm), 2)
                self.assertEqual({row.model_version for row in arm}, {version})
                self.assertIsNotNone(arm[0].experiment_assignment_id)
                self.assertEqual(arm[0].experiment_assignment_id, arm[1].experiment_assignment_id)
            self.assertEqual({row.model_version for row in records if row.user_id == 'outside'}, {'A', 'C'})
            self.assertTrue(all(row.experiment_assignment_id is None for row in records if row.user_id == 'outside'))
            self.assertTrue(all(row.model_route.startswith('cost_aware:') for row in records if row.user_id == 'warm'))

            app = create_app(profile_store=self.store, config=settings | {'TREATMENT_MODEL_VERSION': 'missing'})
            fallback = app.test_client().get('/recommend/warm')
            self.assertEqual(fallback.status_code, 200)
            records = recommendation_serves.list_records()
            self.assertEqual(len(records), 7)
            fallback_record, = [row for row in records if row.user_id == 'warm' and row.experiment_assignment_id is None]
            self.assertEqual(fallback_record.model_version, 'A')
