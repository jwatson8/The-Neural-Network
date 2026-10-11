import argparse
from pathlib import Path

import joblib
import numpy as np
from sklearn.metrics.pairwise import cosine_similarity

try:
    from .preprocess import build_ratings, build_user_item_matrix
except ImportError:  # Allow direct execution as python Frank/src/train.py
    from preprocess import build_ratings, build_user_item_matrix

from utils.dataProvider import DATA_PATH, PreparedModelData, loadPreparedData

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL_PATH = PROJECT_ROOT / "models" / "frank_model.joblib"


def build_item_similarity(user_item_matrix):
    # user_item_matrix:
    # rows    = users
    # columns = movies
    #
    # cosine_similarity expects each row to be one object.
    # We want to compare movies, so transpose:
    #
    # movie × user
    item_user_matrix = user_item_matrix.T.fillna(0.0)

    similarity_matrix = cosine_similarity(item_user_matrix)

    return similarity_matrix


def save_model(similarity_matrix, movie_ids, model_path=DEFAULT_MODEL_PATH):
    model_path = Path(model_path)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {"similarity_matrix": similarity_matrix, "movie_ids": movie_ids}, model_path
    )


def load_model(model_path=DEFAULT_MODEL_PATH):
    model = joblib.load(Path(model_path))
    required = {"similarity_matrix", "movie_ids"}
    if not required <= set(model):
        raise ValueError(f"Frank artifact is missing keys: {sorted(required - set(model))}")
    if model["similarity_matrix"].shape[0] != len(model["movie_ids"]):
        raise ValueError("Frank artifact has misaligned similarities and movie IDs")
    return model


def train_model(data: PreparedModelData, model_path=DEFAULT_MODEL_PATH):
    ratings = build_ratings(data.train_events)
    user_item_matrix = build_user_item_matrix(ratings)
    similarity_matrix = build_item_similarity(user_item_matrix)
    movie_ids = user_item_matrix.columns.tolist()
    save_model(similarity_matrix, movie_ids, model_path)
    return {
        "training_ratings": len(ratings),
        "users": user_item_matrix.shape[0],
        "movies": user_item_matrix.shape[1],
        "similarity_min": float(np.min(similarity_matrix)),
        "similarity_max": float(np.max(similarity_matrix)),
    }


def main():
    parser = argparse.ArgumentParser(description="Train Frank's recommendation model")
    parser.add_argument("--data-dir", type=Path, default=DATA_PATH)
    parser.add_argument("--model-out", type=Path, default=DEFAULT_MODEL_PATH)
    args = parser.parse_args()
    stats = train_model(loadPreparedData(args.data_dir), args.model_out)
    load_model(args.model_out)
    print(stats)
    print(f"Saved model: {args.model_out}")


if __name__ == "__main__":
    main()
