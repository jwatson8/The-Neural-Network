"""Inference adapter for the Milestone 0 SVD/content model bundle."""

import numpy as np


def validate_model(model):
    required = {
        'movies', 'mi', 'ui', 'seen', 'user_factors', 'movie_factors',
        'vectorizer', 'content', 'popularity',
    }
    missing = required.difference(model)
    if missing:
        raise ValueError(f'Model bundle is missing fields: {sorted(missing)}')
    if len(model['movies']) != len(model['popularity']):
        raise ValueError('Movie catalog and popularity scores have different lengths')
    if model['movie_factors'].shape[1] != len(model['movies']):
        raise ValueError('Movie factors do not match the catalog')


def recommend(model, user_id, profile=None, k=20):
    if not 1 <= k <= 20:
        raise ValueError('k must be between 1 and 20')

    seen = model['seen'].get(user_id, set())
    if seen:
        user_index = model['ui'][user_id]
        scores = model['user_factors'][user_index] @ model['movie_factors']
        route = 'collaborative_svd'
    elif profile is not None:
        query = model['vectorizer'].transform([
            profile.get('positive', ''), profile.get('negative', ''),
        ])
        matches = (model['content'] @ query.T).toarray()
        scores = matches[:, 0] - 0.7 * matches[:, 1] + 0.02 * model['popularity']
        route = 'llm_content'
    else:
        scores = model['popularity'].copy()
        route = 'popularity_fallback'

    scores = np.asarray(scores).copy()
    if seen:
        scores[list(seen)] = -np.inf
    ranked = np.argsort(-scores, kind='stable')
    movie_ids = []
    for index in ranked:
        movie_id = str(model['movies'][int(index)]['movie_id'])
        if np.isfinite(scores[index]) and movie_id not in movie_ids:
            movie_ids.append(movie_id)
        if len(movie_ids) == k:
            break
    return movie_ids, route