"""Flask recommendation API; model inference stays out of cold-start I/O."""

import logging
import hashlib
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import joblib
from flask import Flask, Response, jsonify, request
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.exceptions import HTTPException

from src.database.interactions import (
    cold_start_jobs,
    database_summary,
    recommendation_serves,
    users,
)
from src.data_quality.boundary import (
    validate_recommendation_request,
    validate_recommendation_response,
)
from src.data_quality.quality_logging import log_validation_failure
from src.service.cold_start import recommend_cold_start
from src.service.model_adapter import recommend, validate_model
from src.service.store import ProfileStore

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'))


def create_app(model=None, profile_store=None, config=None):
    app = Flask(__name__)
    app.config.update(
        MODEL_PATH=os.getenv(
            'MODEL_PATH',
            str(Path(__file__).resolve().parents[2] / 'artifacts' / 'model.joblib'),
        ),
        MODEL_VERSION=os.getenv('MODEL_VERSION', 'unknown'),
        PROFILE_SEED_PATH=os.getenv('PROFILE_SEED_PATH', str(Path(__file__).resolve().parents[1] / 'model' / 'llm_profiles.json')),
    )
    if config:
        app.config.update(config)

    model_sha256 = None
    if model is None:
        model_path = Path(app.config['MODEL_PATH'])
        if model_path.is_file():
            with model_path.open('rb') as stream:
                model_sha256 = hashlib.file_digest(stream, 'sha256').hexdigest()
                stream.seek(0)
                model = joblib.load(stream)
    if model is not None:
        validate_model(model)
        app.config['MODEL_VERSION'] = model.get('update_metadata', {}).get('version', app.config['MODEL_VERSION'])

    store = profile_store or ProfileStore()
    if profile_store is None:
        try:
            store.seed_profiles(app.config['PROFILE_SEED_PATH'])
        except Exception as error:
            logger.warning('profile_seed_failed error_type=%s', type(error).__name__)
    app.extensions['recommendation_model'] = model
    app.extensions['profile_store'] = store

    @app.errorhandler(HTTPException)
    def handle_http_error(error):
        return jsonify(error=error.name, message=error.description), error.code

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
        ready = app.extensions['recommendation_model'] is not None
        try:
            store.healthy()
        except Exception:
            ready = False
        return jsonify(status='ok' if ready else 'not_ready', model_version=app.config['MODEL_VERSION'],
                       model_sha256=model_sha256), 200 if ready else 503

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
            return jsonify(error='Bad Request', message='Invalid recommendation request'), 400
        user_id = validated_request['user_id']

        request_received_at = datetime.now(UTC)
        started = time.perf_counter()
        loaded_model = app.extensions['recommendation_model']
        if loaded_model is None:
            return jsonify(error='Service Unavailable', message='Model unavailable'), 503

        if loaded_model['seen'].get(user_id):
            movie_ids, route = recommend(loaded_model, user_id, k=20)
        else:
            try:
                result = recommend_cold_start(loaded_model, store, user_id, k=20)
            except (SQLAlchemyError, RuntimeError) as error:
                result = None
                logger.warning('cold_start_store_read_failed user_id=%s error_type=%s', user_id, type(error).__name__)
            if result is None:
                movie_ids, route = recommend(loaded_model, user_id, k=20)
                try:
                    store.enqueue(user_id)
                except (SQLAlchemyError, RuntimeError) as error:
                    logger.warning('profile_job_enqueue_failed user_id=%s error_type=%s', user_id, type(error).__name__)
            else:
                movie_ids, route = result

        response_body = ','.join(movie_ids)
        response_items, errors = validate_recommendation_response(response_body)
        if errors:
            log_validation_failure('recommendation_api', errors)
            return jsonify(error='Internal Server Error', message='Recommendation generation failed'), 500
        movie_ids = [item.movie_id for item in response_items]

        elapsed_ms = (time.perf_counter() - started) * 1000
        users.create_missing_many([{'user_id': user_id}])
        recommendation_serves.create(
            user_id=user_id,
            served_at=datetime.now(UTC),
            model_route=route,
            model_version=app.config['MODEL_VERSION'],
            recommendations=[{'movie_id': movie_id} for movie_id in movie_ids],
            request_received_at=request_received_at,
            serve_duration_ms=elapsed_ms,
        )
        logger.info(
            'recommendation user_id=%s route=%s model_version=%s duration_ms=%.2f status=200',
            user_id, route, app.config['MODEL_VERSION'], elapsed_ms,
        )
        return Response(response_body, status=200, mimetype='text/plain')

    return app


app = create_app()
