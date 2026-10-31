"""Evaluate the five frozen IMDb models on the official test split."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import sklearn
import torch
from sklearn.metrics import classification_report
from threadpoolctl import threadpool_limits

from deeptext.data.dataset import load_imdb_split
from deeptext.evaluation.comparison import (
    benchmark_single_review_latency,
    evaluate_comparison_model,
    required_artifact_sizes,
)
from deeptext.experiments.final_evaluation import _validate_test_records
from deeptext.inference.predictor import (
    NeuralSentimentPredictor,
    load_recurrent_predictor,
    load_textcnn_predictor,
)
from deeptext.models.baseline import (
    load_baseline_artifacts,
    predict_baseline_probabilities,
)

DEFAULT_DATASET = Path("datasets/aclImdb")
DEFAULT_OUTPUT_ROOT = Path("outputs/final_evaluation")
BASELINE_RUN = Path("outputs/baseline/imdb-baseline-20261030T110055.976336Z")
TEXTCNN_RUN = Path("outputs/textcnn/imdb-textcnn-20261030T122328.015737Z")
PHASE7_RUN = Path(
    "outputs/final_evaluation/imdb-final-evaluation-20261031T075001.786903Z"
)
PHASE12_RUN = Path(
    "outputs/neural_comparison/imdb-neural-comparison-20261031T101806.410943Z"
)
MODEL_NAMES = {
    "logistic_regression": "TF-IDF + Logistic Regression",
    "textcnn": "TextCNN",
    "rnn": "Vanilla RNN",
    "lstm": "LSTM",
    "gru": "GRU",
}
RECURRENT_MODEL_NAMES = {"rnn": "VanillaRNN", "lstm": "LSTM", "gru": "GRU"}
MODEL_KEYS = tuple(MODEL_NAMES)
CLASS_NAMES = ("negative", "positive")
METRIC_NAMES = ("accuracy", "precision", "recall", "f1")
INFERENCE_BATCH_SIZE = 128
LATENCY_SAMPLE_COUNT = 100
LATENCY_REPETITIONS = 3
MAX_EXAMPLES_PER_MODEL_CATEGORY = 5
MAX_TEXT_CHARACTERS = 1_000
COLOURS = ("#12A76A", "#F28E2B", "#E45756", "#39A89B", "#C49A2A")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_hashes(run_directories: dict[str, Path]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for model_key, directory in run_directories.items():
        if not directory.is_dir():
            raise FileNotFoundError(f"Saved {model_key} run is missing: {directory}")
        files = [path for path in directory.rglob("*") if path.is_file()]
        if not files:
            raise FileNotFoundError(
                f"Saved {model_key} run contains no files: {directory}"
            )
        for path in sorted(files):
            relative = path.relative_to(directory).as_posix()
            hashes[f"{model_key}/{directory.name}/{relative}"] = _sha256(path)
    return hashes


def _latest_run(root: Path, prefix: str) -> Path:
    runs = []
    for directory in root.glob(f"{prefix}*"):
        metadata_path = directory / "metadata.json"
        if directory.is_dir() and metadata_path.is_file():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            created_at = metadata.get("created_at_utc")
            if isinstance(created_at, str):
                runs.append((created_at, directory))
    if not runs:
        raise FileNotFoundError(f"No saved experiment for {prefix} under {root}")
    return max(runs, key=lambda item: item[0])[1]


def _validate_frozen_metadata(
    model_key: str, metadata: dict[str, Any], model_name: str
) -> None:
    if (
        metadata.get("model_name") != model_name
        or metadata.get("official_test_evaluated") is not False
        or metadata.get("unlabeled_reviews_used") is not False
    ):
        raise ValueError(f"Frozen metadata for {model_key} is incompatible.")
    if (
        model_key != "logistic_regression"
        and metadata.get("vocabulary_fit_partition") != "training"
    ):
        raise ValueError(f"{model_key} vocabulary was not fitted on training data.")


def _run_paths(project_root: Path) -> dict[str, Path]:
    runs = {
        "logistic_regression": (project_root / BASELINE_RUN).resolve(),
        "textcnn": (project_root / TEXTCNN_RUN).resolve(),
    }
    for key in RECURRENT_MODEL_NAMES:
        runs[key] = _latest_run(
            project_root / "outputs" / "recurrent" / key, f"imdb-{key}-"
        )
    return runs


def _phase12_validation(project_root: Path, runs: dict[str, Path]) -> Path:
    comparison_path = (project_root / PHASE12_RUN / "comparison.json").resolve()
    if not comparison_path.is_file():
        raise FileNotFoundError(
            f"Phase 12 comparison report is missing: {comparison_path}"
        )
    comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
    if (
        comparison.get("official_test_evaluated") is not False
        or comparison.get("validation_only") is not True
        or set(comparison.get("models", [])) != set(MODEL_KEYS)
    ):
        raise ValueError("Phase 12 comparison is not the expected validation-only run.")
    source_artifacts = comparison["source_artifacts"]
    for key in ("logistic_regression", "textcnn", *RECURRENT_MODEL_NAMES):
        source_key = "baseline" if key == "logistic_regression" else key
        recorded = Path(source_artifacts[source_key]).resolve()
        if recorded != runs[key]:
            raise ValueError(f"Phase 12 comparison uses a different {key} run.")
    return comparison_path


def _validate_prediction_rows(
    records: pd.DataFrame, predictions: dict[str, pd.DataFrame]
) -> None:
    expected_ids = records["review_id"].tolist()
    expected_labels = records["label"].to_numpy(dtype=np.int64)
    for key, frame in predictions.items():
        if frame["review_id"].tolist() != expected_ids:
            raise ValueError(f"{key} predictions do not match official test row order.")
        if not np.array_equal(
            frame["true_label"].to_numpy(dtype=np.int64), expected_labels
        ):
            raise ValueError(f"{key} prediction labels are not aligned with test rows.")
        labels = frame["predicted_label"].to_numpy(dtype=np.int64)
        if not np.isin(labels, [0, 1]).all():
            raise ValueError(f"{key} predictions include a non-binary class.")
        probabilities = frame["positive_probability"].to_numpy(dtype=np.float64)
        if (
            not np.isfinite(probabilities).all()
            or ((probabilities < 0) | (probabilities > 1)).any()
        ):
            raise ValueError(f"{key} probabilities are invalid.")


def _latency_sample_indices(labels: np.ndarray, sample_count: int) -> np.ndarray:
    """Choose a deterministic class-balanced subset of official test rows."""
    per_class = max(1, sample_count // 2)
    selected: list[int] = []
    for label in (0, 1):
        indices = np.flatnonzero(labels == label)
        count = min(per_class, len(indices))
        if count:
            positions = np.linspace(0, len(indices) - 1, count, dtype=int)
            selected.extend(indices[positions].tolist())
    return np.asarray(sorted(selected[:sample_count]), dtype=np.int64)


def _prediction_frame(
    records: pd.DataFrame,
    predicted: np.ndarray,
    positive_probability: np.ndarray,
) -> pd.DataFrame:
    labels = records["label"].to_numpy(dtype=np.int64)
    confidence = np.maximum(positive_probability, 1.0 - positive_probability)
    return pd.DataFrame(
        {
            "review_id": records["review_id"].tolist(),
            "true_label": labels,
            "predicted_label": predicted.astype(np.int64),
            "negative_probability": 1.0 - positive_probability,
            "positive_probability": positive_probability,
            "confidence": confidence,
            "correct": predicted == labels,
        }
    )


def _infer_neural(
    predictor: NeuralSentimentPredictor,
    texts: list[str],
    *,
    device: torch.device,
    batch_size: int = INFERENCE_BATCH_SIZE,
) -> tuple[np.ndarray, np.ndarray, float]:
    synchronize = torch.cuda.synchronize if device.type == "cuda" else None
    warmup = predictor.predict_batch(
        texts[: min(batch_size, len(texts))], batch_size=batch_size
    )
    del warmup
    if synchronize is not None:
        synchronize()
    started = time.perf_counter()
    predictions: list[int] = []
    probabilities: list[float] = []
    for offset in range(0, len(texts), batch_size):
        rows = predictor.predict_batch(
            texts[offset : offset + batch_size], batch_size=batch_size
        )
        predictions.extend(int(row["predicted_class"]) for row in rows)
        probabilities.extend(float(row["positive_probability"]) for row in rows)
    if synchronize is not None:
        synchronize()
    elapsed = time.perf_counter() - started
    return np.asarray(predictions, dtype=np.int64), np.asarray(probabilities), elapsed


def _infer_baseline(
    model: Any,
    texts: list[str],
    batch_size: int = INFERENCE_BATCH_SIZE,
) -> tuple[np.ndarray, np.ndarray, float]:
    """Run one shared batch pass that produces both labels and probabilities."""
    first_batch = texts[:batch_size]
    if not first_batch:
        raise ValueError("The official test split must contain reviews.")
    predict_baseline_probabilities(model, first_batch)
    started = time.perf_counter()
    predicted: list[int] = []
    positive_probability: list[float] = []
    for offset in range(0, len(texts), batch_size):
        class_probabilities = predict_baseline_probabilities(
            model, texts[offset : offset + batch_size]
        )
        predicted.extend(np.argmax(class_probabilities, axis=1).tolist())
        positive_probability.extend(class_probabilities[:, 1].tolist())
    elapsed = time.perf_counter() - started
    return (
        np.asarray(predicted, dtype=np.int64),
        np.asarray(positive_probability, dtype=np.float64),
        elapsed,
    )


def _predict_baseline_label(model: Any, review: str) -> int:
    probabilities = predict_baseline_probabilities(model, [review])
    return int(np.argmax(probabilities[0]))


def _evaluate_metrics(
    labels: np.ndarray, predictions: dict[str, pd.DataFrame]
) -> tuple[dict[str, Any], dict[str, Any]]:
    metrics: dict[str, Any] = {}
    reports: dict[str, Any] = {}
    for key, frame in predictions.items():
        predicted = frame["predicted_label"].to_numpy(dtype=np.int64)
        positive_probability = frame["positive_probability"].to_numpy(dtype=np.float64)
        values = evaluate_comparison_model(labels, predicted, positive_probability)
        report = classification_report(
            labels,
            predicted,
            labels=[0, 1],
            target_names=list(CLASS_NAMES),
            output_dict=True,
            zero_division=0,
        )
        values["per_class"] = {
            name: {
                "precision": float(report[name]["precision"]),
                "recall": float(report[name]["recall"]),
                "f1": float(report[name]["f1-score"]),
                "support": int(report[name]["support"]),
            }
            for name in CLASS_NAMES
        }
        values["total_correct"] = int(np.count_nonzero(predicted == labels))
        values["total_incorrect"] = int(np.count_nonzero(predicted != labels))
        metrics[key] = values
        reports[key] = report
    return metrics, reports


def _pairwise_agreement(predictions: dict[str, pd.DataFrame]) -> dict[str, Any]:
    agreement = {}
    for left_index, left in enumerate(MODEL_KEYS):
        left_labels = predictions[left]["predicted_label"].to_numpy(dtype=np.int64)
        for right in MODEL_KEYS[left_index:]:
            right_labels = predictions[right]["predicted_label"].to_numpy(
                dtype=np.int64
            )
            count = int(np.count_nonzero(left_labels == right_labels))
            agreement[f"{left}_vs_{right}"] = {
                "agreement_count": count,
                "disagreement_count": len(left_labels) - count,
                "agreement_rate": count / len(left_labels),
            }
    return agreement


def _error_analysis(
    records: pd.DataFrame, predictions: dict[str, pd.DataFrame]
) -> tuple[pd.DataFrame, dict[str, Any]]:
    labels = records["label"].to_numpy(dtype=np.int64)
    model_predictions = {
        key: frame["predicted_label"].to_numpy(dtype=np.int64)
        for key, frame in predictions.items()
    }
    masks: dict[str, np.ndarray] = {}
    counts: dict[str, int] = {}
    tags_by_index: dict[int, set[str]] = defaultdict(set)
    selection_counts: dict[str, int] = {}
    for key in MODEL_KEYS:
        predicted = model_predictions[key]
        groups = {
            "false_positive": (labels == 0) & (predicted == 1),
            "false_negative": (labels == 1) & (predicted == 0),
            "correct_positive": (labels == 1) & (predicted == 1),
            "correct_negative": (labels == 0) & (predicted == 0),
        }
        for group_name, mask in groups.items():
            tag = f"{key}_{group_name}"
            masks[tag] = mask
            counts[tag] = int(mask.sum())
            indices = np.flatnonzero(mask)
            if len(indices) > MAX_EXAMPLES_PER_MODEL_CATEGORY:
                positions = np.linspace(
                    0, len(indices) - 1, MAX_EXAMPLES_PER_MODEL_CATEGORY, dtype=int
                )
                indices = indices[positions]
            selection_counts[tag] = len(indices)
            for index in indices:
                tags_by_index[int(index)].add(tag)

    prediction_matrix = np.column_stack([model_predictions[key] for key in MODEL_KEYS])
    correct_matrix = prediction_matrix == labels[:, None]
    joint_masks = {
        "all_five_misclassified": ~correct_matrix.any(axis=1),
        "all_five_correct": correct_matrix.all(axis=1),
        "models_disagree": np.any(
            prediction_matrix != prediction_matrix[:, :1], axis=1
        ),
    }
    for name, mask in joint_masks.items():
        counts[name] = int(mask.sum())
        indices = np.flatnonzero(mask)
        if len(indices) > MAX_EXAMPLES_PER_MODEL_CATEGORY:
            positions = np.linspace(
                0, len(indices) - 1, MAX_EXAMPLES_PER_MODEL_CATEGORY, dtype=int
            )
            indices = indices[positions]
        selection_counts[name] = len(indices)
        for index in indices:
            tags_by_index[int(index)].add(name)

    rows = []
    for index in sorted(tags_by_index):
        row = records.iloc[index]
        item: dict[str, Any] = {
            "review_id": str(row["review_id"]),
            "true_label": int(labels[index]),
            "true_sentiment": CLASS_NAMES[int(labels[index])],
            "analysis_tags": ";".join(sorted(tags_by_index[index])),
            "review_text": str(row["text"])[:MAX_TEXT_CHARACTERS],
        }
        for key in MODEL_KEYS:
            frame = predictions[key].iloc[index]
            item[f"{key}_prediction"] = int(frame["predicted_label"])
            item[f"{key}_positive_probability"] = float(frame["positive_probability"])
            item[f"{key}_confidence"] = float(frame["confidence"])
        rows.append(item)
    return pd.DataFrame(rows), {
        "counts": counts,
        "selection": {
            "method": (
                "up to five evenly spaced rows in original test loader order "
                "per category"
            ),
            "maximum_review_text_characters": MAX_TEXT_CHARACTERS,
            "selected_rows_per_category": selection_counts,
            "unique_selected_reviews": len(rows),
            "review_text_saved_only_for_selected_examples": True,
        },
    }


def _save_confusion_matrix(matrix: list[list[int]], title: str, path: Path) -> None:
    figure, axis = plt.subplots(figsize=(5.2, 4.4))
    image = axis.imshow(matrix, cmap="Greens")
    axis.set(
        xticks=[0, 1],
        yticks=[0, 1],
        xticklabels=CLASS_NAMES,
        yticklabels=CLASS_NAMES,
        xlabel="Predicted sentiment",
        ylabel="Actual sentiment",
        title=title,
    )
    axis.set_xticks(np.arange(-0.5, 2, 1), minor=True)
    axis.set_yticks(np.arange(-0.5, 2, 1), minor=True)
    axis.grid(which="minor", color="white", linewidth=2)
    for row in range(2):
        for column in range(2):
            axis.text(column, row, str(matrix[row][column]), ha="center", va="center")
    figure.colorbar(image, ax=axis, label="Reviews")
    figure.tight_layout()
    figure.savefig(path, dpi=150)
    plt.close(figure)


def _save_plots(
    output_directory: Path,
    metrics: dict[str, Any],
    benchmarks: dict[str, Any],
) -> list[str]:
    plot_directory = output_directory / "plots"
    plot_directory.mkdir(parents=True)
    positions = np.arange(len(MODEL_KEYS))
    names = [MODEL_NAMES[key] for key in MODEL_KEYS]
    plot_names = []
    for metric in ("accuracy", "f1"):
        figure, axis = plt.subplots(figsize=(9, 4.8))
        values = [100 * metrics[key][metric] for key in MODEL_KEYS]
        bars = axis.bar(positions, values, color=COLOURS)
        axis.bar_label(bars, fmt="%.2f%%", padding=3)
        axis.set(
            xticks=positions,
            xticklabels=names,
            ylabel="Test score (%)",
            ylim=(0, 100),
            title=f"Five-model {metric.capitalize()} comparison",
        )
        axis.tick_params(axis="x", rotation=12)
        axis.grid(axis="y", alpha=0.2)
        figure.tight_layout()
        path = f"{metric}_comparison.png"
        figure.savefig(plot_directory / path, dpi=150)
        plt.close(figure)
        plot_names.append(f"plots/{path}")

    figure, axis = plt.subplots(figsize=(9, 4.8))
    width = 0.36
    axis.bar(
        positions - width / 2,
        [100 * metrics[key]["precision"] for key in MODEL_KEYS],
        width,
        label="Precision",
        color="#12A76A",
    )
    axis.bar(
        positions + width / 2,
        [100 * metrics[key]["recall"] for key in MODEL_KEYS],
        width,
        label="Recall",
        color="#F28E2B",
    )
    axis.set(
        xticks=positions,
        xticklabels=names,
        ylabel="Test score (%)",
        ylim=(0, 100),
        title="Five-model precision and recall",
    )
    axis.tick_params(axis="x", rotation=12)
    axis.legend()
    axis.grid(axis="y", alpha=0.2)
    figure.tight_layout()
    figure.savefig(plot_directory / "precision_recall_comparison.png", dpi=150)
    plt.close(figure)
    plot_names.append("plots/precision_recall_comparison.png")

    for measure, title, filename, value_key in (
        (
            "single_review_latency",
            "Average single-review latency",
            "inference_latency_comparison.png",
            "mean_latency_ms",
        ),
        (
            "full_dataset_reviews_per_second",
            "Full test-split inference throughput",
            "inference_throughput_comparison.png",
            "reviews_per_second",
        ),
    ):
        figure, axis = plt.subplots(figsize=(9, 4.8))
        devices = ("cpu", "cuda")
        width = 0.36
        for offset, device in enumerate(devices):
            values = []
            for key in MODEL_KEYS:
                value = benchmarks[key].get(device)
                if measure == "single_review_latency":
                    values.append(
                        value["single_review_latency"][value_key] if value else 0
                    )
                else:
                    values.append(
                        value["full_dataset_reviews_per_second"] if value else 0
                    )
            color = "#E45756" if device == "cpu" else "#39A89B"
            axis.bar(
                positions + (offset - 0.5) * width,
                values,
                width,
                label=device.upper(),
                color=color,
            )
        axis.set(
            xticks=positions,
            xticklabels=names,
            ylabel="Milliseconds"
            if measure == "single_review_latency"
            else "Reviews / second",
            title=title,
        )
        axis.tick_params(axis="x", rotation=12)
        axis.legend()
        axis.grid(axis="y", alpha=0.2)
        figure.tight_layout()
        figure.savefig(plot_directory / filename, dpi=150)
        plt.close(figure)
        plot_names.append(f"plots/{filename}")
    return plot_names


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def run_phase13_evaluation(
    dataset_directory: str | Path = DEFAULT_DATASET,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
) -> dict[str, Any]:
    """Evaluate all saved models on all official test reviews without fitting."""
    project_root = Path.cwd().resolve()
    dataset_path = Path(dataset_directory).expanduser()
    if not dataset_path.is_absolute():
        dataset_path = project_root / dataset_path
    dataset_path = dataset_path.resolve()
    runs = _run_paths(project_root)
    phase12_path = _phase12_validation(project_root, runs)
    original_hashes = _artifact_hashes(runs)

    baseline, baseline_metadata = load_baseline_artifacts(runs["logistic_regression"])
    _validate_frozen_metadata(
        "logistic_regression", baseline_metadata, "TF-IDF + Logistic Regression"
    )
    if baseline_metadata.get("selected_settings") != {
        "c": 1.0,
        "max_iter": 1000,
        "ngram_range": [1, 2],
        "seed": 42,
    }:
        raise ValueError(
            "Saved baseline settings differ from the frozen Phase 7 model."
        )

    primary_device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    predictors: dict[str, NeuralSentimentPredictor] = {
        "textcnn": load_textcnn_predictor(runs["textcnn"], primary_device)
    }
    for key in RECURRENT_MODEL_NAMES:
        predictors[key] = load_recurrent_predictor(runs[key], primary_device)
    for key, predictor in predictors.items():
        _validate_frozen_metadata(
            key,
            predictor.metadata,
            "TextCNN" if key == "textcnn" else RECURRENT_MODEL_NAMES[key],
        )
        if key == "textcnn" and (
            predictor.metadata.get("checkpoint_reload_verified") is not True
            or predictor.metadata.get("best_epoch") != 2
        ):
            raise ValueError(
                "Saved TextCNN checkpoint metadata differs from the frozen Phase 7 run."
            )

    records = load_imdb_split(dataset_path, "test")
    dataset_validation = _validate_test_records(records, dataset_path)
    texts = records["text"].tolist()
    labels = records["label"].to_numpy(dtype=np.int64)
    predictions: dict[str, pd.DataFrame] = {}
    benchmarks: dict[str, Any] = {}

    with threadpool_limits(limits=1):
        baseline_predicted, baseline_probability, baseline_elapsed = _infer_baseline(
            baseline, texts
        )
    predictions["logistic_regression"] = _prediction_frame(
        records, baseline_predicted, baseline_probability
    )
    latency_indices = _latency_sample_indices(labels, LATENCY_SAMPLE_COUNT)
    latency_texts = [texts[int(index)] for index in latency_indices]
    with threadpool_limits(limits=1):
        baseline_latency = benchmark_single_review_latency(
            lambda text: _predict_baseline_label(baseline, text),
            latency_texts,
            warmup_count=5,
            repetitions=LATENCY_REPETITIONS,
            device="CPU (single BLAS thread)",
        )
    benchmarks["logistic_regression"] = {
        "cpu": {
            "device": "CPU (single BLAS thread)",
            "inference_method": (
                "One fitted-pipeline predict_proba call per batch; labels from "
                "class-probability argmax"
            ),
            "total_test_inference_seconds": baseline_elapsed,
            "full_dataset_reviews_per_second": len(texts) / baseline_elapsed,
            "full_dataset_average_batch_latency_ms": baseline_elapsed
            * 1000
            / max(1, (len(texts) + INFERENCE_BATCH_SIZE - 1) // INFERENCE_BATCH_SIZE),
            "batch_size": INFERENCE_BATCH_SIZE,
            "test_review_count": len(texts),
            "single_review_latency": baseline_latency,
            "model_loading_included": False,
        }
    }

    selected_neural_device_predictions: dict[str, np.ndarray] = {}
    neural_devices = ("cpu", "cuda") if torch.cuda.is_available() else ("cpu",)
    for key, predictor in predictors.items():
        benchmarks[key] = {}
        selected_output = None
        for device_name in neural_devices:
            device = torch.device(device_name)
            current_predictor = (
                predictor
                if device == primary_device
                else (
                    load_textcnn_predictor(runs[key], device)
                    if key == "textcnn"
                    else load_recurrent_predictor(runs[key], device)
                )
            )
            sample = latency_texts
            synchronize: Callable[[], None] | None = (
                torch.cuda.synchronize if device.type == "cuda" else None
            )
            previous_threads = torch.get_num_threads()
            try:
                if device.type == "cpu":
                    torch.set_num_threads(1)
                with threadpool_limits(limits=1):
                    device_predictions, device_probabilities, elapsed = _infer_neural(
                        current_predictor, texts, device=device
                    )
                    latency = benchmark_single_review_latency(
                        lambda text, p=current_predictor: p.predict(text),
                        sample,
                        warmup_count=5,
                        repetitions=LATENCY_REPETITIONS,
                        device=(
                            torch.cuda.get_device_name(device)
                            if device.type == "cuda"
                            else "CPU (single PyTorch/BLAS thread)"
                        ),
                        synchronize=synchronize,
                    )
            finally:
                if device.type == "cpu":
                    torch.set_num_threads(previous_threads)
            device_predictions_frame = _prediction_frame(
                records, device_predictions, device_probabilities
            )
            benchmarks[key][device_name] = {
                "device": torch.cuda.get_device_name(device)
                if device.type == "cuda"
                else "CPU",
                "total_test_inference_seconds": elapsed,
                "full_dataset_reviews_per_second": len(texts) / elapsed,
                "full_dataset_average_batch_latency_ms": elapsed
                * 1000
                / max(
                    1, (len(texts) + INFERENCE_BATCH_SIZE - 1) // INFERENCE_BATCH_SIZE
                ),
                "batch_size": INFERENCE_BATCH_SIZE,
                "test_review_count": len(texts),
                "warmup_batches": 1,
                "single_review_latency": latency,
                "model_loading_included": False,
            }
            if device == primary_device:
                selected_output = device_predictions_frame
                selected_neural_device_predictions[key] = device_predictions
            elif selected_output is not None:
                benchmarks[key][device_name][
                    "prediction_label_disagreements_vs_primary"
                ] = int(
                    np.count_nonzero(
                        device_predictions != selected_neural_device_predictions[key]
                    )
                )
            del current_predictor
        if selected_output is None:
            raise RuntimeError(f"No primary-device predictions generated for {key}.")
        predictions[key] = selected_output
        if primary_device.type == "cuda":
            for device_name in benchmarks[key]:
                if device_name != "cuda":
                    benchmarks[key][device_name][
                        "prediction_label_disagreements_vs_primary"
                    ] = int(
                        np.count_nonzero(
                            predictions[key]["predicted_label"].to_numpy(dtype=np.int64)
                            != selected_neural_device_predictions[key]
                        )
                    )

    _validate_prediction_rows(records, predictions)
    metrics, reports = _evaluate_metrics(labels, predictions)
    pairwise = _pairwise_agreement(predictions)
    prediction_matrix = np.column_stack(
        [
            predictions[key]["predicted_label"].to_numpy(dtype=np.int64)
            for key in MODEL_KEYS
        ]
    )
    correct_matrix = prediction_matrix == labels[:, None]
    joint_counts = {
        "all_five_correct": int(correct_matrix.all(axis=1).sum()),
        "all_five_misclassified": int((~correct_matrix.any(axis=1)).sum()),
        "models_disagree": int(
            np.any(prediction_matrix != prediction_matrix[:, :1], axis=1).sum()
        ),
    }

    phase7_path = (project_root / PHASE7_RUN / "final_results.json").resolve()
    phase7_metadata_path = phase7_path.with_name("metadata.json")
    if not phase7_path.is_file() or not phase7_metadata_path.is_file():
        raise FileNotFoundError(
            "The saved Phase 7 evaluation is required for consistency checks."
        )
    phase7 = json.loads(phase7_path.read_text(encoding="utf-8"))
    phase7_metadata = json.loads(phase7_metadata_path.read_text(encoding="utf-8"))
    if phase7.get("official_test_evaluated") is not True or phase7.get(
        "sample_count"
    ) != len(records):
        raise ValueError("Phase 7 results do not match the official test set.")
    historical_predictions = pd.read_csv(
        phase7_path.parent / phase7["artifacts"]["test_predictions"]
    )
    consistency = {}
    for key, phase7_key, historical_column in (
        (
            "logistic_regression",
            "logistic_regression",
            "logistic_regression_prediction",
        ),
        ("textcnn", "textcnn", "textcnn_prediction"),
    ):
        if historical_predictions["review_id"].tolist() != records[
            "review_id"
        ].tolist() or not np.array_equal(
            historical_predictions["true_label"].to_numpy(dtype=np.int64), labels
        ):
            raise ValueError(
                "Phase 7 prediction rows do not align with the official test set."
            )
        current = metrics[key]
        historical = phase7["metrics"][phase7_key]
        prediction_differences = int(
            np.count_nonzero(
                predictions[key]["predicted_label"].to_numpy(dtype=np.int64)
                != historical_predictions[historical_column].to_numpy(dtype=np.int64)
            )
        )
        metric_differences = {
            name: float(current[name]) - float(historical[name])
            for name in METRIC_NAMES
        }
        consistency[key] = {
            "historical_phase7_evaluation_id": phase7["evaluation_id"],
            "phase7_metrics": {name: historical[name] for name in METRIC_NAMES},
            "phase13_metrics": {name: current[name] for name in METRIC_NAMES},
            "metric_differences": metric_differences,
            "metrics_match_within_1e-12": all(
                abs(value) <= 1e-12 for value in metric_differences.values()
            ),
            "prediction_label_differences": prediction_differences,
            "phase7_artifacts_were_unchanged_after_phase7": phase7_metadata.get(
                "artifacts_unchanged"
            )
            is True,
        }

    artifact_sizes = {}
    for key, run in runs.items():
        if key == "logistic_regression":
            files = {
                "baseline.joblib": run / "baseline.joblib",
                "metadata.json": run / "metadata.json",
            }
        else:
            files = {
                "best_model.pt": run / "best_model.pt",
                "metadata.json": run / "metadata.json",
                "vocabulary.json": run / "vocabulary.json",
            }
        artifact_sizes[key] = required_artifact_sizes(files)
    model_info = {}
    for key, run in runs.items():
        if key == "logistic_regression":
            metadata = baseline_metadata
            params = None
            architecture = "TF-IDF + Logistic Regression"
        else:
            metadata = predictors[key].metadata
            params = sum(
                parameter.numel()
                for parameter in predictors[key].model.parameters()
                if parameter.requires_grad
            )
            architecture = "TextCNN" if key == "textcnn" else RECURRENT_MODEL_NAMES[key]
        model_info[key] = {
            "name": MODEL_NAMES[key],
            "architecture": architecture,
            "inference_devices": ["cpu"]
            if key == "logistic_regression"
            else list(neural_devices),
            "primary_prediction_device": "cpu"
            if key == "logistic_regression"
            else str(primary_device),
            "trainable_parameter_count": int(params) if params is not None else None,
            "artifact_size_bytes": artifact_sizes[key]["total_bytes"],
            "artifact_files": artifact_sizes[key]["files"],
            "artifact_size_measurement": (
                "Model loader files: fitted pipeline and metadata"
                if key == "logistic_regression"
                else "Model loader files: checkpoint, training vocabulary, and metadata"
            ),
            "artifact_directory": str(run),
            "model_configuration": metadata.get(
                "model_config", metadata.get("selected_settings")
            ),
            "training_duration_seconds_historical": metadata.get(
                "training_duration_seconds"
            ),
            "training_duration_is_phase13_measurement": False,
        }

    output_root_path = Path(output_root).expanduser()
    if not output_root_path.is_absolute():
        output_root_path = project_root / output_root_path
    output_root_path = output_root_path.resolve()
    if any(
        output_root_path == run or run in output_root_path.parents
        for run in runs.values()
    ):
        raise ValueError(
            "Evaluation output must not be inside a saved model artifact directory."
        )
    run_id = f"imdb-phase13-final-evaluation-{datetime.now(UTC):%Y%m%dT%H%M%S.%fZ}"
    output_directory = output_root_path / run_id
    output_directory.mkdir(parents=True, exist_ok=False)
    for directory in ("predictions", "confusion_matrices", "error_analysis"):
        (output_directory / directory).mkdir()

    for key, frame in predictions.items():
        frame.to_csv(
            output_directory / "predictions" / f"{key}.csv",
            index=False,
            lineterminator="\n",
        )
        matrix = metrics[key]["confusion_matrix"]
        _write_json(
            output_directory / "confusion_matrices" / f"{key}.json",
            {
                "model": key,
                "labels": [0, 1],
                "order": "[[TN, FP], [FN, TP]]",
                "matrix": matrix,
            },
        )
        _save_confusion_matrix(
            matrix,
            MODEL_NAMES[key],
            output_directory / "confusion_matrices" / f"{key}.png",
        )

    examples, error_summary = _error_analysis(records, predictions)
    examples.to_csv(
        output_directory / "error_analysis" / "selected_examples.csv",
        index=False,
        lineterminator="\n",
    )
    _write_json(output_directory / "error_analysis" / "summary.json", error_summary)
    _write_json(
        output_directory / "metrics.json",
        {
            "sample_count": len(records),
            "positive_class": 1,
            "zero_division": 0,
            "models": metrics,
            "classification_reports": reports,
        },
    )
    comparison = {
        "evaluation_id": run_id,
        "sample_count": len(records),
        "models": model_info,
        "metrics": metrics,
        "pairwise_prediction_agreement": pairwise,
        "joint_prediction_counts": joint_counts,
        "phase7_consistency": consistency,
    }
    _write_json(output_directory / "comparison.json", comparison)
    _write_json(output_directory / "benchmark.json", benchmarks)
    plots = _save_plots(output_directory, metrics, benchmarks)

    final_hashes = _artifact_hashes(runs)
    if final_hashes != original_hashes:
        raise RuntimeError(
            "One or more frozen model artifacts changed during evaluation."
        )
    phase7_hashes = phase7_metadata.get("artifact_sha256_after", {})
    phase7_hash_checks = {}
    for phase7_key, historical_hash in phase7_hashes.items():
        category, relative_path = phase7_key.split("/", 1)
        model_key = {
            "baseline": "logistic_regression",
            "textcnn": "textcnn",
        }.get(category)
        if model_key is not None:
            phase13_key = f"{model_key}/{relative_path}"
            phase7_hash_checks[phase7_key] = (
                final_hashes.get(phase13_key) == historical_hash
            )
    if not phase7_hash_checks or not all(phase7_hash_checks.values()):
        raise RuntimeError("Phase 7 baseline/TextCNN artifact hashes no longer match.")
    metadata = {
        "evaluation_id": run_id,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "dataset": "aclImdb v1.0",
        "dataset_split": "official test",
        "dataset_directory": str(dataset_path),
        "dataset_validation": dataset_validation,
        "sample_count": len(records),
        "model_names": MODEL_NAMES,
        "source_artifacts": {key: str(path) for key, path in runs.items()},
        "phase12_comparison": str(phase12_path),
        "phase7_historical_evaluation": str(phase7_path.parent),
        "artifact_sha256_before": original_hashes,
        "artifact_sha256_after": final_hashes,
        "artifacts_unchanged": original_hashes == final_hashes,
        "phase7_artifact_hashes_match": phase7_hash_checks,
        "model_training_started": False,
        "preprocessing_refitted": False,
        "checkpoint_selection_performed": False,
        "test_based_model_selection": False,
        "unlabeled_reviews_used": False,
        "official_test_evaluated": True,
        "primary_neural_inference_device": str(primary_device),
        "device_name": torch.cuda.get_device_name(primary_device)
        if primary_device.type == "cuda"
        else "CPU",
        "inference_batch_size": INFERENCE_BATCH_SIZE,
        "latency_sample_count": len(latency_indices),
        "latency_repetitions": LATENCY_REPETITIONS,
        "latency_warmup_reviews": 5,
        "test_review_id_order_sha256": dataset_validation["review_id_order_sha256"],
        "metric_definitions": {
            "positive_class": 1,
            "negative_class": 0,
            "confusion_matrix_labels": [0, 1],
            "confusion_matrix_order": "[[TN, FP], [FN, TP]]",
            "zero_division": 0,
        },
        "software_versions": {
            "python": platform.python_version(),
            "pytorch": str(torch.__version__),
            "scikit_learn": sklearn.__version__,
            "numpy": np.__version__,
            "pandas": pd.__version__,
        },
        "generated_plots": plots,
        "known_limitations": [
            (
                "These metrics describe the existing frozen model runs on one "
                "benchmark dataset and do not establish statistical significance "
                "or universal superiority."
            ),
            (
                "Model confidence values are uncalibrated scores and do not "
                "guarantee correctness."
            ),
            (
                "Historical model training durations are copied from metadata "
                "and were not measured during Phase 13."
            ),
        ],
    }
    _write_json(output_directory / "metadata.json", metadata)
    comparison["output_directory"] = str(output_directory)
    return {
        "output_directory": str(output_directory),
        "metrics": metrics,
        "comparison": comparison,
        "benchmarks": benchmarks,
        "metadata": metadata,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    args = parser.parse_args()
    result = run_phase13_evaluation(args.dataset_dir, args.output_root)
    print(
        json.dumps(
            {
                "output_directory": result["output_directory"],
                "metrics": result["metrics"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
