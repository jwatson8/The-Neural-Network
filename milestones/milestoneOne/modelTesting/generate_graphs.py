import argparse
import os
import tempfile
from pathlib import Path
from statistics import pstdev

os.environ.setdefault(
    "MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "model-comparison-matplotlib")
)

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter

ROOT = Path(__file__).resolve().parent
DEFAULT_INFERENCE_RESULTS = ROOT / "results" / "inference_tests.md"
DEFAULT_TRAINING_RESULTS = ROOT / "results" / "training_times.md"
DEFAULT_MODEL_DIR = ROOT / "models"
DEFAULT_OUTPUT_DIR = ROOT / "results" / "graphs"

ALL_MODELS = ["Nathan", "Alvajoy", "Frank", "Jace", "Average rating baseline"]
PERSISTED_MODELS = ALL_MODELS[:4]
COLORS = {
    "Nathan": "#4C78A8",
    "Alvajoy": "#F58518",
    "Frank": "#54A24B",
    "Jace": "#E45756",
    "Average rating baseline": "#B279A2",
}
ARTIFACTS = {
    "Nathan": "nathan_model.keras",
    "Alvajoy": "alvajoy_model.joblib",
    "Frank": "frank_model.joblib",
    "Jace": "jace_model.joblib",
}


def _latest_level_two_section(text, prefix):
    lines = text.splitlines()
    matches = [index for index, line in enumerate(lines) if line.startswith(prefix)]
    if not matches:
        raise ValueError(f"Missing results section beginning with: {prefix}")
    start = matches[-1]
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith("## ")
        ),
        len(lines),
    )
    return lines[start], lines[start:end]


def _table_after(section, heading):
    try:
        heading_index = section.index(heading)
    except ValueError as error:
        raise ValueError(f"Missing table heading: {heading}") from error

    table_start = next(
        (
            index
            for index in range(heading_index + 1, len(section))
            if section[index].startswith("|")
        ),
        None,
    )
    if table_start is None or table_start + 2 > len(section):
        raise ValueError(f"Missing Markdown table after: {heading}")

    table_lines = []
    for line in section[table_start:]:
        if not line.startswith("|"):
            break
        table_lines.append(line)
    if len(table_lines) < 3:
        raise ValueError(f"Incomplete Markdown table after: {heading}")

    def cells(line):
        return [cell.strip() for cell in line.strip().strip("|").split("|")]

    headers = cells(table_lines[0])
    rows = []
    for line in table_lines[2:]:
        values = cells(line)
        if len(values) != len(headers):
            raise ValueError(f"Malformed table row after {heading}: {line}")
        rows.append(dict(zip(headers, values)))
    return rows


def _rows_by_model(rows, expected_models):
    indexed = {row["Model"]: row for row in rows}
    missing = [model for model in expected_models if model not in indexed]
    if missing:
        raise ValueError(f"Results table is missing models: {missing}")
    return {model: indexed[model] for model in expected_models}


