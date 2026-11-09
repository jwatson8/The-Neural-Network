"""Inference adapter for existing users in the trained Jace model."""

import numpy as np
from src.model.routing import uses_svd


def validate_model(model):
    required = {
        'movies', 'ui', 'seen', 'user_factors', 'movie_factors', 'vectorizer',
        'popularity', 'content',
    }
    missing = required.difference(model)
    if missing:
        raise ValueError(f'Model bundle is missing fields: {sorted(missing)}')
    if len(model['movies']) != len(model['popularity']):
        raise ValueError('Movie catalog and popularity scores have different lengths')
    if model['movie_factors'].shape[1] != len(model['movies']):
        raise ValueError('Movie factors do not match the catalog')
    if hasattr(model['content'], 'shape') and model['content'].shape[0] != len(model['movies']):
        raise ValueError('Content features do not match the catalog')


def recommend(
    model, user_id, k=20, *, exclude_movie_ids=None, allow_svd=True
):
    if not 1 <= k <= 20:
        raise ValueError('k must be between 1 and 20')

    seen = model['seen'].get(user_id, set())
    if allow_svd and uses_svd(model, user_id):
        user_index = model['ui'][user_id]
        scores = model['user_factors'][user_index] @ model['movie_factors']
        route = 'collaborative_svd'
    else:
        scores = model['popularity'].copy()
        route = 'popularity_fallback'

    scores = np.asarray(scores).copy()
    if seen:
        scores[list(seen)] = -np.inf
    excluded = {str(movie_id) for movie_id in (exclude_movie_ids or ())}
    for index, movie in enumerate(model['movies']):
        if str(movie['movie_id']) in excluded:
            scores[index] = -np.inf
    ranked = np.argsort(-scores, kind='stable')
    movie_ids = []
    for index in ranked:
        movie_id = str(model['movies'][int(index)]['movie_id'])
        if np.isfinite(scores[index]) and movie_id not in movie_ids:
            movie_ids.append(movie_id)
        if len(movie_ids) == k:
            break
    return movie_ids, route
