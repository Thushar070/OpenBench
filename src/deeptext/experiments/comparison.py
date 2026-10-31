"""Audit and compare the saved IMDb baseline and TextCNN artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import sklearn
import torch
from sklearn.pipeline import Pipeline
from threadpoolctl import threadpool_limits
from torch.utils.data import DataLoader

from deeptext.data.dataset import load_imdb_split, split_training_data
from deeptext.data.torch_dataset import TextReviewDataset
from deeptext.evaluation.comparison import (
    available_history_metrics,
    benchmark_batch_throughput,
    benchmark_single_review_latency,
    compare_error_categories,
    evaluate_comparison_model,
    required_artifact_sizes,
)
from deeptext.evaluation.metrics import evaluate_binary_predictions
from deeptext.inference.predictor import (
    TextCNNSentimentPredictor,
    load_textcnn_predictor,
)
from deeptext.models.baseline import (
    load_baseline_artifacts,
    predict_baseline,
    predict_baseline_probabilities,
)
from deeptext.preprocessing.encoding import encode_text
from deeptext.training.checkpoint import load_textcnn_checkpoint

SEED = 42
VALIDATION_SIZE = 0.2
BENCHMARK_SAMPLE_COUNT = 100
BENCHMARK_REPETITIONS = 3
BENCHMARK_BATCH_SIZE = 32
METRIC_NAMES = ("accuracy", "precision", "recall", "f1")


def _discover_latest_run(root: Path, name_prefix: str) -> Path:
    candidates: list[tuple[str, Path]] = []
    for directory in root.glob(f"{name_prefix}*"):
        metadata_path = directory / "metadata.json"
        if not directory.is_dir() or not metadata_path.is_file():
            continue
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        created_at = metadata.get("created_at_utc")
        if isinstance(created_at, str):
            candidates.append((created_at, directory))
    if not candidates:
        raise FileNotFoundError(f"No completed {name_prefix} artifact run in {root}.")
    return max(candidates, key=lambda item: item[0])[1]


def _fingerprint(records: pd.DataFrame) -> str:
    digest = hashlib.sha256()
    for review_id, label, text in records[["review_id", "label", "text"]].itertuples(
        index=False, name=None
    ):
        text_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        digest.update(f"{review_id}\0{int(label)}\0{text_digest}\n".encode("utf-8"))
    return digest.hexdigest()


def _metrics_match(actual: dict[str, Any], recorded: dict[str, Any]) -> bool:
    return all(
        np.isclose(float(actual[name]), float(recorded[name]), rtol=0, atol=1e-12)
        for name in METRIC_NAMES
    )


def _validate_saved_split(
    metadata: dict[str, Any],
    model_name: str,
    training_count: int,
    validation_count: int,
) -> None:
    split = metadata.get("split", {})
    if (
        split.get("source") != "official labeled training split only"
        or split.get("random_state", metadata.get("seed")) != SEED
        or split.get("validation_fraction") != VALIDATION_SIZE
        or split.get("training_rows") != training_count
        or split.get("validation_rows") != validation_count
        or metadata.get("official_test_evaluated") is not False
        or metadata.get("unlabeled_reviews_used") is not False
    ):
        raise ValueError(f"{model_name} metadata does not match the audited split.")


def _predict_textcnn_batch(
    predictor: TextCNNSentimentPredictor, reviews: Sequence[str]
) -> list[int]:
    encoded = [
        encode_text(
            review,
            predictor.vocabulary,
            max_length=predictor.model.config.max_sequence_length,
        )
        for review in reviews
    ]
    input_ids = torch.tensor(
        [row[0] for row in encoded], dtype=torch.long, device=predictor.device
    )
    lengths = torch.tensor(
        [row[1] for row in encoded], dtype=torch.long, device=predictor.device
    )
    with torch.inference_mode():
        predictions = predictor.model(input_ids, lengths).argmax(dim=1)
    return predictions.cpu().tolist()


def _error_category(
    true_label: int, baseline_prediction: int, textcnn_prediction: int
) -> str:
    baseline_correct = baseline_prediction == true_label
    textcnn_correct = textcnn_prediction == true_label
    if baseline_correct and textcnn_correct:
        return "both_correct"
    if baseline_correct:
        return "baseline_only_correct"
    if textcnn_correct:
        return "textcnn_only_correct"
    return "both_wrong_same_prediction"


def _confusion_plot(matrix: list[list[int]], title: str, destination: Path) -> None:
    figure, axis = plt.subplots(figsize=(5.2, 4.3))
    image = axis.imshow(matrix, cmap="Blues")
    axis.set(
        xticks=[0, 1],
        yticks=[0, 1],
        xticklabels=["Negative", "Positive"],
        yticklabels=["Negative", "Positive"],
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
    figure.savefig(destination, dpi=150)
    plt.close(figure)


def _save_plots(
    output_directory: Path,
    metrics: dict[str, dict[str, Any]],
    benchmarks: dict[str, Any],
    history: list[dict[str, Any]],
    best_epoch: int,
) -> list[str]:
    plot_names: list[str] = []
    model_labels = {
        "logistic_regression": "TF-IDF + Logistic Regression",
        "textcnn": "TextCNN",
    }

    figure, axis = plt.subplots(figsize=(7, 4.5))
    positions = np.arange(len(METRIC_NAMES))
    width = 0.36
    for offset, model_key in enumerate(model_labels):
        values = [100 * metrics[model_key][name] for name in METRIC_NAMES]
        axis.bar(
            positions + (offset - 0.5) * width,
            values,
            width,
            label=model_labels[model_key],
        )
    axis.set(
        xticks=positions,
        xticklabels=[name.capitalize() for name in METRIC_NAMES],
        ylabel="Validation score (%)",
        ylim=(0, 100),
        title="Classification metrics on the shared validation set",
    )
    axis.legend()
    axis.grid(axis="y", alpha=0.25)
    figure.tight_layout()
    figure.savefig(output_directory / "classification_metrics.png", dpi=150)
    plt.close(figure)
    plot_names.append("classification_metrics.png")

    _confusion_plot(
        metrics["logistic_regression"]["confusion_matrix"],
        model_labels["logistic_regression"],
        output_directory / "confusion_matrix_logistic_regression.png",
    )
    _confusion_plot(
        metrics["textcnn"]["confusion_matrix"],
        model_labels["textcnn"],
        output_directory / "confusion_matrix_textcnn.png",
    )
    plot_names.extend(
        ["confusion_matrix_logistic_regression.png", "confusion_matrix_textcnn.png"]
    )

    figure, axis = plt.subplots(figsize=(6.5, 4.3))
    positions = np.arange(2)
    width = 0.35
    for offset, model_key in enumerate(model_labels):
        counts = [
            metrics[model_key]["false_positive"],
            metrics[model_key]["false_negative"],
        ]
        axis.bar(
            positions + (offset - 0.5) * width,
            counts,
            width,
            label=model_labels[model_key],
        )
    axis.set(
        xticks=positions,
        xticklabels=["False positives", "False negatives"],
        ylabel="Validation reviews",
        title="Validation error counts",
    )
    axis.legend()
    axis.grid(axis="y", alpha=0.25)
    figure.tight_layout()
    figure.savefig(output_directory / "false_positive_negative.png", dpi=150)
    plt.close(figure)
    plot_names.append("false_positive_negative.png")

    cpu_latency = benchmarks["single_review_latency_cpu"]
    figure, axis = plt.subplots(figsize=(6.5, 4.3))
    latency_labels = [
        "TF-IDF + Logistic Regression (CPU)",
        "TextCNN (CPU)",
    ]
    latency_values = [
        cpu_latency["logistic_regression"]["median_latency_ms"],
        cpu_latency["textcnn"]["median_latency_ms"],
    ]
    gpu_latency = benchmarks.get("single_review_latency_gpu", {}).get("textcnn")
    if gpu_latency is not None:
        latency_labels.append("TextCNN (CUDA)")
        latency_values.append(gpu_latency["median_latency_ms"])
    axis.bar(
        latency_labels,
        latency_values,
    )
    axis.set(
        ylabel="Median end-to-end latency (ms/review)",
        title="Single-review latency by model and device",
    )
    axis.grid(axis="y", alpha=0.25)
    figure.tight_layout()
    figure.savefig(output_directory / "inference_latency_by_device.png", dpi=150)
    plt.close(figure)
    plot_names.append("inference_latency_by_device.png")

    throughput_rows = [
        (
            "TF-IDF + Logistic Regression (CPU)",
            benchmarks["batch_throughput_cpu"]["logistic_regression"][
                "samples_per_second"
            ],
        ),
        (
            "TextCNN (CPU)",
            benchmarks["batch_throughput_cpu"]["textcnn"]["samples_per_second"],
        ),
    ]
    gpu_throughput = benchmarks.get("batch_throughput", {}).get("textcnn_gpu")
    if gpu_throughput is not None:
        throughput_rows.append(
            (
                "TextCNN (CUDA)",
                gpu_throughput["samples_per_second"],
            )
        )
    figure, axis = plt.subplots(figsize=(7, 4.5))
    axis.bar([row[0] for row in throughput_rows], [row[1] for row in throughput_rows])
    axis.set(ylabel="Samples per second", title="End-to-end batch inference throughput")
    axis.tick_params(axis="x", rotation=18)
    axis.grid(axis="y", alpha=0.25)
    figure.tight_layout()
    figure.savefig(output_directory / "batch_throughput.png", dpi=150)
    plt.close(figure)
    plot_names.append("batch_throughput.png")

    recorded_metrics = available_history_metrics(history)
    plotted_groups = [
        ("training_loss", "validation_loss", "Loss", "Cross-entropy"),
        ("training_accuracy", "validation_accuracy", "Accuracy", "Accuracy"),
    ]
    plotted_groups = [
        group
        for group in plotted_groups
        if group[0] in recorded_metrics or group[1] in recorded_metrics
    ]
    if "training_f1" in recorded_metrics or "validation_f1" in recorded_metrics:
        plotted_groups.append(("training_f1", "validation_f1", "F1", "F1 score"))
    if plotted_groups:
        figure, axes = plt.subplots(
            len(plotted_groups),
            1,
            figsize=(7.5, 3.2 * len(plotted_groups)),
            squeeze=False,
        )
        epochs = [row["epoch"] for row in history]
        for axis, (training_key, validation_key, title, ylabel) in zip(
            axes.flat, plotted_groups, strict=True
        ):
            for key, label in (
                (training_key, "Training"),
                (validation_key, "Validation"),
            ):
                if key in recorded_metrics:
                    axis.plot(
                        epochs, [row[key] for row in history], marker="o", label=label
                    )
            axis.axvline(
                best_epoch,
                color="tab:red",
                linestyle="--",
                label=f"Best epoch {best_epoch}",
            )
            axis.set(title=f"TextCNN {title} by epoch", xlabel="Epoch", ylabel=ylabel)
            axis.legend()
            axis.grid(alpha=0.25)
        figure.tight_layout()
        figure.savefig(output_directory / "textcnn_learning_curves.png", dpi=150)
        plt.close(figure)
        plot_names.append("textcnn_learning_curves.png")
    return plot_names


def run_comparison_experiment(
    dataset_directory: str | Path = Path("datasets/aclImdb"),
    baseline_run_directory: str | Path | None = None,
    textcnn_run_directory: str | Path | None = None,
    output_root: str | Path = Path("outputs/comparison"),
) -> dict[str, Any]:
    """Reproduce validation predictions and write comparison data and plots."""
    project_root = Path.cwd().resolve()
    baseline_run = (
        Path(baseline_run_directory).expanduser().resolve()
        if baseline_run_directory is not None
        else _discover_latest_run(
            project_root / "outputs" / "baseline", "imdb-baseline-"
        )
    )
    textcnn_run = (
        Path(textcnn_run_directory).expanduser().resolve()
        if textcnn_run_directory is not None
        else _discover_latest_run(project_root / "outputs" / "textcnn", "imdb-textcnn-")
    )

    baseline_model, baseline_metadata = load_baseline_artifacts(baseline_run)
    if not isinstance(baseline_model, Pipeline):
        raise TypeError(
            "The selected baseline artifact must be a scikit-learn Pipeline."
        )
    original_training = load_imdb_split(dataset_directory, "train")
    training, validation = split_training_data(
        original_training, validation_size=VALIDATION_SIZE, seed=SEED
    )
    training_count, validation_count = len(training), len(validation)
    _validate_saved_split(
        baseline_metadata, "Baseline", training_count, validation_count
    )

    cnn_metadata = json.loads(
        (textcnn_run / "metadata.json").read_text(encoding="utf-8")
    )
    _validate_saved_split(cnn_metadata, "TextCNN", training_count, validation_count)
    if cnn_metadata.get("vocabulary_fit_partition") != "training":
        raise ValueError("TextCNN vocabulary was not recorded as training-only.")

    training_texts = training["text"].tolist()
    validation_texts = validation["text"].tolist()
    labels = validation["label"].to_numpy(dtype=np.int64)
    row_ids = validation["review_id"].tolist()
    overlap_texts = set(training_texts).intersection(validation_texts)
    duplicate_flags = np.asarray([text in overlap_texts for text in validation_texts])
    baseline_predictions = np.asarray(
        predict_baseline(baseline_model, validation_texts), dtype=np.int64
    )
    baseline_probabilities = predict_baseline_probabilities(
        baseline_model, validation_texts
    )[:, 1]

    metric_device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    textcnn_model, vocabulary, _ = load_textcnn_checkpoint(textcnn_run, metric_device)
    if textcnn_model.config.vocab_size != vocabulary.size:
        raise ValueError("TextCNN checkpoint and saved vocabulary sizes differ.")
    if textcnn_model.config.padding_idx != vocabulary.pad_id:
        raise ValueError("TextCNN checkpoint and saved vocabulary padding IDs differ.")
    textcnn_dataset = TextReviewDataset(
        validation_texts,
        labels,
        vocabulary,
        max_length=textcnn_model.config.max_sequence_length,
    )
    textcnn_loader = DataLoader(
        textcnn_dataset,
        batch_size=64,
        shuffle=False,
        num_workers=0,
        pin_memory=metric_device.type == "cuda",
    )
    textcnn_predictions: list[int] = []
    textcnn_probabilities: list[float] = []
    textcnn_model.eval()
    with torch.inference_mode():
        for batch in textcnn_loader:
            logits = textcnn_model(
                batch["input_ids"].to(metric_device, non_blocking=True),
                batch["length"].to(metric_device, non_blocking=True),
            )
            probabilities = torch.softmax(logits, dim=1)
            textcnn_predictions.extend(probabilities.argmax(dim=1).cpu().tolist())
            textcnn_probabilities.extend(probabilities[:, 1].cpu().tolist())
    textcnn_predictions_array = np.asarray(textcnn_predictions, dtype=np.int64)
    textcnn_probabilities_array = np.asarray(textcnn_probabilities, dtype=np.float64)

    metrics = {
        "logistic_regression": evaluate_comparison_model(
            labels, baseline_predictions, baseline_probabilities
        ),
        "textcnn": evaluate_comparison_model(
            labels, textcnn_predictions_array, textcnn_probabilities_array
        ),
    }
    recorded_baseline_metrics = baseline_metadata["validation_metrics"]
    recorded_textcnn_metrics = cnn_metadata["best_validation_metrics"]
    reproducibility = {
        "logistic_regression_matches_recorded_metrics": _metrics_match(
            metrics["logistic_regression"], recorded_baseline_metrics
        ),
        "textcnn_matches_recorded_metrics": _metrics_match(
            metrics["textcnn"], recorded_textcnn_metrics
        ),
        "baseline_artifact_reload_predictions_equivalent": baseline_metadata.get(
            "artifact_reload_predictions_equivalent", False
        ),
        "textcnn_checkpoint_reload_verified": cnn_metadata.get(
            "checkpoint_reload_verified", False
        ),
    }
    if not all(reproducibility.values()):
        raise RuntimeError(
            "Reproduced predictions or metrics did not match saved artifacts."
        )

    category_counts = compare_error_categories(
        labels, baseline_predictions, textcnn_predictions_array
    )
    baseline_duplicate_metrics = None
    textcnn_duplicate_metrics = None
    if duplicate_flags.any():
        baseline_duplicate_metrics = evaluate_binary_predictions(
            labels[duplicate_flags], baseline_predictions[duplicate_flags]
        )
        textcnn_duplicate_metrics = evaluate_binary_predictions(
            labels[duplicate_flags], textcnn_predictions_array[duplicate_flags]
        )
    nonduplicate_flags = ~duplicate_flags
    subgroup_metrics = {
        "duplicate_validation_rows": {
            "sample_count": int(duplicate_flags.sum()),
            "unique_exact_texts_shared": len(overlap_texts),
            "logistic_regression": baseline_duplicate_metrics,
            "textcnn": textcnn_duplicate_metrics,
        },
        "nonduplicate_validation_rows": {
            "sample_count": int(nonduplicate_flags.sum()),
            "logistic_regression": (
                evaluate_binary_predictions(
                    labels[nonduplicate_flags], baseline_predictions[nonduplicate_flags]
                )
                if nonduplicate_flags.any()
                else None
            ),
            "textcnn": (
                evaluate_binary_predictions(
                    labels[nonduplicate_flags],
                    textcnn_predictions_array[nonduplicate_flags],
                )
                if nonduplicate_flags.any()
                else None
            ),
        },
    }

    sample_count = min(BENCHMARK_SAMPLE_COUNT, validation_count)
    benchmark_reviews = validation_texts[:sample_count]

    def baseline_cpu_predictor(text: str) -> int:
        return predict_baseline(baseline_model, [text])[0]

    textcnn_cpu_predictor = load_textcnn_predictor(textcnn_run, device="cpu")
    torch.set_num_threads(1)
    benchmark_device = "CPU (single PyTorch thread; scikit-learn/BLAS thread limit 1)"
    with threadpool_limits(limits=1):
        latency_cpu = {
            "logistic_regression": benchmark_single_review_latency(
                baseline_cpu_predictor,
                benchmark_reviews,
                warmup_count=5,
                repetitions=BENCHMARK_REPETITIONS,
                device=benchmark_device,
            ),
            "textcnn": benchmark_single_review_latency(
                textcnn_cpu_predictor.predict,
                benchmark_reviews,
                warmup_count=5,
                repetitions=BENCHMARK_REPETITIONS,
                device=benchmark_device,
            ),
        }

        def baseline_batch(reviews: Sequence[str]) -> list[int]:
            return predict_baseline(baseline_model, reviews)

        throughput_cpu = {
            "logistic_regression": benchmark_batch_throughput(
                baseline_batch,
                benchmark_reviews,
                batch_size=BENCHMARK_BATCH_SIZE,
                warmup_count=1,
                repetitions=BENCHMARK_REPETITIONS,
                device=benchmark_device,
            ),
            "textcnn": benchmark_batch_throughput(
                lambda reviews: _predict_textcnn_batch(textcnn_cpu_predictor, reviews),
                benchmark_reviews,
                batch_size=BENCHMARK_BATCH_SIZE,
                warmup_count=1,
                repetitions=BENCHMARK_REPETITIONS,
                device=benchmark_device,
            ),
        }

    benchmarks: dict[str, Any] = {
        "procedure": {
            "validation_sample": "first reviews in deterministic validation order",
            "sample_count": sample_count,
            "single_review_warmups_per_model": 5,
            "single_review_timed_repetitions": BENCHMARK_REPETITIONS,
            "single_review_statistic": (
                "mean and median over each review across repetitions"
            ),
            "batch_size": BENCHMARK_BATCH_SIZE,
            "batch_warmup_batches": 1,
            "batch_timed_repetitions": BENCHMARK_REPETITIONS,
            "timing_boundary": (
                "raw review input through each model's existing preprocessing "
                "and prediction; "
                "model loading excluded"
            ),
            "cpu_thread_configuration": benchmark_device,
            "cuda_synchronization": (
                "torch.cuda.synchronize before and after timed calls"
            ),
        },
        "single_review_latency_cpu": latency_cpu,
        "batch_throughput_cpu": throughput_cpu,
        "batch_throughput": {
            "logistic_regression_cpu": throughput_cpu["logistic_regression"],
            "textcnn_cpu": throughput_cpu["textcnn"],
        },
        "prediction_generation_device": str(metric_device),
    }
    if torch.cuda.is_available():
        textcnn_gpu_predictor = load_textcnn_predictor(textcnn_run, device="cuda")

        def synchronize_cuda() -> None:
            torch.cuda.synchronize(textcnn_gpu_predictor.device)

        benchmarks["single_review_latency_gpu"] = {
            "textcnn": benchmark_single_review_latency(
                textcnn_gpu_predictor.predict,
                benchmark_reviews,
                warmup_count=5,
                repetitions=BENCHMARK_REPETITIONS,
                device=torch.cuda.get_device_name(textcnn_gpu_predictor.device),
                synchronize=synchronize_cuda,
            )
        }
        benchmarks["batch_throughput"]["textcnn_gpu"] = benchmark_batch_throughput(
            lambda reviews: _predict_textcnn_batch(textcnn_gpu_predictor, reviews),
            benchmark_reviews,
            batch_size=BENCHMARK_BATCH_SIZE,
            warmup_count=1,
            repetitions=BENCHMARK_REPETITIONS,
            device=torch.cuda.get_device_name(textcnn_gpu_predictor.device),
            synchronize=synchronize_cuda,
        )
    else:
        benchmarks["single_review_latency_gpu"] = {"textcnn": None}
        benchmarks["batch_throughput"]["textcnn_gpu"] = None

    baseline_required_sizes = required_artifact_sizes(
        {
            "baseline.joblib": baseline_run / "baseline.joblib",
            "metadata.json": baseline_run / "metadata.json",
        }
    )
    textcnn_required_sizes = required_artifact_sizes(
        {
            "best_model.pt": textcnn_run / "best_model.pt",
            "vocabulary.json": textcnn_run / "vocabulary.json",
            "metadata.json": textcnn_run / "metadata.json",
        }
    )
    history = json.loads((textcnn_run / "history.json").read_text(encoding="utf-8"))[
        "epochs"
    ]
    history_metrics = available_history_metrics(history)
    output_directory_root = Path(output_root).expanduser()
    created_at = datetime.now(UTC)
    comparison_id = f"imdb-comparison-{created_at:%Y%m%dT%H%M%S.%fZ}"
    output_directory = output_directory_root / comparison_id
    output_directory.mkdir(parents=True, exist_ok=False)

    validation_predictions = pd.DataFrame(
        {
            "review_id": row_ids,
            "true_label": labels,
            "baseline_prediction": baseline_predictions,
            "baseline_positive_probability": baseline_probabilities,
            "textcnn_prediction": textcnn_predictions_array,
            "textcnn_positive_probability": textcnn_probabilities_array,
            "exact_text_seen_in_training": duplicate_flags,
        }
    )
    predictions_path = output_directory / "validation_predictions.csv"
    validation_predictions.to_csv(predictions_path, index=False, lineterminator="\n")

    error_examples: list[dict[str, Any]] = []
    for index, row in validation.iterrows():
        baseline_prediction = int(baseline_predictions[index])
        textcnn_prediction = int(textcnn_predictions_array[index])
        category = _error_category(
            int(labels[index]), baseline_prediction, textcnn_prediction
        )
        if category == "both_correct":
            continue
        error_examples.append(
            {
                "review_id": str(row["review_id"]),
                "text_snippet": str(row["text"])[:300],
                "true_label": int(labels[index]),
                "baseline_prediction": baseline_prediction,
                "baseline_positive_probability": float(baseline_probabilities[index]),
                "textcnn_prediction": textcnn_prediction,
                "textcnn_positive_probability": float(
                    textcnn_probabilities_array[index]
                ),
                "error_category": category,
                "exact_text_seen_in_training": bool(duplicate_flags[index]),
            }
        )
        if len(error_examples) >= 12:
            break

    split_audit = {
        "dataset_source": "aclImdb labeled training split only",
        "baseline_split_parameters": {
            "validation_fraction": baseline_metadata["split"]["validation_fraction"],
            "seed": baseline_metadata["split"]["random_state"],
            "stratified": True,
        },
        "textcnn_split_parameters": {
            "validation_fraction": cnn_metadata["split"]["validation_fraction"],
            "seed": cnn_metadata["split"]["random_state"],
            "stratified": True,
        },
        "training_count": training_count,
        "validation_count": validation_count,
        "training_review_fingerprint_sha256": _fingerprint(training),
        "validation_review_fingerprint_sha256": _fingerprint(validation),
        "same_split_implementation_and_parameters": True,
        "historical_sample_ids_were_saved": False,
        "identity_evidence": (
            "Both experiment runners call load_imdb_split(dataset, 'train') "
            "followed by "
            "split_training_data with validation_size=0.2 and seed=42; this audit "
            "reconstructs that deterministic split and both saved metric sets "
            "reproduce exactly. "
            "Original artifacts do not contain row IDs or a split fingerprint."
        ),
        "exact_duplicate_overlap": {
            "unique_texts_shared_between_training_and_validation": len(overlap_texts),
            "validation_rows_matching_training_text": int(duplicate_flags.sum()),
        },
        "duplicate_subgroup_metrics": subgroup_metrics,
    }
    comparison_results: dict[str, Any] = {
        "comparison_id": comparison_id,
        "created_at_utc": created_at.isoformat(),
        "dataset": "aclImdb v1.0",
        "validation_only": True,
        "official_test_evaluated": False,
        "unlabeled_reviews_used": False,
        "sample_count": validation_count,
        "label_mapping": {"negative": 0, "positive": 1},
        "metric_settings": {
            "positive_class": 1,
            "confusion_matrix_labels": [0, 1],
            "confusion_matrix_layout": (
                "[[true_negative, false_positive], [false_negative, true_positive]]"
            ),
            "zero_division": 0,
        },
        "artifacts": {
            "baseline_run": str(baseline_run.relative_to(project_root)),
            "textcnn_run": str(textcnn_run.relative_to(project_root)),
            "validation_predictions": predictions_path.name,
        },
        "software_versions": {
            "python": platform.python_version(),
            "scikit_learn": sklearn.__version__,
            "numpy": np.__version__,
            "pandas": pd.__version__,
            "torch": str(torch.__version__),
        },
        "split_audit": split_audit,
        "historical_metric_reproduction": {
            "reproduced": reproducibility,
            "recorded_training_times_seconds": {
                "logistic_regression": baseline_metadata["training_duration_seconds"],
                "textcnn": cnn_metadata["training_duration_seconds"],
            },
            "training_times_directly_comparable": False,
            "training_time_limitation": (
                "Historical timings use different scopes: baseline pipeline "
                "fitting versus TextCNN multi-epoch training plus validation "
                "and checkpoint selection. "
                "Neither model was retrained for this comparison."
            ),
            "recorded_baseline_inference_latency": baseline_metadata.get(
                "inference_latency"
            ),
            "historical_latency_directly_comparable": False,
            "latency_limitation": (
                "The baseline history used one pass and one repetition; the new "
                "benchmark uses the same sample, multiple repetitions, median "
                "and mean, and end-to-end timing."
            ),
        },
        "metrics": metrics,
        "error_analysis": {
            "categories": category_counts,
            "selected_error_examples": error_examples,
        },
        "benchmarks": benchmarks,
        "artifact_sizes": {
            "measurement_boundary": (
                "Files required by the existing artifact loaders; excludes the "
                "separate "
                "TextCNN training history and all comparison outputs."
            ),
            "logistic_regression": baseline_required_sizes,
            "textcnn": textcnn_required_sizes,
            "historical_baseline_model_file_only_bytes": baseline_metadata.get(
                "model_artifact_size_bytes"
            ),
            "historical_textcnn_checkpoint_only_bytes": (textcnn_run / "best_model.pt")
            .stat()
            .st_size,
        },
        "textcnn_learning_history": {
            "epoch_count": len(history),
            "best_epoch": cnn_metadata["best_epoch"],
            "available_metrics": list(history_metrics),
            "history_file": str(
                (textcnn_run / "history.json").relative_to(project_root)
            ),
        },
    }
    plot_names = _save_plots(
        output_directory,
        metrics,
        benchmarks,
        history,
        int(cnn_metadata["best_epoch"]),
    )
    comparison_results["plots"] = plot_names
    results_path = output_directory / "comparison.json"
    results_path.write_text(
        json.dumps(comparison_results, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return {
        "comparison_id": comparison_id,
        "comparison_directory": str(output_directory.resolve()),
        "comparison_results": str(results_path.resolve()),
        "metrics": metrics,
        "error_categories": category_counts,
        "split_audit": split_audit,
        "benchmarks": benchmarks,
        "artifact_sizes": comparison_results["artifact_sizes"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare saved IMDb baseline and TextCNN artifacts on validation data."
        )
    )
    parser.add_argument("--dataset-dir", type=Path, default=Path("datasets/aclImdb"))
    parser.add_argument("--baseline-run-dir", type=Path)
    parser.add_argument("--textcnn-run-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/comparison"))
    arguments = parser.parse_args()
    result = run_comparison_experiment(
        dataset_directory=arguments.dataset_dir,
        baseline_run_directory=arguments.baseline_run_dir,
        textcnn_run_directory=arguments.textcnn_run_dir,
        output_root=arguments.output_dir,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
