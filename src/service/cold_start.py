"""Rank the trained catalog using saved LLM preferences, without external I/O."""
import numpy as np
from src.service.model_adapter import recommend


def recommend_cold_start(
    model, store, user_id, k=20, *, exclude_movie_ids=None, allow_svd=True
):
    if not 1 <= k <= 20:
        raise ValueError('k must be between 1 and 20')
    profile = store.get_profile(user_id)
    if profile is None:
        return None
    positive, negative = profile.get('positive', ''), profile.get('negative', '')
    if not positive.strip() and not negative.strip():
        return recommend(
            model, user_id, k=k,
            exclude_movie_ids=exclude_movie_ids,
            allow_svd=allow_svd,
        )
    query = model['vectorizer'].transform([positive, negative])
    matches = (model['content'] @ query.T).toarray()
    scores = matches[:, 0] - 0.7 * matches[:, 1] + 0.02 * model['popularity']
    # Low-history users can now have watched movies; exclude them on this path too.
    seen = model['seen'].get(user_id, set())
    if seen:
        scores[list(seen)] = -np.inf
    ranked = np.argsort(-scores, kind='stable')
    ids = []
    excluded = {str(movie_id) for movie_id in (exclude_movie_ids or ())}
    for index in ranked:
        movie_id = str(model['movies'][int(index)]['movie_id'])
        if (
            np.isfinite(scores[index])
            and movie_id not in excluded
            and movie_id not in ids
        ):
            ids.append(movie_id)
        if len(ids) == k:
            break
    return ids, 'llm_content'