def _parse_results(inference_path, training_path):
    inference_text = Path(inference_path).read_text(encoding="utf-8")
    training_text = Path(training_path).read_text(encoding="utf-8")

    inference_title, inference_section = _latest_level_two_section(
        inference_text, "## Evaluation "
    )
    pointwise = _rows_by_model(
        _table_after(inference_section, "### Pointwise warm-rating metrics"),
        ALL_MODELS,
    )
    native_heading = next(
        (
            line
            for line in inference_section
            if line.startswith("### Native top-")
        ),
        None,
    )
    if native_heading is None or "top-20" not in native_heading:
        raise ValueError("Latest inference results are not a top-20 evaluation")
    native = _rows_by_model(
        _table_after(inference_section, native_heading), ALL_MODELS
    )

    training_title, training_section = _latest_level_two_section(
        training_text, "## Benchmark "
    )
    training_averages = _rows_by_model(
        _table_after(training_section, "### Averages"), PERSISTED_MODELS
    )
    training_runs = _table_after(training_section, "### Individual runs")

    throughput_title, throughput_section = _latest_level_two_section(
        training_text, "## Prediction throughput benchmark "
    )
    if "- Top K: 20" not in throughput_section:
        raise ValueError("Latest throughput results are not a top-20 benchmark")
    throughput_summary = _rows_by_model(
        _table_after(throughput_section, "### Throughput summary"),
        PERSISTED_MODELS,
    )
    throughput_runs = _table_after(
        throughput_section, "### Individual prediction requests"
    )

    return {
        "inference_title": inference_title.removeprefix("## "),
        "training_title": training_title.removeprefix("## "),
        "throughput_title": throughput_title.removeprefix("## "),
        "pointwise": pointwise,
        "native": native,
        "training_averages": training_averages,
        "training_runs": training_runs,
        "throughput_summary": throughput_summary,
        "throughput_runs": throughput_runs,
    }


def _timing_standard_deviations(rows, column, multiplier=1.0):
    values = {model: [] for model in PERSISTED_MODELS}
    for row in rows:
        model = row.get("Model")
        if model in values:
            values[model].append(float(row[column]) * multiplier)
    missing = [model for model, samples in values.items() if not samples]
    if missing:
        raise ValueError(f"Timing samples are missing models: {missing}")
    return {
        model: pstdev(samples) if len(samples) > 1 else 0.0
        for model, samples in values.items()
    }


