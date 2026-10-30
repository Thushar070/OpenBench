"""Metrics shared by binary text-classification models."""

from __future__ import annotations

from numbers import Integral
from typing import Any

import numpy as np
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
)


def validate_binary_labels(values: Any, name: str) -> np.ndarray:
    """Return validated binary integer labels as a one-dimensional array."""
    labels = np.asarray(values)
    if labels.ndim != 1:
        raise ValueError(f"{name} must be a one-dimensional sequence of labels.")
    if labels.size == 0:
        raise ValueError(f"{name} must contain at least one label.")

    valid = [
        isinstance(value, Integral)
        and not isinstance(value, (bool, np.bool_))
        and int(value) in (0, 1)
        for value in labels.tolist()
    ]
    if not all(valid):
        raise ValueError(f"{name} must contain only binary integers 0 and 1.")
    return labels.astype(np.int64, copy=False)


def evaluate_binary_predictions(
    true_labels: Any,
    predicted_labels: Any,
    probabilities: Any | None = None,
) -> dict[str, object]:
    """Return binary metrics with confusion rows/columns ordered as 0, then 1.

    Labels use 0 for negative and 1 for positive. If supplied, probabilities
    are the predicted probabilities for class 1 and are validated for shape
    and range; threshold selection is intentionally outside this function.
    """
    actual = validate_binary_labels(true_labels, "true_labels")
    predicted = validate_binary_labels(predicted_labels, "predicted_labels")
    if len(actual) != len(predicted):
        raise ValueError("true_labels and predicted_labels must have equal lengths.")

    if probabilities is not None:
        scores = np.asarray(probabilities, dtype=np.float64)
        if scores.ndim != 1 or scores.size != actual.size:
            raise ValueError(
                "probabilities must be a one-dimensional sequence matching labels."
            )
        if not np.isfinite(scores).all() or ((scores < 0) | (scores > 1)).any():
            raise ValueError("probabilities must be finite values between 0 and 1.")

    matrix = confusion_matrix(actual, predicted, labels=[0, 1])
    return {
        "accuracy": float(accuracy_score(actual, predicted)),
        "precision": float(precision_score(actual, predicted, zero_division=0)),
        "recall": float(recall_score(actual, predicted, zero_division=0)),
        "f1": float(f1_score(actual, predicted, zero_division=0)),
        "confusion_matrix": matrix.tolist(),
        "confusion_matrix_labels": [0, 1],
    }
