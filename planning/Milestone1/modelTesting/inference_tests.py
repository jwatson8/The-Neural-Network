import argparse
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import tensorflow as tf

from Alvajoy.train import load_model as load_alvajoy
from Alvajoy.train import recommend as recommend_alvajoy
from Frank.src.recommend import build_user_ratings
from Frank.src.recommend import predict_rating as predict_frank_rating
from Frank.src.recommend import recommend_existing_user
from Frank.src.train import load_model as load_frank
from Jace.recommender import load_model as load_jace
from Jace.recommender import predict_rating as predict_jace_rating
from Jace.recommender import recommend as recommend_jace
from Nathan.trainModelv1 import load_model as load_nathan
from Nathan.trainModelv1 import recommend as recommend_nathan
from throughput import _seen_by_user, _training_catalog, _validate_catalogs
from throughput import _validate_recommendations
from utils.dataProvider import DATA_PATH, loadPreparedData

ROOT = Path(__file__).resolve().parent
DEFAULT_MODEL_DIR = ROOT / "models"
DEFAULT_RESULTS_FILE = ROOT / "results" / "inference_tests.md"
DEFAULT_THRESHOLD = 7.5
DEFAULT_TOP_K = 20


def _rating_partitions(data, training_catalog):
    training = data.train_events.loc[
        data.train_events["event_type"] == "rating",
        ["user_id", "movie_id", "rating"],
    ].dropna()
    testing = data.test_events.loc[
        data.test_events["event_type"] == "rating",
        ["user_id", "movie_id", "rating"],
    ].dropna()
    training = training.copy()
    testing = testing.copy()
    training["movie_id"] = training["movie_id"].astype(str)
    testing["movie_id"] = testing["movie_id"].astype(str)

    warm_mask = testing["movie_id"].isin(set(training_catalog))
    warm = testing.loc[warm_mask].reset_index(drop=True)
    cold = testing.loc[~warm_mask].reset_index(drop=True)

    training_pairs = set(
        training[["user_id", "movie_id"]].itertuples(index=False, name=None)
    )
    warm_pairs = set(warm[["user_id", "movie_id"]].itertuples(index=False, name=None))
    overlap = training_pairs & warm_pairs
    if overlap:
        raise ValueError("Warm evaluation contains user-movie pairs seen in training")
    return training, warm, cold


def _point_metrics(actual, predicted, threshold):
    actual = np.asarray(actual, dtype=float)
    predicted = np.asarray(predicted, dtype=float)
    if actual.shape != predicted.shape or actual.size == 0:
        raise ValueError("Actual and predicted ratings must be non-empty and aligned")

    actual_positive = actual >= threshold
    predicted_positive = predicted >= threshold
    tp = int(np.sum(actual_positive & predicted_positive))
    fp = int(np.sum(~actual_positive & predicted_positive))
    tn = int(np.sum(~actual_positive & ~predicted_positive))
    fn = int(np.sum(actual_positive & ~predicted_positive))

    def ratio(numerator, denominator):
        return float(numerator / denominator) if denominator else 0.0

    errors = predicted - actual
    return {
        "ratings": len(actual),
        "mae": float(np.mean(np.abs(errors))),
        "rmse": float(np.sqrt(np.mean(np.square(errors)))),
        "precision": ratio(tp, tp + fp),
        "recall": ratio(tp, tp + fn),
        "false_positive_rate": ratio(fp, fp + tn),
        "false_negative_rate": ratio(fn, fn + tp),
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
    }


def _ranking_summary(user_results, top_k):
    if not user_results:
        raise ValueError("Ranking evaluation requires at least one user")
    precisions = [row["hits"] / top_k for row in user_results]
    recalls = [row["hits"] / row["relevant"] for row in user_results]
    return {
        "users": len(user_results),
        "top_k": top_k,
        "precision_at_k": float(np.mean(precisions)),
        "recall_at_k": float(np.mean(recalls)),
        "hit_rate_at_k": float(np.mean([row["hits"] > 0 for row in user_results])),
        "ndcg_at_k": float(np.mean([row["ndcg"] for row in user_results])),
        "total_hits": sum(row["hits"] for row in user_results),
        "total_relevant": sum(row["relevant"] for row in user_results),
    }


