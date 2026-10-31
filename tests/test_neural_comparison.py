"""Tests for saved validation-prediction integrity in Phase 12."""

import numpy as np
import pandas as pd
import pytest

from deeptext.experiments.neural_comparison import _read_predictions


def _write_predictions(path, rows):
    pd.DataFrame(
        rows,
        columns=(
            "review_id",
            "true_label",
            "predicted_label",
            "positive_probability",
        ),
    ).to_csv(path, index=False)


def test_read_predictions_accepts_aligned_rows(tmp_path):
    path = tmp_path / "predictions.csv"
    _write_predictions(
        path,
        [("train/neg/1.txt", 0, 0, 0.1), ("train/pos/2.txt", 1, 1, 0.9)],
    )

    predictions, probabilities = _read_predictions(
        path, ["train/neg/1.txt", "train/pos/2.txt"], np.array([0, 1])
    )

    np.testing.assert_array_equal(predictions, [0, 1])
    np.testing.assert_allclose(probabilities, [0.1, 0.9])


def test_read_predictions_rejects_row_order_mismatch(tmp_path):
    path = tmp_path / "predictions.csv"
    _write_predictions(
        path,
        [("train/pos/2.txt", 1, 1, 0.9), ("train/neg/1.txt", 0, 0, 0.1)],
    )

    with pytest.raises(ValueError, match="order does not match"):
        _read_predictions(
            path, ["train/neg/1.txt", "train/pos/2.txt"], np.array([0, 1])
        )


def test_read_predictions_rejects_label_and_probability_corruption(tmp_path):
    path = tmp_path / "predictions.csv"
    _write_predictions(path, [("train/neg/1.txt", 1, 0, 1.2)])

    with pytest.raises(ValueError, match="labels differ"):
        _read_predictions(path, ["train/neg/1.txt"], np.array([0]))

    _write_predictions(path, [("train/neg/1.txt", 0, 0, 1.2)])
    with pytest.raises(ValueError, match="Invalid positive probabilities"):
        _read_predictions(path, ["train/neg/1.txt"], np.array([0]))
