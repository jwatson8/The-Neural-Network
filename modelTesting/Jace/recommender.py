import argparse
import csv
import gzip
import hashlib
import json
from pathlib import Path

import joblib
import numpy as np
from scipy.sparse import csr_matrix
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer

from utils.dataProvider import DATA_PATH, PreparedModelData, loadPreparedData

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DEFAULT_MODEL_PATH = PROJECT_ROOT / "models" / "jace_model.joblib"


def read_csv(path):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def descriptions(user):
    return {
        key: user.get(key, "")
        for key in ("self_description_likes", "self_description_dislikes")
    }


def description_hash(user):
    return hashlib.sha256(
        json.dumps(descriptions(user), sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def load_data(directory):
    events = read_csv(directory / "events.csv.gz")
    users = read_csv(directory / "users.csv.gz")
    movies = read_csv(directory / "movies.csv.gz")
    if len({u["user_id"] for u in users}) != len(users):
        raise ValueError("Duplicate user IDs")
    if len({m["movie_id"] for m in movies}) != len(movies):
        raise ValueError("Duplicate movie IDs")
    uid = {u["user_id"] for u in users}
    mid = {m["movie_id"] for m in movies}
    for e in events:
        if e["user_id"] not in uid:
            raise ValueError("Event references missing user")
        if e["event_type"] not in ("watch", "rating", "account_created"):
            raise ValueError("Unexpected event type")
        if e["event_type"] != "account_created" and e["movie_id"] not in mid:
            raise ValueError("Event references missing movie")
        if e["event_type"] == "rating" and not 1 <= float(e["rating"]) <= 10:
            raise ValueError("Rating outside 1–10")
    return events, users, movies


def fit(events, users, movies, components=32):
    ui = {u["user_id"]: i for i, u in enumerate(users)}
    mi = {m["movie_id"]: i for i, m in enumerate(movies)}
    seen = {u: set() for u in ui}
    values, rated = {}, set()
    # A watch counted once per user isn't great evidence of preference so i made an explicit rating overwrite it because it is clearer
    for e in sorted(events, key=lambda e: e["timestamp"]):
        if e["event_type"] == "account_created":
            continue
        u, m = e["user_id"], e["movie_id"]
        seen[u].add(mi[m])
        pair = (ui[u], mi[m])
        if e["event_type"] == "rating":
            values[pair] = (float(e["rating"]) - 5.5) / 4.5
            rated.add(pair)
        elif pair not in rated:
            values[pair] = 0.25
    if not values:
        raise ValueError("No training interactions")
    pairs = list(values)
    matrix = csr_matrix(
        ([values[p] for p in pairs], ([p[0] for p in pairs], [p[1] for p in pairs])),
        shape=(len(users), len(movies)),
        dtype=np.float64,
    )
    rank = min(components, min(matrix.shape) - 1)
    if rank < 1:
        raise ValueError("Need at least two users and movies")
    svd = TruncatedSVD(n_components=rank, n_iter=10, random_state=42)
    user_factors = svd.fit_transform(matrix)
    documents = [
        " ".join(
            [m["title"], m.get("genres", "").replace("|", " "), m.get("overview", "")]
        )
        for m in movies
    ]
    vectorizer = TfidfVectorizer(
        stop_words="english", ngram_range=(1, 2), sublinear_tf=True, max_features=20000
    )
    content = vectorizer.fit_transform(documents)
    positive_count = np.asarray((matrix > 0).sum(axis=0)).ravel().astype(float)
    popularity = np.log1p(positive_count)
    popularity /= max(float(popularity.max()), 1.0)
    return dict(
        users=users,
        movies=movies,
        ui=ui,
        mi=mi,
        seen=seen,
        user_factors=user_factors,
        movie_factors=svd.components_,
        vectorizer=vectorizer,
        content=content,
        popularity=popularity,
        stats=dict(
            events=len(events),
            users=len(users),
            movies=len(movies),
            user_movie_pairs=len(values),
            components=rank,
            active_users=sum(bool(s) for s in seen.values()),
        ),
    )


def load_profiles(path):
    return (
        json.loads(path.read_text(encoding="utf-8"))["profiles"]
        if path.exists()
        else {}
    )


def predict_rating(model, user_id, movie_id):
    """Map Jace's normalized collaborative score back to the 1–10 scale."""
    user_id = str(user_id)
    movie_id = str(movie_id)
    if user_id not in model["ui"] or not model["seen"].get(user_id):
        raise ValueError(f"User {user_id} has no collaborative history")
    if movie_id not in model["mi"]:
        raise ValueError(f"Movie {movie_id} is missing from Jace's model")

    raw_score = float(
        model["user_factors"][model["ui"][user_id]]
        @ model["movie_factors"][:, model["mi"][movie_id]]
    )
    return float(np.clip(5.5 + 4.5 * raw_score, 1.0, 10.0))


def recommend(model, user_id, profiles, k=10, candidate_movie_ids=None):
    if k < 1:
        raise ValueError("k must be positive")

    if candidate_movie_ids is None:
        candidate_indices = np.arange(len(model["movies"]), dtype=int)
    else:
        requested = {str(movie_id) for movie_id in candidate_movie_ids}
        missing = requested - set(model["mi"])
        if missing:
            raise ValueError(
                f"Candidate movies are missing from Jace's model: {sorted(missing)[:5]}"
            )
        candidate_indices = np.asarray(
            [
                index
                for index, movie in enumerate(model["movies"])
                if movie["movie_id"] in requested
            ],
            dtype=int,
        )

    seen = model["seen"].get(user_id, set())
    if seen:
        scores = (
            model["user_factors"][model["ui"][user_id]]
            @ model["movie_factors"][:, candidate_indices]
        )
        method = "collaborative_svd"
    elif user_id in profiles:
        profile = profiles[user_id]
        if user_id not in model["ui"]:
            raise ValueError("Profile user is missing from users.csv.gz; retrain first")
        user = model["users"][model["ui"][user_id]]
        if profile["input_sha256"] != description_hash(user):
            raise ValueError(
                "Stale LLM profile: regenerate after changing descriptions"
            )
        query = model["vectorizer"].transform(
            [profile["positive"], profile["negative"]]
        )
        matches = (model["content"][candidate_indices] @ query.T).toarray()
        scores = (
            matches[:, 0]
            - 0.7 * matches[:, 1]
            + 0.02 * model["popularity"][candidate_indices]
        )
        method = "llm_content"
    else:
        # Don't bypass the required LLM when descriptions are available
        if user_id in model["ui"]:
            user = model["users"][model["ui"][user_id]]
            if any(descriptions(user).values()):
                raise ValueError(
                    "No LLM profile for this new user. Run cold_start.py first."
                )
        scores = model["popularity"][candidate_indices].copy()
        method = "popularity_no_description"
    scores = np.asarray(scores).copy()
    if seen:
        scores[np.isin(candidate_indices, list(seen))] = -np.inf
    # Stable tie ordering follows input movie order
    ranked = np.argsort(-scores, kind="stable")
    picks = [
        (int(candidate_indices[i]), float(scores[i]))
        for i in ranked
        if np.isfinite(scores[i])
    ][:k]
    return dict(
        user_id=user_id,
        method=method,
        recommendations=[
            dict(
                movie_id=model["movies"][i]["movie_id"],
                title=model["movies"][i]["title"],
                score=round(score, 6),
            )
            for i, score in picks
        ],
    )


def train_model(data: PreparedModelData, model_path=DEFAULT_MODEL_PATH):
    """Fit Jace's model using only the shared training interactions."""
    events = data.train_events.copy()
    users = data.users.copy()
    movies = data.movies.copy()
    events["user_id"] = events["user_id"].astype(str)
    events.loc[events["movie_id"].notna(), "movie_id"] = events.loc[
        events["movie_id"].notna(), "movie_id"
    ].astype(str)
    users["user_id"] = users["user_id"].astype(str)
    movies["movie_id"] = movies["movie_id"].astype(str)
    model = fit(
        events.astype(object).where(events.notna(), "").to_dict("records"),
        users.astype(object).where(users.notna(), "").to_dict("records"),
        movies.astype(object).where(movies.notna(), "").to_dict("records"),
    )
    model_path = Path(model_path)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, model_path)
    return model["stats"]


def load_model(model_path=DEFAULT_MODEL_PATH):
    model = joblib.load(Path(model_path))
    required = {
        "users",
        "movies",
        "ui",
        "mi",
        "seen",
        "user_factors",
        "movie_factors",
        "vectorizer",
        "content",
        "popularity",
    }
    if not required <= set(model):
        raise ValueError(f"Jace artifact is missing keys: {sorted(required - set(model))}")
    return model


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DATA_PATH)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL_PATH)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("train")
    rec = sub.add_parser("recommend")
    rec.add_argument("--user-id", required=True)
    rec.add_argument("--k", type=int, default=10)
    rec.add_argument("--profiles", type=Path, default=ROOT / "llm_profiles.json")
    args = parser.parse_args()
    if args.command == "train":
        stats = train_model(loadPreparedData(args.data), args.model)
        load_model(args.model)
        print(json.dumps(stats, indent=2))
    else:
        model = load_model(args.model)
        print(
            json.dumps(
                recommend(model, args.user_id, load_profiles(args.profiles), args.k),
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
