"""Run a validation-only configuration comparison for the IMDB baseline."""

from __future__ import annotations

import argparse
import json
import platform
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

import joblib
import numpy as np
import pandas as pd
import sklearn

from deeptext.data.dataset import load_imdb_split, split_training_data
from deeptext.evaluation.metrics import evaluate_binary_predictions
from deeptext.models.baseline import (
    BaselineSettings,
    load_baseline_artifacts,
    predict_baseline,
    predict_baseline_probabilities,
    save_baseline_artifacts,
    train_baseline,
)

DEFAULT_SEED = 42
VALIDATION_SIZE = 0.2
REGULARIZATION_STRENGTHS = (0.1, 1.0)


def _class_counts(labels: Sequence[int] | pd.Series) -> dict[str, int]:
    counts = pd.Series(labels).value_counts().to_dict()
    return {"negative": int(counts.get(0, 0)), "positive": int(counts.get(1, 0))}


def _validation_overlap(
    training_texts: Sequence[str], validation_texts: Sequence[str]
) -> dict[str, int]:
    training_review_texts = set(training_texts)
    overlapping_validation_rows = sum(
        text in training_review_texts for text in validation_texts
    )
    return {
        "unique_exact_texts_shared": len(
            training_review_texts.intersection(validation_texts)
        ),
        "validation_rows_matching_training_text": overlapping_validation_rows,
    }


def _measure_inference_latency(
    model: Any,
    texts: Sequence[str],
    sample_count: int = 100,
    warmup_count: int = 5,
) -> dict[str, float | int]:
    sample = list(texts[:sample_count])
    if not sample:
        raise ValueError("Validation texts are required to measure inference latency.")

    for text in sample[: min(warmup_count, len(sample))]:
        predict_baseline(model, [text])

    started = time.perf_counter()
    for text in sample:
        predict_baseline(model, [text])
    elapsed = time.perf_counter() - started
    return {
        "batch_size": 1,
        "sample_count": len(sample),
        "seconds_per_review": elapsed / len(sample),
        "milliseconds_per_review": elapsed * 1_000 / len(sample),
    }


def run_baseline_experiment(
    dataset_dir: str | Path = Path("datasets/aclImdb"),
    output_root: str | Path = Path("outputs/baseline"),
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """Train candidate models on 80% of IMDB train and select on 20% only."""
    original_training = load_imdb_split(dataset_dir, "train")
    training, validation = split_training_data(
        original_training, validation_size=VALIDATION_SIZE, seed=seed
    )
    training_texts = training["text"].tolist()
    validation_texts = validation["text"].tolist()
    training_labels = training["label"].to_numpy()
    validation_labels = validation["label"].to_numpy()

    settings_to_compare = [
        BaselineSettings(ngram_range=ngram_range, c=c, seed=seed)
        for ngram_range in ((1, 1), (1, 2))
        for c in REGULARIZATION_STRENGTHS
    ]
    comparisons: list[dict[str, Any]] = []
    best_model = None
    best_settings = None
    best_rank: tuple[float, float, int, float] | None = None

    for settings in settings_to_compare:
        started = time.perf_counter()
        model = train_baseline(training_texts, training_labels, settings)
        training_duration = time.perf_counter() - started
        predictions = predict_baseline(model, validation_texts)
        probabilities = predict_baseline_probabilities(model, validation_texts)[:, 1]
        metrics = evaluate_binary_predictions(
            validation_labels, predictions, probabilities
        )
        comparison = {
            "settings": settings.to_dict(),
            "training_duration_seconds": training_duration,
            "validation_metrics": metrics,
        }
        comparisons.append(comparison)

        rank = (
            float(metrics["f1"]),
            float(metrics["accuracy"]),
            int(settings.ngram_range == (1, 1)),
            -settings.c,
        )
        if best_rank is None or rank > best_rank:
            best_rank = rank
            best_model = model
            best_settings = settings

    assert best_model is not None and best_settings is not None
    selected_comparison = next(
        result
        for result in comparisons
        if result["settings"] == best_settings.to_dict()
    )
    validation_metrics = selected_comparison["validation_metrics"]
    inference_latency = _measure_inference_latency(best_model, validation_texts)

    created_at = datetime.now(UTC)
    experiment_id = f"imdb-baseline-{created_at:%Y%m%dT%H%M%S.%fZ}"
    destination = Path(output_root).expanduser() / experiment_id
    destination.mkdir(parents=True, exist_ok=False)
    metadata: dict[str, Any] = {
        "experiment_id": experiment_id,
        "created_at_utc": created_at.isoformat(),
        "dataset": "aclImdb v1.0",
        "model_name": "TF-IDF + Logistic Regression",
        "selection_rule": (
            "Highest validation F1, then accuracy; prefer unigrams and then "
            "the smaller C on ties."
        ),
        "selected_settings": best_settings.to_dict(),
        "configuration_comparisons": comparisons,
        "seed": seed,
        "split": {
            "source": "official labeled training split only",
            "validation_fraction": VALIDATION_SIZE,
            "random_state": seed,
            "training_rows": len(training),
            "validation_rows": len(validation),
            "training_class_counts": _class_counts(training["label"]),
            "validation_class_counts": _class_counts(validation["label"]),
            "exact_duplicate_overlap": _validation_overlap(
                training_texts, validation_texts
            ),
        },
        "validation_metrics": validation_metrics,
        "training_duration_seconds": selected_comparison["training_duration_seconds"],
        "inference_latency": inference_latency,
        "software_versions": {
            "python": platform.python_version(),
            "scikit_learn": sklearn.__version__,
            "numpy": np.__version__,
            "pandas": pd.__version__,
            "joblib": joblib.__version__,
        },
        "official_test_evaluated": False,
        "unlabeled_reviews_used": False,
        "vectorizer_fit_partition": "training",
        "artifact_reload_predictions_equivalent": False,
    }
    model_path, metadata_path = save_baseline_artifacts(
        best_model, destination, metadata
    )
    reloaded_model, saved_metadata = load_baseline_artifacts(destination)
    verification_texts = validation_texts[: min(25, len(validation_texts))]
    predictions_before_reload = predict_baseline(best_model, verification_texts)
    predictions_after_reload = predict_baseline(reloaded_model, verification_texts)
    probabilities_before_reload = predict_baseline_probabilities(
        best_model, verification_texts
    )
    probabilities_after_reload = predict_baseline_probabilities(
        reloaded_model, verification_texts
    )
    equivalent = predictions_before_reload == predictions_after_reload and np.allclose(
        probabilities_before_reload, probabilities_after_reload
    )
    saved_metadata["artifact_reload_predictions_equivalent"] = bool(equivalent)
    metadata_path.write_text(
        json.dumps(saved_metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if not equivalent:
        raise RuntimeError(
            "Reloaded baseline predictions did not match the fitted model."
        )

    saved_metadata["artifact_directory"] = str(destination.resolve())
    saved_metadata["model_artifact_path"] = str(model_path.resolve())
    saved_metadata["metadata_path"] = str(metadata_path.resolve())
    return saved_metadata


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare small TF-IDF/Logistic Regression settings on IMDB validation data."
        )
    )
    parser.add_argument("--dataset-dir", type=Path, default=Path("datasets/aclImdb"))
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/baseline"))
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    arguments = parser.parse_args()

    result = run_baseline_experiment(
        dataset_dir=arguments.dataset_dir,
        output_root=arguments.output_dir,
        seed=arguments.seed,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
