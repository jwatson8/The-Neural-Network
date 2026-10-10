import argparse
import random
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from time import perf_counter

from Alvajoy.train import load_model as load_alvajoy
from Alvajoy.train import recommend as recommend_alvajoy
from Frank.src.recommend import build_user_ratings, recommend_existing_user
from Frank.src.train import load_model as load_frank
from Jace.recommender import load_model as load_jace
from Jace.recommender import recommend as recommend_jace
from Nathan.trainModelv1 import load_model as load_nathan
from Nathan.trainModelv1 import recommend as recommend_nathan
from utils.dataProvider import DATA_PATH, loadPreparedData

ROOT = Path(__file__).resolve().parent
DEFAULT_MODEL_DIR = ROOT / "models"
DEFAULT_RESULTS_FILE = ROOT / "results" / "training_times.md"
DEFAULT_TESTS = 100
DEFAULT_TOP_K = 20
DEFAULT_SEED = 42


def _training_catalog(data):
    interacted = set(
        data.train_events.loc[
            data.train_events["movie_id"].notna(), "movie_id"
        ].astype(str)
    )
    return [
        str(movie_id)
        for movie_id in data.movies["movie_id"]
        if str(movie_id) in interacted
    ]


def _seen_by_user(events):
    interactions = events.loc[
        events["movie_id"].notna(), ["user_id", "movie_id"]
    ].copy()
    interactions["movie_id"] = interactions["movie_id"].astype(str)
    return interactions.groupby("user_id")["movie_id"].apply(set).to_dict()


def _cohort_counts(data, training_catalog):
    test_pairs = data.test_events.loc[
        data.test_events["movie_id"].notna(), ["user_id", "movie_id"]
    ].drop_duplicates()
    test_pairs = test_pairs.copy()
    test_pairs["movie_id"] = test_pairs["movie_id"].astype(str)
    warm_mask = test_pairs["movie_id"].isin(set(training_catalog))

    cohorts = {}
    for name, mask in (("warm", warm_mask), ("cold", ~warm_mask)):
        cohort = test_pairs.loc[mask]
        cohorts[name] = {
            "pairs": len(cohort),
            "users": cohort["user_id"].nunique(),
            "movies": cohort["movie_id"].nunique(),
        }
    return cohorts, set(test_pairs.loc[warm_mask, "user_id"])


def _select_users(ratings_by_user, warm_test_users, tests, seed):
    eligible = sorted(set(ratings_by_user) & set(warm_test_users))
    if tests < 1:
        raise ValueError("tests must be at least 1")
    if tests > len(eligible):
        raise ValueError(
            f"Requested {tests} users, but only {len(eligible)} users have both "
            "training ratings and warm test interactions"
        )
    return random.Random(seed).sample(eligible, tests)


def _validate_catalogs(models, training_catalog):
    expected = set(training_catalog)

    alvajoy_items = {
        str(models["Alvajoy"]["algorithm"].trainset.to_raw_iid(inner_id))
        for inner_id in models["Alvajoy"]["algorithm"].trainset.all_items()
    }
    if alvajoy_items != expected:
        raise ValueError("Alvajoy's artifact does not match the shared training catalog")

    frank_items = {str(movie_id) for movie_id in models["Frank"]["movie_ids"]}
    if frank_items != expected:
        raise ValueError("Frank's artifact does not match the shared training catalog")

    jace_items = set(models["Jace"]["mi"])
    if not expected <= jace_items:
        raise ValueError("Jace's artifact is missing movies from the training catalog")

    lookup_vocabularies = []
    for layer in models["Nathan"].layers:
        if hasattr(layer, "get_vocabulary"):
            vocabulary = {str(value) for value in layer.get_vocabulary()}
            vocabulary.discard("[UNK]")
            lookup_vocabularies.append(vocabulary)
    if not any(vocabulary == expected for vocabulary in lookup_vocabularies):
        raise ValueError("Nathan's artifact does not match the shared training catalog")


def _validate_recommendations(recommendations, candidates, seen, top_k):
    if len(recommendations) != top_k:
        raise ValueError(
            f"Expected {top_k} recommendations, received {len(recommendations)}"
        )
    movie_ids = [str(item["movie_id"]) for item in recommendations]
    if len(movie_ids) != len(set(movie_ids)):
        raise ValueError("A predictor returned duplicate movie IDs")
    if not set(movie_ids) <= set(candidates):
        raise ValueError("A predictor returned a movie outside the shared catalog")
    if set(movie_ids) & set(seen):
        raise ValueError("A predictor returned a movie already seen during training")


