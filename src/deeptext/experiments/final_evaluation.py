"""Evaluate the frozen baseline and TextCNN on the official IMDb test split."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
from collections import defaultdict
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
from sklearn.metrics import classification_report

from deeptext.data.dataset import STANDARD_COUNTS, load_imdb_split, validate_records
from deeptext.evaluation.comparison import (
    compare_error_categories,
    evaluate_comparison_model,
)
from deeptext.evaluation.metrics import validate_binary_labels
from deeptext.inference.predictor import load_textcnn_predictor
from deeptext.models.baseline import (
    load_baseline_artifacts,
    predict_baseline,
    predict_baseline_probabilities,
)

BASELINE_RUN = Path("outputs/baseline/imdb-baseline-20261030T110055.976336Z")
TEXTCNN_RUN = Path("outputs/textcnn/imdb-textcnn-20261030T122328.015737Z")
DEFAULT_DATASET_DIRECTORY = Path("datasets/aclImdb")
DEFAULT_OUTPUT_ROOT = Path("outputs/final_evaluation")
TEXTCNN_BATCH_SIZE = 128
MAX_EXAMPLE_SNIPPET_LENGTH = 400
EXAMPLES_PER_ERROR_CATEGORY = 5
METRIC_NAMES = ("accuracy", "precision", "recall", "f1")
MODEL_NAMES = {
    "logistic_regression": "TF-IDF + Logistic Regression",
    "textcnn": "TextCNN",
}
EXPECTED_TEST_ROOT_FILES = {"labeledBow.feat", "urls_neg.txt", "urls_pos.txt"}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_hashes(run_directories: tuple[Path, ...]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for run_directory in run_directories:
        for path in sorted(run_directory.rglob("*")):
            if path.is_file():
                key = path.relative_to(run_directory.parent.parent).as_posix()
                hashes[key] = _sha256_file(path)
    return hashes


def _validate_test_records(
    records: pd.DataFrame,
    dataset_directory: str | Path,
    expected_class_counts: dict[int, int] | None = None,
) -> dict[str, Any]:
    """Validate source IDs, labels, order population, and standard counts."""
    required_columns = {"text", "label", "split", "review_id"}
    missing = required_columns.difference(records.columns)
    if missing:
        raise ValueError(f"Test records are missing columns: {sorted(missing)}")
    validate_records(records)
    labels = validate_binary_labels(records["label"].to_numpy(), "test labels")
    if not records["split"].eq("test").all():
        raise ValueError(
            "Every evaluation record must come from the official test split."
        )

    identifiers = records["review_id"].tolist()
    if not all(isinstance(value, str) and value for value in identifiers):
        raise ValueError("Every test record must have a non-empty review identifier.")
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("The official test split contains duplicate review IDs.")

    class_counts = {0: 0, 1: 0}
    for review_id, label in zip(identifiers, labels, strict=True):
        if review_id.startswith("test/neg/"):
            source_label = 0
        elif review_id.startswith("test/pos/"):
            source_label = 1
        else:
            raise ValueError(
                f"Test record is not identified under test/neg or test/pos: {review_id}"
            )
        if int(label) != source_label:
            raise ValueError(f"Label does not match test directory for {review_id}.")
        class_counts[source_label] += 1

    expected_counts = expected_class_counts or {
        0: STANDARD_COUNTS["test"]["neg"],
        1: STANDARD_COUNTS["test"]["pos"],
    }
    if class_counts != expected_counts:
        raise ValueError(
            "Official test class counts do not match the expected dataset: "
            f"expected {expected_counts}, found {class_counts}."
        )

    root = Path(dataset_directory).expanduser()
    test_directory = root / "test"
    if not test_directory.is_dir():
        raise FileNotFoundError(f"Official test directory not found: {test_directory}")
    root_entries = list(test_directory.iterdir())
    unexpected_entries = sorted(
        entry.name
        for entry in root_entries
        if (
            (entry.is_dir() and entry.name not in {"neg", "pos"})
            or (entry.is_file() and entry.name not in EXPECTED_TEST_ROOT_FILES)
        )
    )
    if unexpected_entries:
        raise ValueError(
            "Unexpected entries in the official test directory: "
            + ", ".join(unexpected_entries)
        )
    if not {"neg", "pos"}.issubset(
        {entry.name for entry in root_entries if entry.is_dir()}
    ):
        raise ValueError(
            "The official test directory must contain neg and pos folders."
        )

    return {
        "sample_count": len(records),
        "class_distribution": {
            "negative": class_counts[0],
            "positive": class_counts[1],
        },
        "review_ids_unique": True,
        "record_source_verified": True,
        "root_auxiliary_files_not_loaded": sorted(
            entry.name for entry in root_entries if entry.is_file()
        ),
        "review_id_order_sha256": hashlib.sha256(
            "\n".join(identifiers).encode("utf-8")
        ).hexdigest(),
    }


def _validate_prediction_alignment(
    records: pd.DataFrame,
    baseline_predictions: np.ndarray,
    textcnn_predictions: np.ndarray,
) -> None:
    labels = validate_binary_labels(records["label"].to_numpy(), "test labels")
    baseline = validate_binary_labels(baseline_predictions, "baseline predictions")
    textcnn = validate_binary_labels(textcnn_predictions, "TextCNN predictions")
    if len(labels) != len(baseline) or len(labels) != len(textcnn):
        raise ValueError(
            "Both models must predict every official test record exactly once."
        )


def _validation_test_rows(
    validation_metrics: dict[str, dict[str, Any]],
    test_metrics: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = []
    for metric in METRIC_NAMES:
        for model_key, model_name in MODEL_NAMES.items():
            validation_value = float(validation_metrics[model_key][metric])
            test_value = float(test_metrics[model_key][metric])
            if not np.isfinite([validation_value, test_value]).all():
                raise ValueError(f"Non-finite {metric} value cannot be compared.")
            rows.append(
                {
                    "metric": metric,
                    "model": model_name,
                    "validation": validation_value,
                    "test": test_value,
                    "difference_percentage_points": 100
                    * (validation_value - test_value),
                }
            )
    return rows


def _error_analysis(
    records: pd.DataFrame,
    labels: np.ndarray,
    baseline_predictions: np.ndarray,
    baseline_probabilities: np.ndarray,
    textcnn_predictions: np.ndarray,
    textcnn_probabilities: np.ndarray,
) -> tuple[dict[str, Any], pd.DataFrame, np.ndarray]:
    categories = compare_error_categories(
        labels, baseline_predictions, textcnn_predictions
    )
    baseline_correct = baseline_predictions == labels
    textcnn_correct = textcnn_predictions == labels
    both_wrong = ~baseline_correct & ~textcnn_correct
    tags_by_index: dict[int, set[str]] = defaultdict(set)

    masks = {
        "logistic_false_positive": (labels == 0) & (baseline_predictions == 1),
        "logistic_false_negative": (labels == 1) & (baseline_predictions == 0),
        "textcnn_false_positive": (labels == 0) & (textcnn_predictions == 1),
        "textcnn_false_negative": (labels == 1) & (textcnn_predictions == 0),
        "both_misclassified": both_wrong,
        "logistic_only_correct": baseline_correct & ~textcnn_correct,
        "textcnn_only_correct": ~baseline_correct & textcnn_correct,
        "both_wrong_same_prediction": both_wrong
        & (baseline_predictions == textcnn_predictions),
        "predictions_differ": baseline_predictions != textcnn_predictions,
    }
    selected_per_category: dict[str, int] = {}
    for tag, mask in masks.items():
        indices = np.flatnonzero(mask)
        selected_per_category[tag] = min(len(indices), EXAMPLES_PER_ERROR_CATEGORY)
        if len(indices) > EXAMPLES_PER_ERROR_CATEGORY:
            positions = np.linspace(
                0, len(indices) - 1, EXAMPLES_PER_ERROR_CATEGORY, dtype=int
            )
            indices = indices[positions]
        for index in indices:
            tags_by_index[int(index)].add(tag)

    joint_categories = np.full(len(labels), "both_correct", dtype=object)
    joint_categories[baseline_correct & ~textcnn_correct] = "logistic_only_correct"
    joint_categories[~baseline_correct & textcnn_correct] = "textcnn_only_correct"
    joint_categories[both_wrong] = "both_wrong_same_prediction"

    examples = []
    for index in sorted(tags_by_index):
        row = records.iloc[index]
        label = int(labels[index])
        examples.append(
            {
                "review_id": str(row["review_id"]),
                "true_label": label,
                "true_sentiment": "positive" if label == 1 else "negative",
                "logistic_regression_prediction": int(baseline_predictions[index]),
                "logistic_regression_positive_probability": float(
                    baseline_probabilities[index]
                ),
                "textcnn_prediction": int(textcnn_predictions[index]),
                "textcnn_positive_probability": float(textcnn_probabilities[index]),
                "error_category": str(joint_categories[index]),
                "error_tags": ";".join(sorted(tags_by_index[index])),
                "review_text_snippet": str(row["text"])[:MAX_EXAMPLE_SNIPPET_LENGTH],
            }
        )

    summary = {
        "counts": {
            **categories,
            "logistic_regression_false_positives": int(
                masks["logistic_false_positive"].sum()
            ),
            "logistic_regression_false_negatives": int(
                masks["logistic_false_negative"].sum()
            ),
            "textcnn_false_positives": int(masks["textcnn_false_positive"].sum()),
            "textcnn_false_negatives": int(masks["textcnn_false_negative"].sum()),
            "both_misclassified": int(both_wrong.sum()),
        },
        "example_selection": {
            "method": "up to five evenly spaced rows per category in loader order",
            "maximum_snippet_characters": MAX_EXAMPLE_SNIPPET_LENGTH,
            "selected_rows_per_category": selected_per_category,
            "unique_selected_rows": len(examples),
        },
    }
    return summary, pd.DataFrame(examples), joint_categories


def _save_confusion_matrix(
    matrix: list[list[int]], title: str, destination: Path
) -> None:
    figure, axis = plt.subplots(figsize=(5.4, 4.5))
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
    test_metrics: dict[str, dict[str, Any]],
    validation_rows: list[dict[str, Any]],
    error_counts: dict[str, int],
) -> list[str]:
    plot_names = []
    metric_positions = np.arange(len(METRIC_NAMES))
    bar_width = 0.36

    figure, axis = plt.subplots(figsize=(8.5, 5))
    for offset, model_key in enumerate(MODEL_NAMES):
        values = [100 * test_metrics[model_key][name] for name in METRIC_NAMES]
        axis.bar(
            metric_positions + (offset - 0.5) * bar_width,
            values,
            bar_width,
            label=MODEL_NAMES[model_key],
        )
    axis.set(
        xticks=metric_positions,
        xticklabels=[name.capitalize() for name in METRIC_NAMES],
        ylabel="Test score (%)",
        ylim=(0, 100),
        title="Classification metrics on the official IMDb test split",
    )
    axis.legend()
    axis.grid(axis="y", alpha=0.25)
    figure.tight_layout()
    figure.savefig(output_directory / "classification_metrics.png", dpi=150)
    plt.close(figure)
    plot_names.append("classification_metrics.png")

    _save_confusion_matrix(
        test_metrics["logistic_regression"]["confusion_matrix"],
        "Logistic Regression — IMDb test",
        output_directory / "confusion_matrix_logistic_regression.png",
    )
    _save_confusion_matrix(
        test_metrics["textcnn"]["confusion_matrix"],
        "TextCNN — IMDb test",
        output_directory / "confusion_matrix_textcnn.png",
    )
    plot_names.extend(
        ["confusion_matrix_logistic_regression.png", "confusion_matrix_textcnn.png"]
    )

    validation_values = {
        (row["model"], row["metric"]): row["validation"] for row in validation_rows
    }
    test_values = {
        (row["model"], row["metric"]): row["test"] for row in validation_rows
    }
    figure, axis = plt.subplots(figsize=(10, 5.5))
    series = [
        ("TF-IDF + Logistic Regression", "validation", "Baseline validation"),
        ("TF-IDF + Logistic Regression", "test", "Baseline test"),
        ("TextCNN", "validation", "TextCNN validation"),
        ("TextCNN", "test", "TextCNN test"),
    ]
    width = 0.19
    for series_index, (model_name, split_name, label) in enumerate(series):
        mapping = validation_values if split_name == "validation" else test_values
        axis.bar(
            metric_positions + (series_index - 1.5) * width,
            [100 * mapping[(model_name, metric)] for metric in METRIC_NAMES],
            width,
            label=label,
        )
    axis.set(
        xticks=metric_positions,
        xticklabels=[name.capitalize() for name in METRIC_NAMES],
        ylabel="Score (%)",
        ylim=(0, 100),
        title="Validation and official test metrics",
    )
    axis.legend(ncols=2)
    axis.grid(axis="y", alpha=0.25)
    figure.tight_layout()
    figure.savefig(output_directory / "validation_vs_test_comparison.png", dpi=150)
    plt.close(figure)
    plot_names.append("validation_vs_test_comparison.png")

    gap_rows = [row for row in validation_rows]
    figure, axis = plt.subplots(figsize=(8.5, 5))
    for offset, model_name in enumerate(MODEL_NAMES.values()):
        values = [
            next(
                row["difference_percentage_points"]
                for row in gap_rows
                if row["model"] == model_name and row["metric"] == metric
            )
            for metric in METRIC_NAMES
        ]
        axis.bar(
            metric_positions + (offset - 0.5) * bar_width,
            values,
            bar_width,
            label=model_name,
        )
    axis.axhline(0, color="black", linewidth=0.8)
    axis.set(
        xticks=metric_positions,
        xticklabels=[name.capitalize() for name in METRIC_NAMES],
        ylabel="Validation − test (percentage points)",
        title="Observed validation-to-test difference",
    )
    axis.legend()
    axis.grid(axis="y", alpha=0.25)
    figure.tight_layout()
    figure.savefig(output_directory / "generalization_gap.png", dpi=150)
    plt.close(figure)
    plot_names.append("generalization_gap.png")

    figure, axis = plt.subplots(figsize=(7.5, 4.8))
    error_positions = np.arange(2)
    for offset, model_key in enumerate(MODEL_NAMES):
        metrics = test_metrics[model_key]
        axis.bar(
            error_positions + (offset - 0.5) * bar_width,
            [metrics["false_positive"], metrics["false_negative"]],
            bar_width,
            label=MODEL_NAMES[model_key],
        )
    axis.set(
        xticks=error_positions,
        xticklabels=["False positives", "False negatives"],
        ylabel="Misclassified reviews",
        title="False-positive and false-negative counts",
    )
    axis.legend()
    axis.grid(axis="y", alpha=0.25)
    figure.tight_layout()
    figure.savefig(output_directory / "false_positive_negative.png", dpi=150)
    plt.close(figure)
    plot_names.append("false_positive_negative.png")

    agreement_names = [
        ("both_correct", "Both correct"),
        ("baseline_only_correct", "Logistic Regression only correct"),
        ("textcnn_only_correct", "TextCNN only correct"),
        ("both_wrong_same_prediction", "Both wrong"),
    ]
    figure, axis = plt.subplots(figsize=(9, 5))
    axis.bar(
        [label for _, label in agreement_names],
        [error_counts[key] for key, _ in agreement_names],
        color=["#4C956C", "#6C91BF", "#D99B41", "#B35C5C"],
    )
    axis.set(
        ylabel="Test reviews",
        title="Paired prediction agreement and errors",
    )
    axis.tick_params(axis="x", rotation=16)
    axis.grid(axis="y", alpha=0.25)
    figure.tight_layout()
    figure.savefig(output_directory / "prediction_agreement.png", dpi=150)
    plt.close(figure)
    plot_names.append("prediction_agreement.png")
    return plot_names


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def run_final_evaluation(
    dataset_directory: str | Path = DEFAULT_DATASET_DIRECTORY,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
) -> dict[str, Any]:
    """Evaluate the existing saved models once on all official test reviews."""
    project_root = Path.cwd().resolve()
    dataset_path = Path(dataset_directory).expanduser()
    if not dataset_path.is_absolute():
        dataset_path = project_root / dataset_path
    dataset_path = dataset_path.resolve()
    baseline_run = (project_root / BASELINE_RUN).resolve()
    textcnn_run = (project_root / TEXTCNN_RUN).resolve()
    run_directories = (baseline_run, textcnn_run)

    original_hashes = _artifact_hashes(run_directories)
    baseline_model, baseline_metadata = load_baseline_artifacts(baseline_run)
    if (
        baseline_metadata.get("model_name") != "TF-IDF + Logistic Regression"
        or baseline_metadata.get("official_test_evaluated") is not False
        or baseline_metadata.get("unlabeled_reviews_used") is not False
        or baseline_metadata.get("selected_settings")
        != {
            "c": 1.0,
            "max_iter": 1000,
            "ngram_range": [1, 2],
            "seed": 42,
        }
    ):
        raise ValueError("The saved baseline metadata does not match the frozen run.")

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    textcnn_predictor = load_textcnn_predictor(textcnn_run, device=device)
    textcnn_metadata = textcnn_predictor.metadata
    if (
        textcnn_metadata.get("model_name") != "TextCNN"
        or textcnn_metadata.get("official_test_evaluated") is not False
        or textcnn_metadata.get("unlabeled_reviews_used") is not False
        or textcnn_metadata.get("vocabulary_fit_partition") != "training"
        or textcnn_metadata.get("checkpoint_reload_verified") is not True
        or textcnn_metadata.get("best_epoch") != 2
    ):
        raise ValueError("The saved TextCNN metadata does not match the frozen run.")

    records = load_imdb_split(dataset_path, "test")
    dataset_validation = _validate_test_records(records, dataset_path)
    texts = records["text"].tolist()
    labels = records["label"].to_numpy(dtype=np.int64)

    baseline_predictions = np.asarray(predict_baseline(baseline_model, texts))
    baseline_probabilities = predict_baseline_probabilities(baseline_model, texts)[:, 1]
    textcnn_rows = textcnn_predictor.predict_batch(texts, batch_size=TEXTCNN_BATCH_SIZE)
    textcnn_predictions = np.asarray(
        [row["predicted_class"] for row in textcnn_rows], dtype=np.int64
    )
    textcnn_probabilities = np.asarray(
        [row["positive_probability"] for row in textcnn_rows], dtype=np.float64
    )
    _validate_prediction_alignment(records, baseline_predictions, textcnn_predictions)
    if len(baseline_probabilities) != len(records) or len(textcnn_probabilities) != len(
        records
    ):
        raise ValueError(
            "Both models must provide one class-1 probability per test row."
        )

    test_metrics = {
        "logistic_regression": evaluate_comparison_model(
            labels, baseline_predictions, baseline_probabilities
        ),
        "textcnn": evaluate_comparison_model(
            labels, textcnn_predictions, textcnn_probabilities
        ),
    }
    validation_metrics = {
        "logistic_regression": baseline_metadata["validation_metrics"],
        "textcnn": textcnn_metadata["best_validation_metrics"],
    }
    validation_rows = _validation_test_rows(validation_metrics, test_metrics)
    reports = {
        model_key: classification_report(
            labels,
            predictions,
            labels=[0, 1],
            target_names=["negative", "positive"],
            output_dict=True,
            zero_division=0,
        )
        for model_key, predictions in (
            ("logistic_regression", baseline_predictions),
            ("textcnn", textcnn_predictions),
        )
    }
    error_summary, error_examples, joint_categories = _error_analysis(
        records,
        labels,
        baseline_predictions,
        baseline_probabilities,
        textcnn_predictions,
        textcnn_probabilities,
    )

    timestamp = datetime.now(UTC)
    evaluation_id = f"imdb-final-evaluation-{timestamp:%Y%m%dT%H%M%S.%fZ}"
    output_directory = Path(output_root).expanduser()
    if not output_directory.is_absolute():
        output_directory = project_root / output_directory
    output_directory = output_directory / evaluation_id
    output_directory.mkdir(parents=True, exist_ok=False)

    predictions_frame = pd.DataFrame(
        {
            "review_id": records["review_id"].tolist(),
            "true_label": labels,
            "logistic_regression_prediction": baseline_predictions,
            "logistic_regression_positive_probability": baseline_probabilities,
            "textcnn_prediction": textcnn_predictions,
            "textcnn_positive_probability": textcnn_probabilities,
            "joint_error_category": joint_categories,
            "predictions_differ": baseline_predictions != textcnn_predictions,
        }
    )
    if predictions_frame["review_id"].tolist() != records["review_id"].tolist():
        raise RuntimeError(
            "Saved prediction order no longer matches test record order."
        )
    predictions_frame.to_csv(
        output_directory / "test_predictions.csv", index=False, lineterminator="\n"
    )
    error_examples.to_csv(
        output_directory / "error_analysis.csv", index=False, lineterminator="\n"
    )
    _write_json(
        output_directory / "classification_report.json",
        {
            "positive_class": 1,
            "zero_division": 0,
            "models": reports,
        },
    )

    plot_names = _save_plots(
        output_directory,
        test_metrics,
        validation_rows,
        error_summary["counts"],
    )
    final_results = {
        "evaluation_id": evaluation_id,
        "dataset": "aclImdb v1.0",
        "split": "official test",
        "sample_count": len(records),
        "class_distribution": dataset_validation["class_distribution"],
        "label_mapping": {"negative": 0, "positive": 1},
        "metric_settings": {
            "positive_class": 1,
            "negative_class": 0,
            "confusion_matrix_labels": [0, 1],
            "confusion_matrix_order": "[[TN, FP], [FN, TP]]",
            "zero_division": 0,
        },
        "metrics": test_metrics,
        "validation_metrics_from_saved_metadata": validation_metrics,
        "validation_vs_test": validation_rows,
        "error_analysis": error_summary,
        "prediction_alignment": {
            "same_test_records": True,
            "same_original_order": True,
            "same_ground_truth_labels": True,
            "baseline_prediction_count": len(baseline_predictions),
            "textcnn_prediction_count": len(textcnn_predictions),
        },
        "artifacts": {
            "test_predictions": "test_predictions.csv",
            "classification_report": "classification_report.json",
            "error_analysis_examples": "error_analysis.csv",
            "plots": plot_names,
        },
        "official_test_evaluated": True,
        "unlabeled_reviews_used": False,
    }
    _write_json(output_directory / "final_results.json", final_results)

    final_hashes = _artifact_hashes(run_directories)
    if final_hashes != original_hashes:
        raise RuntimeError("A Phase 3 or Phase 5 artifact changed during evaluation.")
    metadata = {
        "evaluation_id": evaluation_id,
        "evaluation_timestamp_utc": timestamp.isoformat(),
        "dataset": "aclImdb v1.0",
        "dataset_directory": str(dataset_path),
        "dataset_split": "test",
        "dataset_validation": dataset_validation,
        "model_names": MODEL_NAMES,
        "artifact_paths": {
            "logistic_regression": str(baseline_run),
            "textcnn": str(textcnn_run),
        },
        "artifact_sha256_before": original_hashes,
        "artifact_sha256_after": final_hashes,
        "artifacts_unchanged": True,
        "model_configurations": {
            "logistic_regression": baseline_metadata["selected_settings"],
            "textcnn": textcnn_predictor.model.config.to_dict(),
        },
        "preprocessing_configuration": textcnn_metadata["preprocessing"],
        "label_mapping": {"negative": 0, "positive": 1},
        "metric_definitions": final_results["metric_settings"],
        "software_versions": {
            "python": platform.python_version(),
            "pytorch": str(torch.__version__),
            "scikit_learn": sklearn.__version__,
            "numpy": np.__version__,
            "pandas": pd.__version__,
        },
        "devices": {
            "logistic_regression": "cpu",
            "textcnn": str(device),
            "textcnn_device_name": (
                torch.cuda.get_device_name(device) if device.type == "cuda" else "CPU"
            ),
        },
        "evaluation_batch_size": {
            "logistic_regression": len(records),
            "textcnn": TEXTCNN_BATCH_SIZE,
        },
        "evaluation_methods": {
            "logistic_regression": (
                "saved fitted TF-IDF/Logistic Regression pipeline; "
                "one vectorized predict call"
            ),
            "textcnn": (
                "saved checkpoint and training vocabulary; ordered raw-text batches"
            ),
            "preprocessing_refitted": False,
            "model_training_started": False,
            "test_based_model_selection": False,
        },
        "historical_validation_metrics": validation_metrics,
        "validation_limitations": [
            "The original Phase 3 and Phase 5 metadata did not record "
            "validation row IDs or split fingerprints.",
            "Phase 6 reconstructed the shared split and reproduced both "
            "historical metric sets exactly, but historical row membership "
            "cannot be independently proven from those artifacts.",
            "The reconstructed validation split contained 26 unique exact-text "
            "duplicates shared with training across 28 validation rows; this "
            "may affect validation results, but does not establish the cause "
            "of any validation-to-test difference.",
        ],
        "evaluation_limitations": [
            "Validation-to-test differences are observed on these saved runs "
            "and are not a complete measure of model generalization.",
            "A single evaluation does not establish statistical significance "
            "or universal model superiority.",
            "Probabilities from the two model families are not assumed "
            "calibrated or directly comparable.",
            "The test split was used only for this final evaluation, not "
            "training, tuning, preprocessing fitting, or model selection.",
        ],
        "official_test_evaluated": True,
        "unlabeled_reviews_used": False,
        "training_started": False,
        "preprocessing_refitted": False,
        "test_based_model_selection": False,
    }
    _write_json(output_directory / "metadata.json", metadata)
    return {"output_directory": str(output_directory), **final_results}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate frozen IMDb models on the official test split."
    )
    parser.add_argument("--dataset-dir", type=Path, default=DEFAULT_DATASET_DIRECTORY)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    arguments = parser.parse_args()
    result = run_final_evaluation(arguments.dataset_dir, arguments.output_root)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