def _save_figure(figure, output_path):
    figure.savefig(output_path, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(figure)


def _bar_chart(
    output_path,
    title,
    ylabel,
    models,
    values,
    *,
    percent=False,
    errors=None,
    better=None,
):
    figure, axis = plt.subplots(figsize=(12, 7.2), layout="constrained")
    positions = list(range(len(models)))
    bars = axis.bar(
        positions,
        values,
        width=0.62,
        color=[COLORS[model] for model in models],
        yerr=errors,
        capsize=6 if errors is not None else 0,
        error_kw={"elinewidth": 1.5, "capthick": 1.5},
    )

    axis.set_title(title, fontsize=18, fontweight="bold", pad=24)
    if better:
        axis.text(
            0.5,
            1.01,
            better,
            transform=axis.transAxes,
            ha="center",
            va="bottom",
            color="#555555",
            fontsize=10,
        )
    axis.set_ylabel(ylabel, fontsize=11)
    axis.set_xticks(
        positions,
        [
            "Average rating\nbaseline" if model == "Average rating baseline" else model
            for model in models
        ],
    )
    axis.grid(axis="y", linestyle="--", alpha=0.35)
    axis.set_axisbelow(True)
    axis.spines[["top", "right"]].set_visible(False)
    axis.set_ylim(bottom=0)
    if percent:
        axis.yaxis.set_major_formatter(PercentFormatter(xmax=1.0, decimals=0))

    labels = [f"{value:.1%}" if percent else f"{value:.3f}" for value in values]
    axis.bar_label(bars, labels=labels, padding=7, fontsize=9, fontweight="bold")
    _save_figure(figure, output_path)


def _confusion_chart(output_path, pointwise):
    metrics = ["TP", "FP", "TN", "FN"]
    metric_colors = {"TP": "#2E8B57", "FP": "#F28E2B", "TN": "#4E79A7", "FN": "#E15759"}
    values_by_metric = {
        metric: [int(pointwise[model][metric]) for model in ALL_MODELS]
        for metric in metrics
    }
    figure, axis = plt.subplots(figsize=(13, 7.2), layout="constrained")
    positions = list(range(len(ALL_MODELS)))
    width = 0.19
    offsets = [-1.5 * width, -0.5 * width, 0.5 * width, 1.5 * width]

    for metric, offset in zip(metrics, offsets):
        bars = axis.bar(
            [position + offset for position in positions],
            values_by_metric[metric],
            width=width,
            label=metric,
            color=metric_colors[metric],
        )
        axis.bar_label(bars, padding=3, fontsize=7, rotation=90)

    axis.set_title(
        "Pointwise Classification Counts", fontsize=18, fontweight="bold", pad=24
    )
    axis.text(
        0.5,
        1.01,
        "TP and TN are correct decisions; FP and FN are errors",
        transform=axis.transAxes,
        ha="center",
        va="bottom",
        color="#555555",
        fontsize=10,
    )
    axis.set_ylabel("Warm test ratings")
    axis.set_xticks(
        positions,
        [
            "Average rating\nbaseline" if model == "Average rating baseline" else model
            for model in ALL_MODELS
        ],
    )
    axis.set_ylim(bottom=0)
    axis.grid(axis="y", linestyle="--", alpha=0.35)
    axis.set_axisbelow(True)
    axis.spines[["top", "right"]].set_visible(False)
    axis.legend(ncols=4, frameon=False, loc="upper center")
    _save_figure(figure, output_path)


def generate_graphs(
    inference_results=DEFAULT_INFERENCE_RESULTS,
    training_results=DEFAULT_TRAINING_RESULTS,
    model_dir=DEFAULT_MODEL_DIR,
    output_dir=DEFAULT_OUTPUT_DIR,
):
    inference_results = Path(inference_results)
    training_results = Path(training_results)
    model_dir = Path(model_dir)
    output_dir = Path(output_dir)
    data = _parse_results(inference_results, training_results)
    output_dir.mkdir(parents=True, exist_ok=True)

    point_metrics = [
        ("MAE", "pointwise_mae.png", "Mean Absolute Error", "Rating error", False, "Lower is better"),
        ("RMSE", "pointwise_rmse.png", "Root Mean Squared Error", "Rating error", False, "Lower is better"),
        ("Precision", "pointwise_precision.png", "Pointwise Precision", "Precision", True, "Higher is better"),
        ("Recall", "pointwise_recall.png", "Pointwise Recall", "Recall", True, "Higher is better"),
        ("FPR", "pointwise_fpr.png", "False-Positive Rate", "False-positive rate", True, "Lower is better"),
        ("FNR", "pointwise_fnr.png", "False-Negative Rate", "False-negative rate", True, "Lower is better"),
    ]
    generated = []
    for column, filename, title, ylabel, percent, better in point_metrics:
        _bar_chart(
            output_dir / filename,
            title,
            ylabel,
            ALL_MODELS,
            [float(data["pointwise"][model][column]) for model in ALL_MODELS],
            percent=percent,
            better=better,
        )
        generated.append(filename)

    confusion_filename = "pointwise_confusion_counts.png"
    _confusion_chart(output_dir / confusion_filename, data["pointwise"])
    generated.append(confusion_filename)

    for column, filename, title in (
        ("Precision@20", "native_precision_at_20.png", "Native Recommendation Precision@20"),
        ("Recall@20", "native_recall_at_20.png", "Native Recommendation Recall@20"),
        ("Hit Rate@20", "native_hit_rate_at_20.png", "Native Recommendation Hit Rate@20"),
        ("NDCG@20", "native_ndcg_at_20.png", "Native Recommendation NDCG@20"),
    ):
        _bar_chart(
            output_dir / filename,
            title,
            column,
            ALL_MODELS,
            [float(data["native"][model][column]) for model in ALL_MODELS],
            percent=True,
            better="Higher is better",
        )
        generated.append(filename)

    training_std = _timing_standard_deviations(
        data["training_runs"], "Train (seconds)"
    )
    training_filename = "average_training_time.png"
    _bar_chart(
        output_dir / training_filename,
        "Average Training Time",
        "Seconds",
        PERSISTED_MODELS,
        [float(data["training_averages"][model]["Train (seconds)"]) for model in PERSISTED_MODELS],
        errors=[training_std[model] for model in PERSISTED_MODELS],
        better="Lower is better; error bars show ±1 standard deviation",
    )
    generated.append(training_filename)

    throughput_std = _timing_standard_deviations(
        data["throughput_runs"], "Seconds", multiplier=1000
    )
    throughput_filename = "average_top20_request_time.png"
    _bar_chart(
        output_dir / throughput_filename,
        "Average Top-20 Recommendation Time",
        "Milliseconds per request",
        PERSISTED_MODELS,
        [float(data["throughput_summary"][model]["Average (ms)"]) for model in PERSISTED_MODELS],
        errors=[throughput_std[model] for model in PERSISTED_MODELS],
        better="Lower is better; error bars show ±1 standard deviation",
    )
    generated.append(throughput_filename)

    sizes = {}
    for model, filename in ARTIFACTS.items():
        path = model_dir / filename
        if not path.is_file():
            raise FileNotFoundError(f"Missing model artifact: {path}")
        sizes[model] = path.stat().st_size / (1024 * 1024)
    size_filename = "model_artifact_size.png"
    _bar_chart(
        output_dir / size_filename,
        "Serialized Model Artifact Size",
        "MiB",
        PERSISTED_MODELS,
        [sizes[model] for model in PERSISTED_MODELS],
        better="Smaller requires less storage; this is not runtime memory",
    )
    generated.append(size_filename)

    gallery = [
        "# Model comparison graphs",
        "",
        f"- Inference source: {data['inference_title']}",
        f"- Training source: {data['training_title']}",
        f"- Throughput source: {data['throughput_title']}",
        "- Charts use a zero baseline and consistent model colors.",
        "",
        "## Pointwise warm-rating quality",
        "",
    ]
    for filename, caption in (
        ("pointwise_mae.png", "MAE"),
        ("pointwise_rmse.png", "RMSE"),
        ("pointwise_precision.png", "Precision"),
        ("pointwise_recall.png", "Recall"),
        ("pointwise_fpr.png", "False-positive rate"),
        ("pointwise_fnr.png", "False-negative rate"),
        ("pointwise_confusion_counts.png", "Confusion counts"),
    ):
        gallery.extend([f"### {caption}", "", f"![{caption}]({filename})", ""])

    gallery.extend(["## Native top-20 recommendation quality", ""])
    for filename, caption in (
        ("native_precision_at_20.png", "Precision@20"),
        ("native_recall_at_20.png", "Recall@20"),
        ("native_hit_rate_at_20.png", "Hit Rate@20"),
        ("native_ndcg_at_20.png", "NDCG@20"),
    ):
        gallery.extend([f"### {caption}", "", f"![{caption}]({filename})", ""])

    gallery.extend(["## Operational characteristics", ""])
    for filename, caption in (
        ("average_training_time.png", "Average training time"),
        ("average_top20_request_time.png", "Average top-20 request time"),
        ("model_artifact_size.png", "Model artifact size"),
    ):
        gallery.extend([f"### {caption}", "", f"![{caption}]({filename})", ""])
    gallery.extend(
        [
            "The average-rating baseline is omitted from operational and size charts because it is computed in memory and has no persisted model artifact.",
            "",
            "Artifact size measures serialized files on disk, not peak runtime memory.",
            "",
        ]
    )
    (output_dir / "README.md").write_text("\n".join(gallery), encoding="utf-8")
    print(f"Generated {len(generated)} charts and gallery at {output_dir}")
    return generated


def main():
    parser = argparse.ArgumentParser(description="Generate model comparison graphs")
    parser.add_argument(
        "--inference-results", type=Path, default=DEFAULT_INFERENCE_RESULTS
    )
    parser.add_argument(
        "--training-results", type=Path, default=DEFAULT_TRAINING_RESULTS
    )
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    generate_graphs(
        inference_results=args.inference_results,
        training_results=args.training_results,
        model_dir=args.model_dir,
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    main()
