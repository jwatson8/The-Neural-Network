"""Routing uses distinct movie history embedded in the deployed training bundle."""

MIN_SVD_MOVIES = 10


def uses_svd(model, user_id):
    # fit() stores a set of movie indices: watch minutes and repeat ratings do not
    # increase the count. Live events only count after retraining and promotion.
    return (user_id in model['ui']
            and len(set(model['seen'].get(user_id, ()))) >= MIN_SVD_MOVIES)
