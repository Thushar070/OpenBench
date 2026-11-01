"""Regression checks for active-model short-text inference quality."""

from __future__ import annotations

import math

import pytest

from deeptext.dashboard.artifacts import MODEL_KEYS, ArtifactRepository
from deeptext.dashboard.inference import PredictorRegistry

POSITIVE_EXAMPLES = (
    "the movie was good",
    "the movie was too good",
    "this movie is excellent",
    "i loved this movie",
    "the acting was fantastic",
    "what a wonderful film",
    "this was an enjoyable experience",
)

NEGATIVE_EXAMPLES = (
    "the movie was bad",
    "the movie was terrible",
    "i hated this movie",
    "the acting was awful",
    "this was a disappointing film",
)

SEMANTIC_EXAMPLES = (
    "the movie was not good",
    "the movie was not bad",
    "the movie was good but the ending was terrible",
    "the acting was excellent but the story was disappointing",
)

# Frozen-v6 ceilings preserve known validation trade-offs while allowing fixes.
MAX_BASIC_ERRORS = {
    "logistic_regression": 1,
    "textcnn": 0,
    "rnn": 3,
    "lstm": 0,
    "gru": 1,
}


def _model_rows(result: dict[str, object]) -> dict[str, dict[str, object]]:
    models = result["models"]
    assert isinstance(models, list)
    rows = {row["id"]: row for row in models}
    assert set(rows) == set(MODEL_KEYS)
    assert result["available_model_count"] == len(MODEL_KEYS)

    for row in rows.values():
        assert row["status"] == "ok"
        assert row["predicted_class"] in (0, 1)
        negative = row["negative_probability"]
        positive = row["positive_probability"]
        assert isinstance(negative, (int, float))
        assert isinstance(positive, (int, float))
        assert math.isfinite(negative) and math.isfinite(positive)
        assert 0 <= negative <= 1 and 0 <= positive <= 1
        assert math.isclose(negative + positive, 1.0, abs_tol=1e-6)
        assert row["sentiment"] == (
            "positive" if row["predicted_class"] else "negative"
        )
    return rows


def test_active_models_keep_short_text_baseline_and_are_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deeptext.dashboard.inference as inference

    monkeypatch.setattr(inference.torch.cuda, "is_available", lambda: False)
    registry = PredictorRegistry(ArtifactRepository())
    examples = [
        *((text, 1) for text in POSITIVE_EXAMPLES),
        *((text, 0) for text in NEGATIVE_EXAMPLES),
    ]
    errors: dict[str, list[dict[str, object]]] = {
        model_id: [] for model_id in MODEL_KEYS
    }

    for text, expected_label in examples:
        first_result = registry.predict(text)
        first = _model_rows(first_result)
        second = _model_rows(registry.predict(text))
        for model_id in MODEL_KEYS:
            row = first[model_id]
            repeated = second[model_id]
            assert row["predicted_class"] == repeated["predicted_class"]
            assert math.isclose(
                row["positive_probability"],
                repeated["positive_probability"],
                rel_tol=0,
                abs_tol=1e-7,
            )
            if row["predicted_class"] != expected_label:
                errors[model_id].append(
                    {
                        "text": text,
                        "expected": expected_label,
                        "predicted": row["predicted_class"],
                        "positive_probability": row["positive_probability"],
                    }
                )

    excess_errors = {
        model_id: rows
        for model_id, rows in errors.items()
        if len(rows) > MAX_BASIC_ERRORS[model_id]
    }
    assert not excess_errors, f"Short-text regression errors increased: {excess_errors}"


def test_semantic_short_texts_return_valid_results_without_forced_agreement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deeptext.dashboard.inference as inference

    monkeypatch.setattr(inference.torch.cuda, "is_available", lambda: False)
    registry = PredictorRegistry(ArtifactRepository())

    for text in SEMANTIC_EXAMPLES:
        result = registry.predict(text)
        _model_rows(result)
