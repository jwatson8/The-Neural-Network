"""Database-backed recommendations for users without interaction history."""

import numpy as np


def recommend_cold_start(model, store, user_id, k=20):
    if not 1 <= k <= 20:
        raise ValueError('k must be between 1 and 20')

    data = store.get_cold_start_data(user_id)
    if data is None:
        return None

    user = data['user']
    profile = data['profile'] or {}
    positive = profile.get('positive') or user.get('self_description_likes') or ''
    negative = profile.get('negative') or user.get('self_description_dislikes') or ''
    movies = data['movies']
    if not movies:
        return [], 'cold_start_popularity'

    popularity = np.asarray([
        float(movie.get('popularity') or 0) for movie in movies
    ], dtype=float)
    popularity = np.nan_to_num(popularity, nan=0.0, posinf=0.0, neginf=0.0)
    max_popularity = float(popularity.max())
    if max_popularity > 0:
        popularity /= max_popularity

    if positive.strip() or negative.strip():
        documents = [
            ' '.join(filter(None, (
                movie.get('title') or '',
                (movie.get('genres') or '').replace('|', ' '),
                movie.get('overview') or '',
            )))
            for movie in movies
        ]
        movie_features = model['vectorizer'].transform(documents)
        preference_features = model['vectorizer'].transform([positive, negative])
        matches = movie_features @ preference_features.T
        if hasattr(matches, 'toarray'):
            matches = matches.toarray()
        else:
            matches = np.asarray(matches)
        scores = matches[:, 0] - 0.7 * matches[:, 1] + 0.02 * popularity
        route = 'cold_start_content'
    else:
        scores = popularity
        route = 'cold_start_popularity'

    ranked = np.argsort(-scores, kind='stable')[:k]
    return [str(movies[int(index)]['movie_id']) for index in ranked], route