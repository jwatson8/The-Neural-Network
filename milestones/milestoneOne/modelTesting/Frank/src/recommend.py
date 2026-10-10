import argparse
import json
from pathlib import Path

import numpy as np

try:
    from .preprocess import build_ratings, build_watch_history
    from .train import DEFAULT_MODEL_PATH, load_model
except ImportError:  # Allow direct execution as python Frank/src/recommend.py
    from preprocess import build_ratings, build_watch_history
    from train import DEFAULT_MODEL_PATH, load_model

from utils.dataProvider import DATA_PATH, loadPreparedData


def build_user_ratings(events):
    """Return training ratings keyed by user and then movie."""
    ratings = build_ratings(events)
    return {
        user_id: dict(zip(group["movie_id"].astype(str), group["rating"].astype(float)))
        for user_id, group in ratings.groupby("user_id")
    }


def predict_rating(model, user_ratings, movie_id):
    """Convert Frank's centered similarity signal into a 1–10 rating estimate."""
    if not user_ratings:
        raise ValueError("Cannot predict a rating without user rating history")

    movie_ids = [str(value) for value in model["movie_ids"]]
    movie_index = {value: index for index, value in enumerate(movie_ids)}
    target_id = str(movie_id)
    if target_id not in movie_index:
        raise ValueError(f"Movie {target_id} is missing from Frank's model")

    known_ratings = [
        (movie_index[str(rated_id)], float(rating))
        for rated_id, rating in user_ratings.items()
        if str(rated_id) in movie_index
    ]
    if not known_ratings:
        raise ValueError("No user ratings are represented in Frank's model")

    rated_indices = np.asarray([index for index, _ in known_ratings], dtype=int)
    centered_ratings = np.asarray(
        [rating - 5.5 for _, rating in known_ratings], dtype=float
    )
    similarities = model["similarity_matrix"][
        movie_index[target_id], rated_indices
    ]
    total_similarity = float(np.abs(similarities).sum())
    if total_similarity == 0:
        return 5.5

    estimate = 5.5 + float(similarities @ centered_ratings) / total_similarity
    return float(np.clip(estimate, 1.0, 10.0))


def recommend_existing_user(
    model,
    user_id,
    user_ratings,
    seen_movie_ids,
    movies_by_id,
    top_k=20,
    candidate_movie_ids=None,
):
    """Apply Frank's centered-rating item-similarity recommendation method."""
    if top_k < 1:
        raise ValueError("top_k must be positive")
    if not user_ratings:
        raise ValueError(f"User {user_id} has no rating history.")

    movie_ids = [str(movie_id) for movie_id in model["movie_ids"]]
    movie_index = {movie_id: index for index, movie_id in enumerate(movie_ids)}
    known_ratings = [
        (movie_index[str(movie_id)], float(rating))
        for movie_id, rating in user_ratings.items()
        if str(movie_id) in movie_index
    ]
    if not known_ratings:
        raise ValueError(f"User {user_id} has no ratings represented in Frank's model.")

    rated_indices = np.asarray([index for index, _ in known_ratings], dtype=int)
    centered_ratings = np.asarray(
        [rating - 5.5 for _, rating in known_ratings], dtype=float
    )
    scores = model["similarity_matrix"][:, rated_indices] @ centered_ratings

    allowed = (
        set(movie_ids)
        if candidate_movie_ids is None
        else {str(movie_id) for movie_id in candidate_movie_ids}
    )
    missing = allowed - set(movie_ids)
    if missing:
        raise ValueError(
            f"Candidate movies are missing from Frank's model: {sorted(missing)[:5]}"
        )

    seen = {str(movie_id) for movie_id in seen_movie_ids}
    ranked = np.argsort(-scores, kind="stable")
    recommendations = []
    for index in ranked:
        movie_id = movie_ids[index]
        if movie_id in seen or movie_id not in allowed or not np.isfinite(scores[index]):
            continue
        movie = movies_by_id.get(movie_id, {})
        recommendations.append(
            {
                "movie_id": movie_id,
                "title": movie.get("title", ""),
                "genres": movie.get("genres", ""),
                "score": float(scores[index]),
            }
        )
        if len(recommendations) == top_k:
            break
    return recommendations


def main():
    parser = argparse.ArgumentParser(
        description="Generate movie recommendations for an existing user."
    )
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--data-dir", type=Path, default=DATA_PATH)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL_PATH)
    args = parser.parse_args()

    data = loadPreparedData(args.data_dir)
    ratings_by_user = build_user_ratings(data.train_events)
    watched_by_user = build_watch_history(data.train_events)
    movies_by_id = {
        str(movie["movie_id"]): movie for movie in data.movies.to_dict("records")
    }
    recommendations = recommend_existing_user(
        model=load_model(args.model),
        user_id=args.user_id,
        user_ratings=ratings_by_user.get(args.user_id, {}),
        seen_movie_ids=watched_by_user.get(args.user_id, set()),
        movies_by_id=movies_by_id,
        top_k=args.top_k,
    )
    print(json.dumps(recommendations, indent=2))


if __name__ == "__main__":
    main()
