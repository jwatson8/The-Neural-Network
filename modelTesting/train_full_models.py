import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from Alvajoy.train import load_model as load_alvajoy
from Alvajoy.train import train_model as train_alvajoy
from Frank.src.train import load_model as load_frank
from Frank.src.train import train_model as train_frank
from Jace.recommender import load_model as load_jace
from Jace.recommender import train_model as train_jace
from Nathan.trainModelv1 import load_model as load_nathan
from Nathan.trainModelv1 import train_model as train_nathan
from utils.dataProvider import DATA_PATH, loadFullData

ROOT = Path(__file__).resolve().parent
DEFAULT_FULL_MODEL_DIR = ROOT / "full_models"


def train_full_models(
    data_dir: Path | str = DATA_PATH,
    model_dir: Path | str = DEFAULT_FULL_MODEL_DIR,
    nathan_epochs: int = 10,
) -> dict:
    """Train and validate all four artifacts using the complete event history."""
    if nathan_epochs < 1:
        raise ValueError("nathan_epochs must be at least 1")

    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data = loadFullData(data_dir)
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)

    specs = [
        (
            "nathan",
            "nathan_model.keras",
            lambda path: train_nathan(data, path, epochs=nathan_epochs),
            load_nathan,
        ),
        (
            "alvajoy",
            "alvajoy_model.joblib",
            lambda path: train_alvajoy(data, path),
            load_alvajoy,
        ),
        (
            "frank",
            "frank_model.joblib",
            lambda path: train_frank(data, path),
            load_frank,
        ),
        (
            "jace",
            "jace_model.joblib",
            lambda path: train_jace(data, path),
            load_jace,
        ),
    ]

    model_results = {}
    for name, filename, trainer, loader in specs:
        model_path = model_dir / filename
        print(f"Training {name.title()} on all events...", flush=True)
        train_started = perf_counter()
        stats = trainer(model_path)
        train_seconds = perf_counter() - train_started

        load_started = perf_counter()
        loader(model_path)
        load_seconds = perf_counter() - load_started
        model_results[name] = {
            "artifact": filename,
            "artifact_bytes": model_path.stat().st_size,
            "train_seconds": train_seconds,
            "load_seconds": load_seconds,
            "stats": stats,
        }
        print(
            f"Finished {name.title()}: train={train_seconds:.3f}s, "
            f"load={load_seconds:.3f}s",
            flush=True,
        )

    manifest = {
        "trained_at": started_at,
        "purpose": "recommendation_service_initial_models",
        "data_directory": str(Path(data_dir).resolve()),
        "data": data.split_metadata,
        "nathan_epochs": nathan_epochs,
        "models": model_results,
    }
    manifest_path = model_dir / "training_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Wrote training manifest: {manifest_path}", flush=True)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Train all four recommendation models on 100% of events"
    )
    parser.add_argument("--data-dir", type=Path, default=DATA_PATH)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_FULL_MODEL_DIR)
    parser.add_argument("--nathan-epochs", type=int, default=10)
    args = parser.parse_args()
    train_full_models(args.data_dir, args.model_dir, args.nathan_epochs)


if __name__ == "__main__":
    main()
