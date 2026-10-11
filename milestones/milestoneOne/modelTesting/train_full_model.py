import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from Jace.recommender import load_model as load_jace
from Jace.recommender import train_model as train_jace
from utils.dataProvider import DATA_PATH, loadFullData

ROOT = Path(__file__).resolve().parent
DEFAULT_MODEL_PATH = ROOT / "full_models" / "jace_model.joblib"


def train_full_model(
    data_dir: Path | str = DATA_PATH,
    model_path: Path | str = DEFAULT_MODEL_PATH,
) -> dict:
    """Train and validate Jace's model using every event in a dataset."""
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data_dir = Path(data_dir)
    model_path = Path(model_path)
    model_path.parent.mkdir(parents=True, exist_ok=True)

    data = loadFullData(data_dir)
    print(f"Training Jace on all events from {data_dir}...", flush=True)
    train_started = perf_counter()
    stats = train_jace(data, model_path)
    train_seconds = perf_counter() - train_started

    load_started = perf_counter()
    load_jace(model_path)
    load_seconds = perf_counter() - load_started

    manifest = {
        "trained_at": started_at,
        "purpose": "recommendation_service_initial_model",
        "data_directory": str(data_dir.resolve()),
        "data": data.split_metadata,
        "model": {
            "name": "jace",
            "artifact": model_path.name,
            "artifact_bytes": model_path.stat().st_size,
            "train_seconds": train_seconds,
            "load_seconds": load_seconds,
            "stats": stats,
        },
    }
    manifest_path = model_path.parent / "training_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        f"Finished Jace: train={train_seconds:.3f}s, load={load_seconds:.3f}s",
        flush=True,
    )
    print(f"Wrote model: {model_path}", flush=True)
    print(f"Wrote training manifest: {manifest_path}", flush=True)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Train Jace's recommendation model on 100% of a dataset's events"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DATA_PATH,
        help="Directory containing events.csv.gz, users.csv.gz, and movies.csv.gz",
    )
    parser.add_argument(
        "--model-out",
        type=Path,
        default=DEFAULT_MODEL_PATH,
        help="Destination for the trained Jace joblib artifact",
    )
    args = parser.parse_args()
    train_full_model(args.data_dir, args.model_out)


if __name__ == "__main__":
    main()
