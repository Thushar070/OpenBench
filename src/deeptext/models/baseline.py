"""TF-IDF and Logistic Regression baseline for binary sentiment analysis."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

import joblib
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.utils.validation import check_is_fitted

from deeptext.evaluation.metrics import validate_binary_labels


@dataclass(frozen=True)
class BaselineSettings:
    """Small, explicit set of TF-IDF and Logistic Regression settings."""

    ngram_range: tuple[int, int] = (1, 1)
    c: float = 1.0
    seed: int = 42
    max_iter: int = 1_000

    def __post_init__(self) -> None:
        if self.ngram_range not in ((1, 1), (1, 2)):
            raise ValueError("ngram_range must be (1, 1) or (1, 2).")
        if not math.isfinite(self.c) or self.c <= 0:
            raise ValueError("c must be a finite positive number.")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ValueError("seed must be an integer.")
        if isinstance(self.max_iter, bool) or self.max_iter < 1:
            raise ValueError("max_iter must be a positive integer.")

    def to_dict(self) -> dict[str, object]:
        """Return JSON-friendly configuration data."""
        return asdict(self)


def _text_batch(texts: Sequence[str], name: str) -> list[str]:
    if isinstance(texts, str):
        raise TypeError(f"{name} must be a sequence of review strings, not one string.")
    batch = list(texts)
    if not batch:
        raise ValueError(f"{name} must contain at least one review.")
    if not all(isinstance(text, str) for text in batch):
        raise ValueError(f"{name} must contain only strings.")
    return batch


def train_baseline(
    training_texts: Sequence[str],
    training_labels: Sequence[int] | np.ndarray,
    settings: BaselineSettings | None = None,
) -> Pipeline:
    """Fit a TF-IDF/Logistic Regression pipeline on training data only."""
    texts = _text_batch(training_texts, "training_texts")
    labels = validate_binary_labels(training_labels, "training_labels")
    if len(texts) != len(labels):
        raise ValueError("training_texts and training_labels must have equal lengths.")
    if set(labels.tolist()) != {0, 1}:
        raise ValueError("training_labels must include both classes 0 and 1.")
    if not any(text.strip() for text in texts):
        raise ValueError("training_texts must include at least one non-empty review.")

    configuration = settings or BaselineSettings()
    pipeline = Pipeline(
        steps=[
            (
                "tfidf",
                TfidfVectorizer(ngram_range=configuration.ngram_range),
            ),
            (
                "logistic_regression",
                LogisticRegression(
                    C=configuration.c,
                    max_iter=configuration.max_iter,
                    random_state=configuration.seed,
                ),
            ),
        ]
    )
    pipeline.fit(texts, labels)
    return pipeline


def predict_baseline(model: Pipeline, texts: Sequence[str]) -> list[int]:
    """Predict negative (0) or positive (1) labels for review strings."""
    batch = _text_batch(texts, "texts")
    return [int(label) for label in model.predict(batch)]


def predict_baseline_probabilities(model: Pipeline, texts: Sequence[str]) -> np.ndarray:
    """Return per-review probabilities in class order 0 (negative), 1 (positive)."""
    batch = _text_batch(texts, "texts")
    classifier = model.named_steps.get("logistic_regression")
    if classifier is None or not hasattr(classifier, "predict_proba"):
        raise ValueError("The supplied baseline does not provide class probabilities.")
    probabilities = np.asarray(model.predict_proba(batch), dtype=np.float64)
    classes = list(classifier.classes_)
    if classes != [0, 1]:
        raise ValueError("The fitted baseline must contain classes 0 and 1.")
    return probabilities


def save_baseline_artifacts(
    model: Pipeline,
    artifact_directory: str | Path,
    metadata: dict[str, Any],
) -> tuple[Path, Path]:
    """Save a trusted fitted pipeline and its JSON metadata in one directory."""
    check_is_fitted(model.named_steps["tfidf"], attributes=["vocabulary_"])
    check_is_fitted(model.named_steps["logistic_regression"])
    destination = Path(artifact_directory).expanduser()
    destination.mkdir(parents=True, exist_ok=True)
    model_path = destination / "baseline.joblib"
    metadata_path = destination / "metadata.json"

    joblib.dump(model, model_path, compress=3)
    saved_metadata = dict(metadata)
    saved_metadata["model_artifact"] = model_path.name
    saved_metadata["model_artifact_size_bytes"] = model_path.stat().st_size
    with metadata_path.open("w", encoding="utf-8", newline="\n") as metadata_file:
        json.dump(saved_metadata, metadata_file, indent=2, sort_keys=True)
        metadata_file.write("\n")
    return model_path, metadata_path


def load_baseline_artifacts(
    artifact_directory: str | Path,
) -> tuple[Pipeline, dict[str, Any]]:
    """Load local trusted artifacts; never load files from untrusted sources."""
    source = Path(artifact_directory).expanduser()
    model_path = source / "baseline.joblib"
    metadata_path = source / "metadata.json"
    if not model_path.is_file():
        raise FileNotFoundError(f"Baseline model artifact not found: {model_path}")
    if not metadata_path.is_file():
        raise FileNotFoundError(f"Baseline metadata not found: {metadata_path}")

    model = joblib.load(model_path)
    if not isinstance(model, Pipeline):
        raise ValueError("The saved baseline artifact is not a scikit-learn Pipeline.")
    if (
        "tfidf" not in model.named_steps
        or "logistic_regression" not in model.named_steps
    ):
        raise ValueError("The saved pipeline is missing required baseline steps.")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError("Baseline metadata must contain a JSON object.")
    return model, metadata
