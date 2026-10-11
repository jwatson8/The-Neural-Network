import argparse
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from surprise import Dataset, Reader, SVD, accuracy

from utils.dataProvider import DATA_PATH, PreparedModelData, loadPreparedData

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL_PATH = PROJECT_ROOT / "models" / "alvajoy_model.joblib"


def _rating_rows(events):
    return events.loc[
        events["event_type"] == "rating", ["user_id", "movie_id", "rating"]
    ].dropna(subset=["user_id", "movie_id", "rating"])


def legacy_exploratory_preprocessing(data: PreparedModelData):
    """Retain the submitted preprocessing that was not connected to either model."""
    train_events = data.train_events.dropna(subset=["rating"]).copy()
    train_events["timestamp"] = pd.to_datetime(train_events["timestamp"])
    train_events = pd.get_dummies(train_events, columns=["event_type"])
    users = data.users.copy()
    movies = data.movies.copy()

    users["self_description_likes"] = (
        users["self_description_likes"]
        .fillna("")
        .str.lower()
        .str.replace(r"[^\w\s]", "")
    )
    users["self_description_dislikes"] = (
        users["self_description_dislikes"]
        .fillna("")
        .str.lower()
        .str.replace(r"[^\w\s]", "")
    )
    users["self_description_likes_tokens"] = users[
        "self_description_likes"
    ].apply(lambda value: value.split())
    users["self_description_dislikes_tokens"] = users[
        "self_description_dislikes"
    ].apply(lambda value: value.split())
    users["gender"] = users["gender"].map({"M": 0, "F": 1, "O": 2, "U": 3})

    movies = pd.concat([movies, movies["genres"].str.get_dummies(sep="|")], axis=1)
    movies = movies.drop(columns=["genres"])
    merged_data = pd.merge(train_events, users, on="user_id")
    merged_data = pd.merge(merged_data, movies, on="movie_id")
    user_avg_rating = (
        train_events.groupby("user_id")["rating"].mean().reset_index()
    )
    merged_data = pd.merge(
        merged_data, user_avg_rating, on="user_id", suffixes=("", "_avg")
    )
    movie_genre_popularity = (
        train_events.groupby("movie_id")["rating"]
        .count()
        .reset_index(name="genre_popularity")
    )
    return pd.merge(merged_data, movie_genre_popularity, on="movie_id")


def train_model(data: PreparedModelData, model_path=DEFAULT_MODEL_PATH):
    """Train Alvajoy's submitted Surprise SVD on the shared rating partition."""
    train_ratings = _rating_rows(data.train_events)
    test_ratings = _rating_rows(data.test_events)

    # Preserve the submitted model choice and rating-scale decision. The only
    # change is that the common splitter now controls which ratings are here.
    reader = Reader(rating_scale=(1, 10))
    surprise_data = Dataset.load_from_df(
        train_ratings[["user_id", "movie_id", "rating"]], reader
    )
    trainset = surprise_data.build_full_trainset()
    algo = SVD()
    algo.fit(trainset)

    rmse = None
    mae = None
    if not test_ratings.empty:
        testset = list(
            test_ratings[["user_id", "movie_id", "rating"]].itertuples(
                index=False, name=None
            )
        )
        predictions = algo.test(testset)
        rmse = float(accuracy.rmse(predictions, verbose=False))
        mae = float(accuracy.mae(predictions, verbose=False))

    artifact = {
        "algorithm": algo,
        "rating_scale": (1, 10),
        "stats": {
            "training_ratings": len(train_ratings),
            "testing_ratings": len(test_ratings),
            "rmse": rmse,
            "mae": mae,
        },
    }
    model_path = Path(model_path)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, model_path)
    return artifact["stats"]


def load_model(model_path=DEFAULT_MODEL_PATH):
    artifact = joblib.load(Path(model_path))
    required = {"algorithm", "rating_scale", "stats"}
    if not required <= set(artifact):
        raise ValueError(
            f"Alvajoy artifact is missing keys: {sorted(required - set(artifact))}"
        )
    return artifact


def recommend(
    artifact,
    user_id,
    movie_ids,
    seen_movie_ids=(),
    k=20,
):
    """Score a candidate catalog with Surprise SVD and return the top items."""
    if k < 1:
        raise ValueError("k must be positive")

    candidates = [str(movie_id) for movie_id in movie_ids]
    algorithm = artifact["algorithm"]
    scores = np.fromiter(
        (algorithm.predict(user_id, movie_id).est for movie_id in candidates),
        dtype=float,
        count=len(candidates),
    )
    seen = {str(movie_id) for movie_id in seen_movie_ids}
    ranked = np.argsort(-scores, kind="stable")
    return [
        {"movie_id": candidates[index], "score": float(scores[index])}
        for index in ranked
        if candidates[index] not in seen and np.isfinite(scores[index])
    ][:k]


def legacy_content_demo(data: PreparedModelData, user_id=1, n=10):
    """Preserve Alvajoy's submitted content demo without treating it as SVD.

    This path is intentionally excluded from comparative training. In the
    submitted implementation it references a ``self_description`` column that
    is absent from the supplied users file and refits its vectorizer before
    comparing user and movie vectors. It remains available to make that
    limitation visible instead of silently redesigning the approach.
    """
    users = data.users.copy()
    movies = data.movies.copy()
    tfidf = TfidfVectorizer(stop_words="english")

    # Original submitted operations, including the known missing-column issue.
    user_tfidf_matrix = tfidf.fit_transform(users["self_description"])
    movie_tfidf_matrix = tfidf.fit_transform(movies["overview"])
    cosine_sim = cosine_similarity(user_tfidf_matrix, movie_tfidf_matrix)

    user_idx = users[users["user_id"] == user_id].index[0]
    sim_scores = list(enumerate(cosine_sim[user_idx]))
    sim_scores = sorted(sim_scores, key=lambda item: item[1], reverse=True)
    top_movie_indices = [item[0] for item in sim_scores[:n]]
    return movies.iloc[top_movie_indices][["movie_id", "title"]]


def main():
    parser = argparse.ArgumentParser(description="Train Alvajoy's SVD model")
    parser.add_argument("--data-dir", type=Path, default=DATA_PATH)
    parser.add_argument("--model-out", type=Path, default=DEFAULT_MODEL_PATH)
    parser.add_argument(
        "--legacy-content-demo",
        action="store_true",
        help="Run the unsupported submitted TF-IDF demo after SVD training",
    )
    args = parser.parse_args()
    data = loadPreparedData(args.data_dir)
    stats = train_model(data, args.model_out)
    load_model(args.model_out)
    print(stats)
    if args.legacy_content_demo:
        print(legacy_content_demo(data, user_id=1, n=10))


if __name__ == "__main__":
    main()