def _binary_ndcg_at_k(recommended_movie_ids, relevant_movie_ids, top_k):
    relevant = {str(movie_id) for movie_id in relevant_movie_ids}
    if not relevant:
        return 0.0

    ranked = [str(movie_id) for movie_id in recommended_movie_ids[:top_k]]
    dcg = sum(
        1.0 / np.log2(rank + 1)
        for rank, movie_id in enumerate(ranked, start=1)
        if movie_id in relevant
    )
    ideal_hits = min(len(relevant), top_k)
    ideal_dcg = sum(
        1.0 / np.log2(rank + 1) for rank in range(1, ideal_hits + 1)
    )
    return float(dcg / ideal_dcg)


def _baseline_recommendations(
    movie_averages, candidates, seen_movie_ids, top_k
):
    scores = np.asarray([movie_averages[movie_id] for movie_id in candidates])
    seen = {str(movie_id) for movie_id in seen_movie_ids}
    ranked = np.argsort(-scores, kind="stable")
    return [
        {"movie_id": candidates[index], "score": float(scores[index])}
        for index in ranked
        if candidates[index] not in seen
    ][:top_k]


def _load_models(model_dir):
    paths = {
        "Nathan": model_dir / "nathan_model.keras",
        "Alvajoy": model_dir / "alvajoy_model.joblib",
        "Frank": model_dir / "frank_model.joblib",
        "Jace": model_dir / "jace_model.joblib",
    }
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            f"Missing model artifacts: {missing}. Run python train_all.py first."
        )
    return {
        "Nathan": load_nathan(paths["Nathan"]),
        "Alvajoy": load_alvajoy(paths["Alvajoy"]),
        "Frank": load_frank(paths["Frank"]),
        "Jace": load_jace(paths["Jace"]),
    }


def _point_predictions(models, warm, ratings_by_user, movie_averages):
    print("Calculating pointwise rating predictions...", flush=True)
    nathan_values = models["Nathan"](
        {
            "user_id": tf.constant(warm["user_id"].astype(str).tolist()),
            "movie_id": tf.constant(warm["movie_id"].tolist()),
        },
        training=False,
    ).numpy().reshape(-1)

    predictions = {
        "Nathan": np.clip(nathan_values, 1.0, 10.0),
        "Alvajoy": np.asarray(
            [
                models["Alvajoy"]["algorithm"].predict(user_id, movie_id).est
                for user_id, movie_id in warm[
                    ["user_id", "movie_id"]
                ].itertuples(index=False, name=None)
            ]
        ),
        "Frank": np.asarray(
            [
                predict_frank_rating(
                    models["Frank"], ratings_by_user[user_id], movie_id
                )
                for user_id, movie_id in warm[
                    ["user_id", "movie_id"]
                ].itertuples(index=False, name=None)
            ]
        ),
        "Jace": np.asarray(
            [
                predict_jace_rating(models["Jace"], user_id, movie_id)
                for user_id, movie_id in warm[
                    ["user_id", "movie_id"]
                ].itertuples(index=False, name=None)
            ]
        ),
        "Average rating baseline": np.asarray(
            [movie_averages[movie_id] for movie_id in warm["movie_id"]]
        ),
    }
    return predictions


