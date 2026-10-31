"""Shared metrics, error categories, and inference benchmarks for comparisons."""

from __future__ import annotations

import math
import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from deeptext.evaluation.metrics import (
    evaluate_binary_predictions,
    validate_binary_labels,
)


def evaluate_comparison_model(
    true_labels: Sequence[int] | np.ndarray,
    predicted_labels: Sequence[int] | np.ndarray,
    positive_probabilities: Sequence[float] | np.ndarray | None = None,
) -> dict[str, Any]:
    """Return shared binary metrics and named confusion-matrix counts."""
    metrics = evaluate_binary_predictions(
        true_labels, predicted_labels, positive_probabilities
    )
    matrix = metrics["confusion_matrix"]
    true_negative, false_positive = matrix[0]
    false_negative, true_positive = matrix[1]
    return {
        **metrics,
        "true_negative": int(true_negative),
        "false_positive": int(false_positive),
        "false_negative": int(false_negative),
        "true_positive": int(true_positive),
        "positive_class": 1,
        "zero_division": 0,
    }


def compare_error_categories(
    true_labels: Sequence[int] | np.ndarray,
    baseline_predictions: Sequence[int] | np.ndarray,
    textcnn_predictions: Sequence[int] | np.ndarray,
) -> dict[str, int]:
    """Count correct, shared-error, and disagreement cases in aligned arrays."""
    actual = validate_binary_labels(true_labels, "true_labels")
    baseline = validate_binary_labels(baseline_predictions, "baseline_predictions")
    textcnn = validate_binary_labels(textcnn_predictions, "textcnn_predictions")
    if len(actual) != len(baseline) or len(actual) != len(textcnn):
        raise ValueError("Labels and both prediction arrays must have equal lengths.")

    baseline_correct = baseline == actual
    textcnn_correct = textcnn == actual
    return {
        "both_correct": int(np.count_nonzero(baseline_correct & textcnn_correct)),
        "baseline_only_correct": int(
            np.count_nonzero(baseline_correct & ~textcnn_correct)
        ),
        "textcnn_only_correct": int(
            np.count_nonzero(~baseline_correct & textcnn_correct)
        ),
        "both_wrong_same_prediction": int(
            np.count_nonzero(
                ~baseline_correct & ~textcnn_correct & (baseline == textcnn)
            )
        ),
        "different_predictions": int(np.count_nonzero(baseline != textcnn)),
    }


def benchmark_single_review_latency(
    predict_one: Callable[[str], Any],
    reviews: Sequence[str],
    *,
    warmup_count: int = 5,
    repetitions: int = 3,
    device: str = "cpu",
    synchronize: Callable[[], None] | None = None,
) -> dict[str, int | float | str | bool]:
    """Measure end-to-end single-review latency over repeated identical inputs."""
    texts = _validate_reviews(reviews)
    _validate_benchmark_counts(warmup_count, repetitions)
    for text in texts[: min(warmup_count, len(texts))]:
        _synchronize(synchronize)
        predict_one(text)
        _synchronize(synchronize)

    observations_ms: list[float] = []
    for _ in range(repetitions):
        for text in texts:
            _synchronize(synchronize)
            started = time.perf_counter()
            predict_one(text)
            _synchronize(synchronize)
            observations_ms.append((time.perf_counter() - started) * 1_000)

    return {
        "device": device,
        "sample_count": len(texts),
        "warmup_count": min(warmup_count, len(texts)),
        "timed_repetitions": repetitions,
        "timed_observations": len(observations_ms),
        "batch_size": 1,
        "mean_latency_ms": statistics.fmean(observations_ms),
        "median_latency_ms": statistics.median(observations_ms),
        "end_to_end_includes_preprocessing": True,
    }


def benchmark_batch_throughput(
    predict_batch: Callable[[Sequence[str]], Any],
    reviews: Sequence[str],
    *,
    batch_size: int = 32,
    warmup_count: int = 1,
    repetitions: int = 3,
    device: str = "cpu",
    synchronize: Callable[[], None] | None = None,
) -> dict[str, int | float | str | bool]:
    """Measure end-to-end samples/second over batches of the same raw reviews."""
    texts = _validate_reviews(reviews)
    _validate_benchmark_counts(warmup_count, repetitions)
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or batch_size < 1
    ):
        raise ValueError("batch_size must be a positive integer.")

    batches = [
        texts[offset : offset + batch_size]
        for offset in range(0, len(texts), batch_size)
    ]
    for _ in range(warmup_count):
        _synchronize(synchronize)
        predict_batch(batches[0])
        _synchronize(synchronize)

    _synchronize(synchronize)
    started = time.perf_counter()
    for _ in range(repetitions):
        for batch in batches:
            predict_batch(batch)
    _synchronize(synchronize)
    elapsed = time.perf_counter() - started
    if not math.isfinite(elapsed) or elapsed <= 0:
        raise RuntimeError("Batch inference benchmark produced an invalid duration.")

    return {
        "device": device,
        "sample_count_per_repetition": len(texts),
        "batch_size": batch_size,
        "batch_count_per_repetition": len(batches),
        "warmup_batches": warmup_count,
        "timed_repetitions": repetitions,
        "elapsed_seconds": elapsed,
        "samples_per_second": len(texts) * repetitions / elapsed,
        "end_to_end_includes_preprocessing": True,
    }


def required_artifact_sizes(artifacts: Mapping[str, str | Path]) -> dict[str, Any]:
    """Measure named on-disk inference artifacts and their combined size."""
    if not artifacts:
        raise ValueError("At least one required artifact must be provided.")
    sizes: dict[str, int] = {}
    for name, path_value in artifacts.items():
        if not name or name in sizes:
            raise ValueError("Artifact names must be non-empty and unique.")
        path = Path(path_value).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Required artifact not found: {path}")
        sizes[name] = path.stat().st_size
    return {"files": sizes, "total_bytes": sum(sizes.values())}


def available_history_metrics(
    history: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    """Return recorded learning-curve metrics without inventing missing fields."""
    if not history:
        raise ValueError("Training history must contain at least one epoch.")
    metric_names = (
        "training_loss",
        "validation_loss",
        "training_accuracy",
        "validation_accuracy",
        "training_f1",
        "validation_f1",
    )
    return tuple(name for name in metric_names if all(name in row for row in history))


def _validate_reviews(reviews: Sequence[str]) -> list[str]:
    if isinstance(reviews, str):
        raise TypeError("reviews must be a sequence of review strings.")
    texts = list(reviews)
    if not texts or not all(isinstance(text, str) for text in texts):
        raise ValueError("reviews must contain at least one string review.")
    return texts


def _validate_benchmark_counts(warmup_count: int, repetitions: int) -> None:
    if (
        isinstance(warmup_count, bool)
        or not isinstance(warmup_count, int)
        or warmup_count < 0
    ):
        raise ValueError("warmup_count must be a non-negative integer.")
    if (
        isinstance(repetitions, bool)
        or not isinstance(repetitions, int)
        or repetitions < 1
    ):
        raise ValueError("repetitions must be a positive integer.")


def _synchronize(synchronize: Callable[[], None] | None) -> None:
    if synchronize is not None:
        synchronize()