def _summaries(measurements):
    summaries = {}
    for model_name, rows in measurements.items():
        seconds = [row["seconds"] for row in rows]
        total = sum(seconds)
        average = fmean(seconds)
        summaries[model_name] = {
            "average_seconds": average,
            "average_milliseconds": average * 1000,
            "requests_per_second": len(seconds) / total,
        }
    return summaries


def _append_report(
    results_file,
    started_at,
    measurements,
    summaries,
    cohorts,
    training_catalog,
    tests,
    top_k,
    seed,
    data_dir,
    model_dir,
):
    results_file = Path(results_file)
    results_file.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not results_file.exists() or results_file.stat().st_size == 0

    lines = []
    if needs_header:
        lines.extend(
            [
                "# Model benchmark results",
                "",
                "Training and top-20 prediction benchmark history.",
                "",
            ]
        )
    else:
        lines.append("")

    lines.extend(
        [
            f"## Prediction throughput benchmark {started_at}",
            "",
            f"- Data directory: `{data_dir}`",
            f"- Model directory: `{model_dir}`",
            f"- Requests per model: {tests}",
            f"- Top K: {top_k}",
            f"- User sample seed: {seed}",
            f"- Shared candidate movies: {len(training_catalog)}",
            "- Timing scope: scoring, seen-item filtering, ranking, and top-K construction",
            "- Model loading, data preparation, and one warm-up request are excluded",
            "",
            "### Throughput summary",
            "",
            "| Model | Candidates | Requests | Average (seconds) | Average (ms) | Requests/second |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for model_name, summary in summaries.items():
        lines.append(
            f"| {model_name} | {len(training_catalog)} | {tests} | "
            f"{summary['average_seconds']:.6f} | "
            f"{summary['average_milliseconds']:.3f} | "
            f"{summary['requests_per_second']:.3f} |"
        )

    lines.extend(
        [
            "",
            "### Test-item cohorts",
            "",
            "Warm items occur in the training catalog. Cold items first occur in the test split and are excluded from the shared throughput workload.",
            "",
            "| Cohort | Movies | User-movie pairs | Users | Included |",
            "| --- | ---: | ---: | ---: | --- |",
            f"| Warm | {cohorts['warm']['movies']} | {cohorts['warm']['pairs']} | {cohorts['warm']['users']} | Yes |",
            f"| Cold | {cohorts['cold']['movies']} | {cohorts['cold']['pairs']} | {cohorts['cold']['users']} | No |",
            "",
            "| Model | Warm pairs supported | Cold pairs included |",
            "| --- | ---: | ---: |",
        ]
    )
    for model_name in measurements:
        lines.append(f"| {model_name} | {cohorts['warm']['pairs']} | 0 |")

    lines.extend(
        [
            "",
            "### Individual prediction requests",
            "",
            "| Model | Test | User ID | Candidates | Returned | Seconds |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for model_name, rows in measurements.items():
        for row in rows:
            lines.append(
                f"| {model_name} | {row['test']} | {row['user_id']} | "
                f"{len(training_catalog)} | {row['returned']} | {row['seconds']:.6f} |"
            )
    lines.extend(["", ""])

    with results_file.open("a", encoding="utf-8") as report:
        report.write("\n".join(lines))


def benchmark_throughput(
    data_dir=DATA_PATH,
    model_dir=DEFAULT_MODEL_DIR,
    results_file=DEFAULT_RESULTS_FILE,
    tests=DEFAULT_TESTS,
    top_k=DEFAULT_TOP_K,
    seed=DEFAULT_SEED,
):
    if top_k < 1:
        raise ValueError("top_k must be at least 1")

    data_dir = Path(data_dir)
    model_dir = Path(model_dir)
    results_file = Path(results_file)
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data = loadPreparedData(data_dir)
    candidates = _training_catalog(data)
    seen_by_user = _seen_by_user(data.train_events)
    ratings_by_user = build_user_ratings(data.train_events)
    cohorts, warm_test_users = _cohort_counts(data, candidates)
    users = _select_users(ratings_by_user, warm_test_users, tests, seed)
    movies_by_id = {
        str(movie["movie_id"]): movie for movie in data.movies.to_dict("records")
    }

    model_paths = {
        "Nathan": model_dir / "nathan_model.keras",
        "Alvajoy": model_dir / "alvajoy_model.joblib",
        "Frank": model_dir / "frank_model.joblib",
        "Jace": model_dir / "jace_model.joblib",
    }
    missing = [str(path) for path in model_paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            f"Missing model artifacts: {missing}. Run python train_all.py first."
        )

    print("Loading four model artifacts (excluded from prediction timing)...", flush=True)
    models = {
        "Nathan": load_nathan(model_paths["Nathan"]),
        "Alvajoy": load_alvajoy(model_paths["Alvajoy"]),
        "Frank": load_frank(model_paths["Frank"]),
        "Jace": load_jace(model_paths["Jace"]),
    }
    _validate_catalogs(models, candidates)

    predictors = {
        "Nathan": lambda user_id: recommend_nathan(
            models["Nathan"],
            user_id,
            candidates,
            seen_by_user.get(user_id, set()),
            top_k,
        ),
        "Alvajoy": lambda user_id: recommend_alvajoy(
            models["Alvajoy"],
            user_id,
            candidates,
            seen_by_user.get(user_id, set()),
            top_k,
        ),
        "Frank": lambda user_id: recommend_existing_user(
            model=models["Frank"],
            user_id=user_id,
            user_ratings=ratings_by_user.get(user_id, {}),
            seen_movie_ids=seen_by_user.get(user_id, set()),
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
    }

    print("Running one unmeasured warm-up request per model...", flush=True)
    for model_name, predictor in predictors.items():
        recommendations = predictor(users[0])
        _validate_recommendations(
            recommendations, candidates, seen_by_user.get(users[0], set()), top_k
        )
        print(f"  Warmed {model_name}", flush=True)

    measurements = {model_name: [] for model_name in predictors}
    total_requests = len(predictors) * tests
    completed = 0
    benchmark_started = perf_counter()

    for model_name, predictor in predictors.items():
        print(f"Benchmarking {model_name}...", flush=True)
        for test_number, user_id in enumerate(users, start=1):
            request_started = perf_counter()
            recommendations = predictor(user_id)
            seconds = perf_counter() - request_started
            _validate_recommendations(
                recommendations, candidates, seen_by_user.get(user_id, set()), top_k
            )
            measurements[model_name].append(
                {
                    "test": test_number,
                    "user_id": user_id,
                    "returned": len(recommendations),
                    "seconds": seconds,
                }
            )

            completed += 1
            elapsed = perf_counter() - benchmark_started
            remaining = (elapsed / completed) * (total_requests - completed)
            print(
                f"  {model_name} {test_number}/{tests}, user={user_id}, "
                f"request={seconds:.6f}s, elapsed={elapsed:.1f}s, "
                f"estimated remaining={remaining:.1f}s",
                flush=True,
            )

    summaries = _summaries(measurements)
    _append_report(
        results_file,
        started_at,
        measurements,
        summaries,
        cohorts,
        candidates,
        tests,
        top_k,
        seed,
        data_dir,
        model_dir,
    )

    print("Average prediction throughput:", flush=True)
    for model_name, summary in summaries.items():
        print(
            f"  {model_name}: {summary['average_milliseconds']:.3f} ms/request, "
            f"{summary['requests_per_second']:.3f} requests/second",
            flush=True,
        )
    print(f"Appended throughput results to {results_file}", flush=True)
    return {
        "started_at": started_at,
        "users": users,
        "candidate_count": len(candidates),
        "cohorts": cohorts,
        "measurements": measurements,
        "summaries": summaries,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Benchmark top-20 recommendation throughput for all four models"
    )
    parser.add_argument("--data-dir", type=Path, default=DATA_PATH)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--results-file", type=Path, default=DEFAULT_RESULTS_FILE)
    parser.add_argument("--tests", type=int, default=DEFAULT_TESTS)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()
    benchmark_throughput(
        data_dir=args.data_dir,
        model_dir=args.model_dir,
        results_file=args.results_file,
        tests=args.tests,
        top_k=args.top_k,
        seed=args.seed,
    )


if __name__ == "__main__":
    main()