def _ranking_user_results(
    models,
    relevant_by_user,
    ratings_by_user,
    seen_by_user,
    movie_averages,
    movies_by_id,
    candidates,
    top_k,
):
    predictors = {
        "Nathan": lambda user_id: recommend_nathan(
            models["Nathan"], user_id, candidates, seen_by_user[user_id], top_k
        ),
        "Alvajoy": lambda user_id: recommend_alvajoy(
            models["Alvajoy"], user_id, candidates, seen_by_user[user_id], top_k
        ),
        "Frank": lambda user_id: recommend_existing_user(
            model=models["Frank"],
            user_id=user_id,
            user_ratings=ratings_by_user[user_id],
            seen_movie_ids=seen_by_user[user_id],
            movies_by_id=movies_by_id,
            top_k=top_k,
            candidate_movie_ids=candidates,
        ),
        "Jace": lambda user_id: recommend_jace(
            models["Jace"],
            str(user_id),
            {},
            top_k,
            candidate_movie_ids=candidates,
        )["recommendations"],
        "Average rating baseline": lambda user_id: _baseline_recommendations(
            movie_averages, candidates, seen_by_user[user_id], top_k
        ),
    }

    users = sorted(relevant_by_user)
    results = {}
    for model_name, predictor in predictors.items():
        print(
            f"Evaluating {model_name} top-{top_k} recommendations for "
            f"{len(users)} users...",
            flush=True,
        )
        user_results = []
        for index, user_id in enumerate(users, start=1):
            recommendations = predictor(user_id)
            _validate_recommendations(
                recommendations, candidates, seen_by_user[user_id], top_k
            )
            recommended_ids = [str(row["movie_id"]) for row in recommendations]
            recommended = set(recommended_ids)
            relevant = relevant_by_user[user_id]
            user_results.append(
                {
                    "user_id": user_id,
                    "recommended_ids": recommended_ids,
                    "relevant_ids": relevant,
                    "relevance_at_rank": [
                        movie_id in relevant for movie_id in recommended_ids
                    ],
                    "hits": len(recommended & relevant),
                    "relevant": len(relevant),
                    "ndcg": _binary_ndcg_at_k(
                        recommended_ids, relevant, top_k
                    ),
                }
            )
            if index % 100 == 0 or index == len(users):
                print(f"  {model_name}: {index}/{len(users)} users", flush=True)
        results[model_name] = user_results
    return results


def _ranking_metrics(
    models,
    relevant_by_user,
    ratings_by_user,
    seen_by_user,
    movie_averages,
    movies_by_id,
    candidates,
    top_k,
):
    user_results = _ranking_user_results(
        models,
        relevant_by_user,
        ratings_by_user,
        seen_by_user,
        movie_averages,
        movies_by_id,
        candidates,
        top_k,
    )
    return {
        model_name: _ranking_summary(rows, top_k)
        for model_name, rows in user_results.items()
    }


