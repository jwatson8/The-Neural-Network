import hashlib
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from utils.dataSplitter import pairFingerprint, splitDataForML

ROOT = Path(__file__).parent.parent
DATA_PATH = ROOT / "data"


def getDataPath(filename):
    return DATA_PATH / filename


@dataclass(frozen=True)
class PreparedModelData:
    train_events: pd.DataFrame
    test_events: pd.DataFrame
    users: pd.DataFrame
    movies: pd.DataFrame
    split_metadata: dict


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def loadPreparedData(
    data_dir: Path | str = DATA_PATH, split_percent: float = 0.8
) -> PreparedModelData:
    """Load the shared files and make the one split consumed by every model."""
    data_dir = Path(data_dir)
    paths = {
        "events": data_dir / "events.csv.gz",
        "users": data_dir / "users.csv.gz",
        "movies": data_dir / "movies.csv.gz",
    }
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Missing data files: {missing}")

    events = pd.read_csv(paths["events"])
    users = pd.read_csv(paths["users"])
    movies = pd.read_csv(paths["movies"])
    train_events, test_events = splitDataForML(events, split_percent)

    train_pairs = train_events.loc[
        train_events["movie_id"].notna(), ["user_id", "movie_id"]
    ].drop_duplicates()
    test_pairs = test_events.loc[
        test_events["movie_id"].notna(), ["user_id", "movie_id"]
    ].drop_duplicates()
    metadata = {
        "strategy": "per_user_chronological_movie_pair",
        "split_percent": split_percent,
        "source_sha256": {name: _sha256(path) for name, path in paths.items()},
        "train_event_count": len(train_events),
        "test_event_count": len(test_events),
        "train_pair_count": len(train_pairs),
        "test_pair_count": len(test_pairs),
        "train_pair_fingerprint": pairFingerprint(train_events),
        "test_pair_fingerprint": pairFingerprint(test_events),
    }
    return PreparedModelData(train_events, test_events, users, movies, metadata)
