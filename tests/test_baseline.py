from pathlib import Path

import numpy as np
import pytest

from deeptext.evaluation.metrics import evaluate_binary_predictions
from deeptext.models.baseline import (
    BaselineSettings,
    load_baseline_artifacts,
    predict_baseline,
    predict_baseline_probabilities,
    save_baseline_artifacts,
    train_baseline,
)

TRAINING_TEXTS = [
    "wonderful delightful movie",
    "excellent happy story",
    "boring awful movie",
    "terrible dull story",
]
TRAINING_LABELS = [1, 1, 0, 0]


def test_baseline_trains_predicts_and_returns_class_probabilities() -> None:
    model = train_baseline(TRAINING_TEXTS, TRAINING_LABELS)

    assert predict_baseline(model, ["wonderful excellent", "awful terrible"]) == [
        1,
        0,
    ]
    probabilities = predict_baseline_probabilities(
        model, ["wonderful excellent", "awful terrible"]
    )
    assert probabilities.shape == (2, 2)
    assert np.all(np.isfinite(probabilities))
    assert np.allclose(probabilities.sum(axis=1), 1.0)


def test_tfidf_fits_training_text_only_and_handles_empty_or_unseen_text() -> None:
    validation_only_term = "validationonlytokenxyz"
    model = train_baseline(
        TRAINING_TEXTS,
        TRAINING_LABELS,
        BaselineSettings(ngram_range=(1, 2)),
    )

    vocabulary = model.named_steps["tfidf"].vocabulary_
    assert validation_only_term not in vocabulary
    assert len(predict_baseline(model, ["", validation_only_term])) == 2
    assert np.all(
        np.isfinite(predict_baseline_probabilities(model, ["", validation_only_term]))
    )


def test_baseline_rejects_invalid_training_inputs_and_empty_prediction_batch() -> None:
    with pytest.raises(ValueError, match="equal lengths"):
        train_baseline(TRAINING_TEXTS, [1, 1])
    with pytest.raises(ValueError, match="both classes"):
        train_baseline(TRAINING_TEXTS[:2], [1, 1])
    with pytest.raises(ValueError, match="binary integers"):
        train_baseline(TRAINING_TEXTS, [1, 1, 0, 2])
    model = train_baseline(TRAINING_TEXTS, TRAINING_LABELS)
    with pytest.raises(ValueError, match="at least one review"):
        predict_baseline(model, [])


def test_binary_metrics_match_hand_calculated_example() -> None:
    result = evaluate_binary_predictions(
        true_labels=[0, 0, 1, 1, 1],
        predicted_labels=[0, 1, 1, 0, 1],
        probabilities=[0.1, 0.7, 0.8, 0.4, 0.9],
    )

    assert result["accuracy"] == pytest.approx(0.6)
    assert result["precision"] == pytest.approx(2 / 3)
    assert result["recall"] == pytest.approx(2 / 3)
    assert result["f1"] == pytest.approx(2 / 3)
    assert result["confusion_matrix"] == [[1, 1], [1, 2]]
    assert result["confusion_matrix_labels"] == [0, 1]


@pytest.mark.parametrize(
    ("true_labels", "predicted_labels", "probabilities", "message"),
    [
        ([0, 1], [[0], [1]], None, "one-dimensional"),
        ([0, 1], [0], None, "equal lengths"),
        ([0, 2], [0, 1], None, "binary integers"),
        ([0, 1], [0, 1], [0.4], "matching labels"),
        ([0, 1], [0, 1], [0.4, float("nan")], "finite values"),
    ],
)
def test_binary_metrics_reject_invalid_shapes_labels_and_probabilities(
    true_labels: list[object],
    predicted_labels: object,
    probabilities: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        evaluate_binary_predictions(true_labels, predicted_labels, probabilities)


def test_saved_and_reloaded_baseline_has_equivalent_predictions(
    tmp_path: Path,
) -> None:
    model = train_baseline(TRAINING_TEXTS, TRAINING_LABELS)
    model_path, metadata_path = save_baseline_artifacts(
        model, tmp_path / "artifact", {"experiment_id": "test-run"}
    )

    reloaded, metadata = load_baseline_artifacts(tmp_path / "artifact")
    samples = ["wonderful movie", "dull story", "unknownwordxyz"]
    assert model_path.is_file()
    assert metadata_path.is_file()
    assert metadata["experiment_id"] == "test-run"
    assert metadata["model_artifact_size_bytes"] == model_path.stat().st_size
    assert predict_baseline(reloaded, samples) == predict_baseline(model, samples)
    assert np.allclose(
        predict_baseline_probabilities(reloaded, samples),
        predict_baseline_probabilities(model, samples),
    )


def test_baseline_training_is_reproducible_for_fixed_settings() -> None:
    settings = BaselineSettings(ngram_range=(1, 2), c=0.5, seed=42)

    first = train_baseline(TRAINING_TEXTS, TRAINING_LABELS, settings)
    second = train_baseline(TRAINING_TEXTS, TRAINING_LABELS, settings)

    assert (
        first.named_steps["tfidf"].vocabulary_
        == second.named_steps["tfidf"].vocabulary_
    )
    assert predict_baseline(first, TRAINING_TEXTS) == predict_baseline(
        second, TRAINING_TEXTS
    )
