"""Flask recommendation API; model inference stays out of cold-start I/O."""

import logging
import os
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from flask import Flask, Response, jsonify, request
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.exceptions import HTTPException

from src.database.interactions import (
    cold_start_jobs,
    database_summary,
    recommendation_serves,
    user_movie_history,
    users,
)
from src.data_quality.boundary import (
    validate_recommendation_request,
    validate_recommendation_response,
)
from src.data_quality.quality_logging import log_validation_failure
from src.service.cold_start import recommend_cold_start
from src.service.cost_aware_treatment import CostAwareTreatmentServer
from src.service.model_adapter import recommend
from src.service.model_registry import ModelRegistry
from src.service.store import ProfileStore
from src.experimentation.manager import ExperimentManager
from src.service.experiment_routing import RecommendationRouter, ServingRequest, ServingResult, TreatmentServer
from src.model.routing import uses_svd

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'))


def create_app(
    model=None, profile_store=None, config=None, *,
    experiment_manager: ExperimentManager | None = None,
    treatment_server: TreatmentServer | None = None,
    experiment_eligibility: Callable[[ServingRequest], bool] | None = None,
) -> Flask:
    app = Flask(__name__)
    app.config.update(
        EXPERIMENTS_ENABLED=os.getenv("EXPERIMENTS_ENABLED") == "true",
        EXPERIMENT_ID=os.getenv("EXPERIMENT_ID"),
        CONTROL_SERVING_POLICY="legacy",
        COST_AWARE_TREATMENT_ENABLED=os.getenv("COST_AWARE_TREATMENT_ENABLED") == "true",
        COST_AWARE_WEIGHT="0.15",
        COST_AWARE_CANDIDATE_MULTIPLIER=5,
        COST_AWARE_MAX_CANDIDATES=200,
        MODEL_PATH=os.getenv(
            'MODEL_PATH',
            str(Path(__file__).resolve().parents[2] / 'artifacts' / 'model.joblib'),
        ),
        MODEL_VERSION=os.getenv('MODEL_VERSION', 'unknown'),
        MODEL_ACTIVATION_ENABLED=os.getenv('MODEL_ACTIVATION_ENABLED') == 'true',
        CONTROL_MODEL_VERSION=os.getenv('CONTROL_MODEL_VERSION'),
        TREATMENT_MODEL_VERSION=os.getenv('TREATMENT_MODEL_VERSION'),
        PROFILE_SEED_PATH=os.getenv('PROFILE_SEED_PATH', str(Path(__file__).resolve().parents[1] / 'model' / 'llm_profiles.json')),
    )
    if config:
        app.config.update(config)

    enabled = app.config['EXPERIMENTS_ENABLED']
    if not isinstance(enabled, bool):
        raise ValueError('EXPERIMENTS_ENABLED must be a boolean')
    experiment_id = None
    if enabled:
        try:
            experiment_id = UUID(str(app.config['EXPERIMENT_ID']))
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError('Enabled experimentation requires one explicit EXPERIMENT_ID UUID') from error
    control_policy = app.config['CONTROL_SERVING_POLICY']
    if not isinstance(control_policy, str) or not control_policy.strip() or len(control_policy) > 255:
        raise ValueError('CONTROL_SERVING_POLICY must be a nonblank string of at most 255 characters')
    manager = experiment_manager if experiment_manager is not None else ExperimentManager(enabled=enabled)
    eligibility = experiment_eligibility if experiment_eligibility is not None else lambda context: True

    managed = app.config['MODEL_ACTIVATION_ENABLED']
    pins = {}
    if enabled:
        pins = {'control': app.config['CONTROL_MODEL_VERSION'],
                'treatment': app.config['TREATMENT_MODEL_VERSION']}
        if managed and not all(pins.values()):
            raise ValueError('Managed experiments require explicit Control and Treatment model pins')
    registry = ModelRegistry(app.config['MODEL_PATH'], app.config['MODEL_VERSION'], model=model, pins=pins)
    app.extensions['model_registry'] = registry
    # Preserve the injectable bundle interface used by offline callers.
    if model is not None:
        app.extensions['recommendation_model'] = model

    store = profile_store or ProfileStore()
    if profile_store is None:
        try:
            store.seed_profiles(app.config['PROFILE_SEED_PATH'])
        except Exception as error:
            logger.warning('profile_seed_failed error_type=%s', type(error).__name__)
    cost_aware_enabled = app.config['COST_AWARE_TREATMENT_ENABLED']
    if not isinstance(cost_aware_enabled, bool):
        raise ValueError('COST_AWARE_TREATMENT_ENABLED must be a boolean')
    cost_config = None
    if enabled and cost_aware_enabled:
        if treatment_server is not None:
            raise ValueError('Choose an injected Treatment or the built-in cost-aware adapter, not both')
        from src.experimentation.cost_reranking import CostAwareConfig

        cost_config = CostAwareConfig(app.config['COST_AWARE_WEIGHT'],
                                      app.config['COST_AWARE_CANDIDATE_MULTIPLIER'],
                                      app.config['COST_AWARE_MAX_CANDIDATES'])
    router = RecommendationRouter(treatment=treatment_server)
    if managed:
        registry.watch()
    app.extensions['profile_store'] = store
    app.extensions['experiment_manager'] = manager
    app.extensions['recommendation_router'] = router

    @app.errorhandler(HTTPException)
    def handle_http_error(error):
        response = error.get_response()
        response.data = jsonify(error=error.name, message=error.description).get_data()
        response.content_type = 'application/json'
        return response

    @app.errorhandler(SQLAlchemyError)
    def handle_database_error(error):
        logger.exception('database_request_failed')
        return jsonify(error='Service Unavailable', message='The database is temporarily unavailable'), 503

    @app.errorhandler(Exception)
    def handle_unexpected_error(error):
        logger.exception('unhandled_api_error')
        return jsonify(error='Internal Server Error', message='An unexpected error occurred'), 500

    @app.get('/health')
    def health():
        snapshot = registry.snapshot()
        default = snapshot.default
        registry_ready = default is not None and app.extensions.get('recommendation_model', True) is not None
        ready = registry_ready
        try:
            store.healthy()
        except Exception:
            ready = False
        return jsonify(
            status='ok' if ready else 'not_ready',
            model_version=default.version if default else app.config['MODEL_VERSION'],
            model_sha256=default.sha256 if default else None,
            registry_ready=registry_ready,
            experiment_models_ready=all(pin is not None for pin in snapshot.pins.values()),
            activation_enabled=managed,
            activation_id=snapshot.activation_id,
            activation_status=snapshot.activation_status,
            activation_error=snapshot.activation_error,
        ), 200 if ready else 503

    @app.get('/database/summary')
    def public_database_summary():
        return jsonify(tables=database_summary.counts())

    @app.get('/database/cold-start-jobs')
    def public_cold_start_jobs():
        try:
            offset = int(request.args.get('offset', '0'))
            limit = int(request.args.get('limit', '50'))
        except ValueError:
            return jsonify(error='Bad Request', message='offset and limit must be integers'), 400
        status = request.args.get('status') or None
        if offset < 0 or not 1 <= limit <= 100:
            return jsonify(error='Bad Request', message='offset must be non-negative and limit must be between 1 and 100'), 400
        if status not in (None, 'pending', 'processing'):
            return jsonify(error='Bad Request', message='status must be pending or processing'), 400

        records = cold_start_jobs.list_records(
            status=status, offset=offset, limit=limit + 1
        )
        has_more = len(records) > limit
        jobs = records[:limit]
        return jsonify(
            jobs=[
                {
                    'status': job.status,
                    'attempts': job.attempts,
                    'next_attempt_at': job.next_attempt_at.isoformat(),
                    'lease_until': job.lease_until.isoformat() if job.lease_until else None,
                    'created_at': job.created_at.isoformat(),
                    'updated_at': job.updated_at.isoformat(),
                }
                for job in jobs
            ],
            offset=offset,
            limit=limit,
            has_more=has_more,
        )

    @app.get('/recommend/<string:user_id>')
    def recommendation(user_id):
        validated_request, errors = validate_recommendation_request(
            {'user_id': user_id}, request.args
        )
        if errors:
            log_validation_failure('recommendation_api', errors)
            user_id_error = next(
                (error for error in errors if error.get('loc') == ('user_id',)),
                None,
            )
            message = user_id_error['msg'] if user_id_error else 'Invalid recommendation request'
            return jsonify(error='Bad Request', message=message), 400
        user_id = validated_request['user_id']

        request_received_at = datetime.now(UTC)
        started = time.perf_counter()
        snapshot = registry.snapshot()
        default = snapshot.default
        if default is None or app.extensions.get('recommendation_model', True) is None:
            return jsonify(error='Service Unavailable', message='Model unavailable'), 503

        users.create_missing_many([{'user_id': user_id}])

        history_rows = user_movie_history.get_user_history(user_id)
        excluded_movie_ids = {str(row.movie_id) for row in history_rows}
        context = ServingRequest(user_id=user_id, exclude_movie_ids=frozenset(excluded_movie_ids),
                                 has_history=bool(history_rows))
        decision = None
        experiment_reason = 'disabled'
        if enabled and app.config['EXPERIMENTS_ENABLED'] is True:
            try:
                eligible = eligibility(context)
                if not isinstance(eligible, bool):
                    raise ValueError('experiment eligibility must return a boolean')
                decision = manager.decide(experiment_id=experiment_id, user_id=user_id,
                                          eligible=eligible, now=request_received_at)
                experiment_reason = decision.reason.value
            except Exception as error:
                decision = None
                experiment_reason = 'experiment_error'
                logger.warning('experiment_decision_failed error_type=%s', type(error).__name__)

        def serve_control(handle):
            loaded_model = handle.bundle
            if history_rows and uses_svd(loaded_model, user_id):
                movie_ids, route = recommend(
                    loaded_model, user_id, k=20,
                    exclude_movie_ids=excluded_movie_ids,
                )
            else:
                try:
                    result = recommend_cold_start(
                        loaded_model, store, user_id, k=20,
                        exclude_movie_ids=excluded_movie_ids,
                        allow_svd=False,
                    )
                except (SQLAlchemyError, RuntimeError) as error:
                    result = None
                    logger.warning('cold_start_store_read_failed user_id=%s error_type=%s', user_id, type(error).__name__)
                if result is None:
                    movie_ids, route = recommend(
                        loaded_model, user_id, k=20,
                        exclude_movie_ids=excluded_movie_ids,
                        allow_svd=False,
                    )
                    try:
                        store.enqueue(user_id)
                    except (SQLAlchemyError, RuntimeError) as error:
                        logger.warning('profile_job_enqueue_failed user_id=%s error_type=%s', user_id, type(error).__name__)
                else:
                    movie_ids, route = result

            # The policy is the whole legacy pipeline, including its cold/popular paths.
            return ServingResult(tuple(movie_ids), handle.version, route,
                                 control_policy,
                                 fallback_path='popularity_fallback' if route == 'popularity_fallback' else None,
                                 fulfilled=True, model_sha256=handle.sha256)

        control = snapshot.pins.get('control', default)
        treatment = snapshot.pins.get('treatment', default)
        control_available = control is not None
        if 'control' in snapshot.pins and control_available and decision is not None:
            control_available = control.version == decision.version and control_policy == decision.policy
        request_router = router
        if decision is not None and decision.variant == 'treatment' and treatment is not None:
            bundle = treatment.bundle
            trained_history = frozenset(
                str(bundle['movies'][index]['movie_id']) for index in bundle['seen'].get(user_id, ())
            )
            context = replace(context, exclude_movie_ids=context.exclude_movie_ids | trained_history)
            if cost_config is not None:
                request_router = RecommendationRouter(CostAwareTreatmentServer.for_model(treatment, store, cost_config))
            elif treatment_server is not None and 'treatment' in snapshot.pins:
                def serve_pinned_treatment(request, assignment):
                    result = treatment_server(request, assignment)
                    if (result.model_version != treatment.version
                            or result.model_sha256 != treatment.sha256):
                        raise ValueError('Treatment did not serve the pinned artifact')
                    return result

                request_router = RecommendationRouter(serve_pinned_treatment)
        served = request_router.serve(
            context, decision, lambda: serve_control(default),
            assigned_control=(lambda: serve_control(control)) if 'control' in snapshot.pins else None,
            control_available=control_available,
            treatment_available=treatment is not None,
        )
        movie_ids, route = served.recommendations, served.model_route
        response_body = ','.join(movie_ids)
        response_items, errors = validate_recommendation_response(response_body)
        if errors:
            log_validation_failure('recommendation_api', errors)
            return jsonify(error='Internal Server Error', message='Recommendation generation failed'), 500
        movie_ids = [item.movie_id for item in response_items]

        response = Response(response_body, status=200, mimetype='text/plain')
        request_id = uuid4()
        exposure = {}
        if decision is not None and decision.assigned and decision.assignment_id is not None and served.fulfilled:
            exposure['experiment_assignment_id'] = decision.assignment_id
        recommendation_serves.create(
            request_id=request_id,
            **exposure,
            user_id=user_id,
            served_at=datetime.now(UTC),
            model_route=route,
            model_version=served.model_version,
            recommendations=[{'movie_id': movie_id} for movie_id in movie_ids],
            request_received_at=request_received_at,
            serve_duration_ms=0,
            request_started_at=started,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info(
            'recommendation user_id=%s route=%s model_version=%s duration_ms=%.2f status=200 request_id=%s model_sha256=%s',
            user_id, route, served.model_version, elapsed_ms, request_id, served.model_sha256,
        )
        if enabled:
            logger.info(
                'experiment_serve request_id=%s assignment_id=%s intended_assignment_id=%s intended_variant=%s '
                'actual_policy=%s policy_version=%s underlying_route=%s fallback_path=%s fulfilled=%s decision=%s',
                request_id, exposure.get('experiment_assignment_id'),
                decision.assignment_id if decision is not None else None,
                decision.variant if decision is not None else None,
                served.policy, served.policy_version, served.underlying_route,
                served.fallback_path, bool(exposure), experiment_reason,
            )
        return response

    return app


app = create_app()
