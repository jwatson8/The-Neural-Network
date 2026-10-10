import argparse
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from time import perf_counter

from Alvajoy.train import load_model as load_alvajoy
from Alvajoy.train import train_model as train_alvajoy
from Frank.src.train import load_model as load_frank
from Frank.src.train import train_model as train_frank
from Jace.recommender import load_model as load_jace
from Jace.recommender import train_model as train_jace
from Nathan.trainModelv1 import load_model as load_nathan
from Nathan.trainModelv1 import train_model as train_nathan
from utils.dataProvider import DATA_PATH, loadPreparedData

ROOT = Path(__file__).resolve().parent
DEFAULT_MODEL_DIR = ROOT / "models"
DEFAULT_RESULTS_FILE = ROOT / "results" / "training_times.md"
DEFAULT_RUNS = 20


def _average_timings(model_timings: dict) -> dict:
    return {
        name: {
            metric: fmean(run[metric] for run in runs)
            for metric in ("train", "load", "total")
        }
        for name, runs in model_timings.items()
    }


def _append_timing_report(
    results_file: Path,
    run_started_at: str,
    model_timings: dict,
    averages: dict,
    data_dir: Path,
    model_dir: Path,
    runs: int,
) -> None:
    results_file.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not results_file.exists() or results_file.stat().st_size == 0

    lines = []
    if needs_header:
        lines.extend(
            [
                "# Model training times",
                "",
                (
                    "Training time covers model fitting and artifact saving. Load time "
                    "covers reloading and validating the saved artifact."
                ),
                "",
            ]
        )

    lines.extend(
        [
            f"## Benchmark {run_started_at}",
            "",
            f"- Data directory: `{data_dir}`",
            f"- Model directory: `{model_dir}`",
            f"- Runs per model: {runs}",
            "- Model artifacts: replaced at the start of each run; final run retained",
            "",
            "### Averages",
            "",
            "| Model | Train (seconds) | Load (seconds) | Total (seconds) |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for name, timing in averages.items():
        lines.append(
            f"| {name.title()} | {timing['train']:.4f} | "
            f"{timing['load']:.4f} | {timing['total']:.4f} |"
        )

    lines.extend(
        [
            "",
            "### Individual runs",
            "",
            "| Model | Run | Train (seconds) | Load (seconds) | Total (seconds) |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for name, timings in model_timings.items():
        for run_number, timing in enumerate(timings, start=1):
            lines.append(
                f"| {name.title()} | {run_number} | {timing['train']:.4f} | "
                f"{timing['load']:.4f} | {timing['total']:.4f} |"
            )
    lines.extend(["", ""])

    with results_file.open("a", encoding="utf-8") as report:
        report.write("\n".join(lines))


def train_all(
    data_dir=DATA_PATH,
    nathan_epochs=10,
    results_file=DEFAULT_RESULTS_FILE,
    runs=DEFAULT_RUNS,
    model_dir=DEFAULT_MODEL_DIR,
):
    if runs < 1:
        raise ValueError("runs must be at least 1")

    run_started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data = loadPreparedData(data_dir)
    data_dir = Path(data_dir)
    results_file = Path(results_file)
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

    model_timings = {name: [] for name, _, _, _ in specs}
    total_jobs = runs * len(specs)
    completed_jobs = 0
    benchmark_started = perf_counter()

    for run_number in range(1, runs + 1):
        print(f"Starting benchmark round {run_number}/{runs}", flush=True)
        for _, filename, _, _ in specs:
            (model_dir / filename).unlink(missing_ok=True)
        print("  Cleared model artifacts from the previous round", flush=True)

        for name, filename, trainer, loader in specs:
            model_path = model_dir / filename
            print(
                f"  Training {name.title()} (run {run_number}/{runs})...",
                flush=True,
            )
            train_started = perf_counter()
            trainer(model_path)
            train_seconds = perf_counter() - train_started

            load_started = perf_counter()
            loader(model_path)
            load_seconds = perf_counter() - load_started
            timing = {
                "train": train_seconds,
                "load": load_seconds,
                "total": train_seconds + load_seconds,
            }
            model_timings[name].append(timing)

            completed_jobs += 1
            elapsed = perf_counter() - benchmark_started
            remaining = (elapsed / completed_jobs) * (total_jobs - completed_jobs)
            print(
                f"  Finished {name.title()}: train={train_seconds:.3f}s, "
                f"load={load_seconds:.3f}s, benchmark elapsed={elapsed:.1f}s, "
                f"estimated remaining={remaining:.1f}s",
                flush=True,
            )

    averages = _average_timings(model_timings)
    _append_timing_report(
        results_file,
        run_started_at,
        model_timings,
        averages,
        data_dir,
        model_dir,
        runs,
    )
    total_elapsed = perf_counter() - benchmark_started
    print("Average timings:", flush=True)
    for name, timing in averages.items():
        print(
            f"  {name.title()}: train={timing['train']:.4f}s, "
            f"load={timing['load']:.4f}s, total={timing['total']:.4f}s",
            flush=True,
        )
    print(f"Completed {total_jobs} training runs in {total_elapsed:.1f}s", flush=True)
    print(f"Appended benchmark results to {results_file}", flush=True)
    return {
        "started_at": run_started_at,
        "runs_per_model": runs,
        "timings": model_timings,
        "averages": averages,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Benchmark all four recommendation models on one shared split"
    )
    parser.add_argument("--data-dir", type=Path, default=DATA_PATH)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--nathan-epochs", type=int, default=10)
    parser.add_argument("--results-file", type=Path, default=DEFAULT_RESULTS_FILE)
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    args = parser.parse_args()
    train_all(
        data_dir=args.data_dir,
        nathan_epochs=args.nathan_epochs,
        results_file=args.results_file,
        runs=args.runs,
        model_dir=args.model_dir,
    )


if __name__ == "__main__":
    main()