def _append_report(
    results_file,
    started_at,
    point_results,
    ranking_results,
    candidate_count,
    warm_count,
    cold_count,
    positive_count,
    negative_count,
    threshold,
    top_k,
    data_dir,
    model_dir,
):
    results_file.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not results_file.exists() or results_file.stat().st_size == 0
    lines = []
    if needs_header:
        lines.extend(
            [
                "# Model inference tests",
                "",
                "Warm-item rating prediction and recommendation-list evaluation.",
                "",
            ]
        )
    else:
        lines.append("")

    lines.extend(
        [
            f"## Evaluation {started_at}",
            "",
            f"- Data directory: `{data_dir}`",
            f"- Model directory: `{model_dir}`",
            f"- Shared candidate movies: {candidate_count}",
            f"- Warm held-out ratings: {warm_count}",
            f"- Cold held-out ratings excluded: {cold_count}",
            f"- Positive ratings (`>= {threshold}`): {positive_count}",
            f"- Negative ratings (`< {threshold}`): {negative_count}",
            f"- Recommendation list size: {top_k}",
            "- Individual predictions are processed in memory and are not written",
            "",
            "### Rating conversions",
            "",
            "- Nathan: native estimate clipped to `[1, 10]`.",
            "- Alvajoy: native Surprise SVD estimate.",
            "- Frank: `5.5 + centered similarity sum / absolute similarity sum`, clipped to `[1, 10]`; zero similarity returns `5.5`.",
            "- Jace: inverse training transform `5.5 + 4.5 × raw score`, clipped to `[1, 10]`.",
            "- Average rating baseline: training-only mean rating for the movie.",
            "",
            "### Pointwise warm-rating metrics",
            "",
            "| Model | Ratings | MAE | RMSE | Precision | Recall | FPR | FNR | TP | FP | TN | FN |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for model_name, result in point_results.items():
        lines.append(
            f"| {model_name} | {result['ratings']} | {result['mae']:.6f} | "
            f"{result['rmse']:.6f} | {result['precision']:.6f} | "
            f"{result['recall']:.6f} | {result['false_positive_rate']:.6f} | "
            f"{result['false_negative_rate']:.6f} | {result['tp']} | "
            f"{result['fp']} | {result['tn']} | {result['fn']} |"
        )

    lines.extend(
        [
            "",
            f"### Native top-{top_k} recommendation metrics",
            "",
            "Only users with at least one positive warm held-out rating are included. Unobserved catalog movies are treated as non-relevant, making precision conservative.",
            "",
            f"| Model | Users | Precision@{top_k} | Recall@{top_k} | Hit Rate@{top_k} | NDCG@{top_k} | Hits | Relevant |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for model_name, result in ranking_results.items():
        lines.append(
            f"| {model_name} | {result['users']} | "
            f"{result['precision_at_k']:.6f} | {result['recall_at_k']:.6f} | "
            f"{result['hit_rate_at_k']:.6f} | {result['ndcg_at_k']:.6f} | "
            f"{result['total_hits']} | {result['total_relevant']} |"
        )
    lines.extend(["", ""])

    with results_file.open("a", encoding="utf-8") as report:
        report.write("\n".join(lines))


def evaluate_inference(
    data_dir=DATA_PATH,
    model_dir=DEFAULT_MODEL_DIR,
    results_file=DEFAULT_RESULTS_FILE,
    threshold=DEFAULT_THRESHOLD,
    top_k=DEFAULT_TOP_K,
):
    if not 1.0 <= threshold <= 10.0:
        raise ValueError("threshold must be between 1 and 10")
    if top_k < 1:
        raise ValueError("top_k must be at least 1")

    data_dir = Path(data_dir)
    model_dir = Path(model_dir)
    results_file = Path(results_file)
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data = loadPreparedData(data_dir)
    candidates = _training_catalog(data)
    training, warm, cold = _rating_partitions(data, candidates)
    ratings_by_user = build_user_ratings(data.train_events)
    seen_by_user = _seen_by_user(data.train_events)
    movie_averages = training.groupby("movie_id")["rating"].mean().to_dict()
    movies_by_id = {
        str(movie["movie_id"]): movie for movie in data.movies.to_dict("records")
    }

    print("Loading model artifacts...", flush=True)
    models = _load_models(model_dir)
    _validate_catalogs(models, candidates)

    predictions = _point_predictions(
        models, warm, ratings_by_user, movie_averages
    )
    actual = warm["rating"].to_numpy(dtype=float)
    point_results = {
        model_name: _point_metrics(actual, values, threshold)
        for model_name, values in predictions.items()
    }

    positive_warm = warm.loc[warm["rating"] >= threshold]
    relevant_by_user = (
        positive_warm.groupby("user_id")["movie_id"].apply(set).to_dict()
    )
    ranking_results = _ranking_metrics(
        models,
        relevant_by_user,
        ratings_by_user,
        seen_by_user,
        movie_averages,
        movies_by_id,
        candidates,
        top_k,
    )

    positive_count = int(np.sum(actual >= threshold))
    negative_count = len(actual) - positive_count
    _append_report(
        results_file,
        started_at,
        point_results,
        ranking_results,
        len(candidates),
        len(warm),
        len(cold),
        positive_count,
        negative_count,
        threshold,
        top_k,
        data_dir,
        model_dir,
    )

    print("Pointwise metrics:", flush=True)
    for model_name, result in point_results.items():
        print(
            f"  {model_name}: MAE={result['mae']:.4f}, "
            f"RMSE={result['rmse']:.4f}, precision={result['precision']:.4f}, "
            f"recall={result['recall']:.4f}",
            flush=True,
        )
    print(f"Top-{top_k} recommendation metrics:", flush=True)
    for model_name, result in ranking_results.items():
        print(
            f"  {model_name}: precision@{top_k}="
            f"{result['precision_at_k']:.4f}, recall@{top_k}="
            f"{result['recall_at_k']:.4f}, hit-rate@{top_k}="
            f"{result['hit_rate_at_k']:.4f}, NDCG@{top_k}="
            f"{result['ndcg_at_k']:.4f}",
            flush=True,
        )
    print(f"Appended inference results to {results_file}", flush=True)
    return {
        "started_at": started_at,
        "pointwise": point_results,
        "ranking": ranking_results,
        "warm_ratings": len(warm),
        "cold_ratings": len(cold),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate warm-item rating predictions and top-K recommendations"
    )
    parser.add_argument("--data-dir", type=Path, default=DATA_PATH)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--results-file", type=Path, default=DEFAULT_RESULTS_FILE)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    args = parser.parse_args()
    evaluate_inference(
        data_dir=args.data_dir,
        model_dir=args.model_dir,
        results_file=args.results_file,
        threshold=args.threshold,
        top_k=args.top_k,
    )


if __name__ == "__main__":
    main()
