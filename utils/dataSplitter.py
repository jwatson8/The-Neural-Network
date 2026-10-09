import hashlib
import json

import pandas as pd


def splitDataForML(
    events: pd.DataFrame, splitPercent: float
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Split complete user/movie interactions chronologically for each user.

    All rows for a user/movie pair are kept together. This is important for the
    supplied data, where each movie interaction has both a watch and a rating
    row. Rows without a movie (for example ``account_created``) are context and
    are assigned to training only.

    Args:
        events: events dataFrame
        splitPercent: floating value between 0 and 1, non-inclusive
    Returns:
        trainData: Earlier interaction pairs plus context-only rows.
        testData: Later interaction pairs used for evaluation.
    """
    if splitPercent <= 0 or splitPercent >= 1:
        raise ValueError("splitPercent must be between (0, 1) exclusive")

    required = {"timestamp", "user_id", "movie_id"}
    missing = required - set(events.columns)
    if missing:
        raise ValueError(f"events is missing required columns: {sorted(missing)}")

    working = events.copy()
    working["timestamp"] = pd.to_datetime(working["timestamp"], errors="raise")

    interactions = working.loc[working["movie_id"].notna()].copy()
    context = working.loc[working["movie_id"].isna()].copy()

    pair_times = (
        interactions.groupby(["user_id", "movie_id"], sort=False)["timestamp"]
        .max()
        .reset_index(name="latest_timestamp")
    )

    train_pairs: set[tuple[object, object]] = set()
    test_pairs: set[tuple[object, object]] = set()

    for _, user_pairs in pair_times.groupby("user_id", sort=False):
        ordered = user_pairs.assign(
            _movie_sort=user_pairs["movie_id"].astype(str)
        ).sort_values(["latest_timestamp", "_movie_sort"], kind="stable")

        pair_count = len(ordered)
        if pair_count == 1:
            split_index = 1
        else:
            split_index = max(
                1, min(int(pair_count * splitPercent), pair_count - 1)
            )

        keys = list(zip(ordered["user_id"], ordered["movie_id"]))
        train_pairs.update(keys[:split_index])
        test_pairs.update(keys[split_index:])

    pair_index = pd.MultiIndex.from_frame(interactions[["user_id", "movie_id"]])
    train_index = pd.MultiIndex.from_tuples(
        list(train_pairs), names=["user_id", "movie_id"]
    )
    test_index = pd.MultiIndex.from_tuples(
        list(test_pairs), names=["user_id", "movie_id"]
    )

    train_interactions = interactions.loc[pair_index.isin(train_index)]
    test_interactions = interactions.loc[pair_index.isin(test_index)]

    train_export = pd.concat([context, train_interactions]).sort_index().copy()
    test_export = test_interactions.sort_index().copy()

    return train_export, test_export


def pairFingerprint(events: pd.DataFrame) -> str:
    """Return a deterministic fingerprint of an event frame's pair membership."""
    pairs = events.loc[events["movie_id"].notna(), ["user_id", "movie_id"]]
    normalized = sorted(
        {
            (str(user_id), str(movie_id))
            for user_id, movie_id in pairs.itertuples(index=False)
        }
    )
    payload = json.dumps(normalized, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
