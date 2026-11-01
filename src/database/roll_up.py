"""Load the initial users, movies, events, and history snapshot.

This command is only for idempotent initial/backfill loading from the recorded CSV
snapshot. Live services and future Kafka consumers must import table APIs from
``src.database.interactions`` instead of invoking this command.
"""

import argparse
import csv
import gzip
import json
from collections.abc import Iterator
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from src.database.interactions import movies, stream_events, user_movie_history, users

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "app" / "data"


def _source_path(data_dir: Path, name: str) -> Path:
    for candidate in (data_dir / f"{name}.csv", data_dir / f"{name}.csv.gz"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"Missing {name}.csv or {name}.csv.gz in {data_dir}")


def _rows(path: Path) -> Iterator[dict[str, str]]:
    open_source = gzip.open if path.suffix == ".gz" else open
    with open_source(path, "rt", encoding="utf-8", newline="") as stream:
        yield from csv.DictReader(stream)


def _optional(value: str) -> str | None:
    return value if value != "" else None


def _chunks(records, size):
    for start in range(0, len(records), size):
        yield records[start : start + size]


def _event_id(row_number: int, row: dict[str, str]):
    identity = json.dumps(
        {"source": "events.csv", "row": row_number, "values": row},
        sort_keys=True,
        separators=(",", ":"),
    )
    return uuid5(NAMESPACE_URL, identity)


def roll_up(data_dir: Path, *, chunk_size: int = 1000) -> dict[str, object]:
    """Load one snapshot without replacing existing source records."""
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")

    user_rows = [
        {
            "user_id": row["user_id"],
            "age": row["age"],
            "occupation": row["occupation"],
            "gender": row["gender"],
            "self_description_likes": _optional(row["self_description_likes"]),
            "self_description_dislikes": _optional(row["self_description_dislikes"]),
        }
        for row in _rows(_source_path(data_dir, "users"))
    ]
    movie_rows = [
        {
            **row,
            "release_date": _optional(row["release_date"]),
            "runtime": _optional(row["runtime"]),
            "overview": _optional(row["overview"]),
            "popularity": _optional(row["popularity"]),
            "vote_average": _optional(row["vote_average"]),
            "vote_count": _optional(row["vote_count"]),
            "imdb_id": _optional(row["imdb_id"]),
            "tmdb_id": _optional(row["tmdb_id"]),
            "license_cost": _optional(row["license_cost"]),
        }
        for row in _rows(_source_path(data_dir, "movies"))
    ]
    skipped_user_ids = users.create_missing_many(user_rows)
    skipped_movie_ids = movies.create_missing_many(movie_rows)

    event_rows = []
    history: dict[tuple[str, str], dict[str, object]] = {}
    inserted_events = 0
    for row_number, row in enumerate(
        _rows(_source_path(data_dir, "events")), start=1
    ):
        event = {**row, "event_id": _event_id(row_number, row)}
        event_rows.append(event)
        if row["movie_id"]:
            key = (row["user_id"], row["movie_id"])
            history.setdefault(
                key,
                {"user_id": key[0], "movie_id": key[1], "latest_rating": None},
            )
            if row["event_type"] == "rating":
                history[key]["latest_rating"] = row["rating"]
        if len(event_rows) == chunk_size:
            inserted_events += stream_events.create_many(event_rows)
            event_rows = []
    if event_rows:
        inserted_events += stream_events.create_many(event_rows)
    for chunk in _chunks(list(history.values()), chunk_size):
        user_movie_history.upsert_many(chunk)

    return {
        "users": {
            "created": len(user_rows) - len(skipped_user_ids),
            "skipped_ids": skipped_user_ids,
        },
        "movies": {
            "created": len(movie_rows) - len(skipped_movie_ids),
            "skipped_ids": skipped_movie_ids,
        },
        "events_inserted": inserted_events,
        "history": len(history),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--chunk-size", type=int, default=1000)
    args = parser.parse_args()
    print(json.dumps(roll_up(args.data_dir, chunk_size=args.chunk_size), indent=2))


if __name__ == "__main__":
    main()
