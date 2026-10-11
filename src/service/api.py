"""Flask recommendation API; model inference stays out of cold-start I/O."""

import logging
import os
import sqlite3
import time
from pathlib import Path

import joblib
from flask import Flask, Response, jsonify

from src.service.cold_start import recommend_cold_start
from src.service.model_adapter import recommend, validate_model
from src.service.store import ProfileStore

logger = logging.getLogger(__name__)


def create_app(model=None, profile_store=None, config=None):
    app = Flask(__name__)
    app.config.update(
        MODEL_PATH=os.getenv(
            'MODEL_PATH',
            str(Path(__file__).resolve().parents[1] / 'app' / 'fullTrainedModel' / 'jace_model.joblib'),
        ),
        MODEL_VERSION=os.getenv('MODEL_VERSION', 'unknown'),
        PROFILE_DB_PATH=os.getenv('PROFILE_DB_PATH', 'state/profiles.sqlite3'),
        PROFILE_SEED_PATH=os.getenv('PROFILE_SEED_PATH', 'llm_profiles.json'),
    )
    if config:
        app.config.update(config)

    if model is None:
        model_path = Path(app.config['MODEL_PATH'])
        if model_path.is_file():
            model = joblib.load(model_path)
    if model is not None:
        validate_model(model)

    store = profile_store or ProfileStore(app.config['PROFILE_DB_PATH'])
    if profile_store is None:
        store.seed_profiles(app.config['PROFILE_SEED_PATH'])
    app.extensions['recommendation_model'] = model
    app.extensions['profile_store'] = store

    @app.get('/health')
    def health():
        ready = app.extensions['recommendation_model'] is not None
        try:
            store.healthy()
        except Exception:
            ready = False
        return jsonify(status='ok' if ready else 'not_ready'), 200 if ready else 503

    @app.get('/recommend/<string:user_id>')
    def recommendation(user_id):
        started = time.perf_counter()
        loaded_model = app.extensions['recommendation_model']
        if loaded_model is None:
            return Response('Model unavailable', status=503, mimetype='text/plain')

        if loaded_model['seen'].get(user_id):
            movie_ids, route = recommend(loaded_model, user_id, k=20)
        else:
            try:
                result = recommend_cold_start(loaded_model, store, user_id, k=20)
            except sqlite3.Error:
                result = None
                logger.warning('cold_start_store_read_failed user_id=%s', user_id)
            if result is None:
                movie_ids, route = recommend(loaded_model, user_id, k=20)
                try:
                    store.enqueue(user_id)
                except sqlite3.Error:
                    logger.warning('profile_job_enqueue_failed user_id=%s', user_id)
            else:
                movie_ids, route = result

        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info(
            'recommendation user_id=%s route=%s model_version=%s duration_ms=%.2f status=200',
            user_id, route, app.config['MODEL_VERSION'], elapsed_ms,
        )
        return Response(','.join(movie_ids), status=200, mimetype='text/plain')

    return app


app = create_app()