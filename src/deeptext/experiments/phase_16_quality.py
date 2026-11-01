"""Source-aware quality summaries for the frozen Phase 16 model audit."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from deeptext.evaluation.metrics import evaluate_binary_predictions
from deeptext.experiments.efficiency import (
    MODEL_KEYS,
    _latest_robustness_sample,
    _latest_verified_phase12,
    _latest_verified_phase13,
)

MODEL_REQUIRED_FILES = {
    "logistic_regression": ("baseline.joblib", "metadata.json"),
    "textcnn": ("best_model.pt", "vocabulary.json", "metadata.json"),
    "rnn": ("best_model.pt", "vocabulary.json", "metadata.json"),
    "lstm": ("best_model.pt", "vocabulary.json", "metadata.json"),
    "gru": ("best_model.pt", "vocabulary.json", "metadata.json"),
}

DIAGNOSTIC_EXAMPLES = (
    ("the movie was good", 1, "basic"),
    ("the movie was too good", 1, "basic"),
    ("this movie is excellent", 1, "basic"),
    ("i loved this movie", 1, "basic"),
    ("the acting was fantastic", 1, "basic"),
    ("what a wonderful film", 1, "basic"),
    ("this was an enjoyable experience", 1, "basic"),
    ("the movie was bad", 0, "basic"),
    ("the movie was terrible", 0, "basic"),
    ("i hated this movie", 0, "basic"),
    ("the acting was awful", 0, "basic"),
    ("this was a disappointing film", 0, "basic"),
    ("the movie was not good", None, "semantic_example"),
    ("the movie was not bad", None, "semantic_example"),
    ("the movie was good but the ending was terrible", None, "semantic_example"),
    (
        "the acting was excellent but the story was disappointing",
        None,
        "semantic_example",
    ),
)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}.")
    return value


def discover_active_generation(root: str | Path) -> dict[str, Any]:
    """Resolve and validate the exact model files named by the active pointer."""
    project_root = Path(root).resolve()
    outputs = (project_root / "outputs").resolve()
    pointer_path = outputs / "models/active_generation.json"
    pointer = _read_json(pointer_path)
    generation_id = pointer.get("generation_id")
    manifest_relative = pointer.get("generation_manifest")
    if not isinstance(generation_id, str) or not generation_id:
        raise ValueError("The active generation pointer has no generation ID.")
    if not isinstance(manifest_relative, str) or not manifest_relative:
        raise ValueError("The active generation pointer has no manifest path.")

    generation_root = (outputs / "models" / generation_id).resolve()
    manifest_path = (outputs / "models" / manifest_relative).resolve()
    if not manifest_path.is_relative_to(generation_root):
        raise ValueError("The active generation manifest escapes its generation.")
    manifest = _read_json(manifest_path)
    if manifest.get("generation_id") != generation_id:
        raise ValueError(
            "The active generation manifest ID does not match its pointer."
        )

    model_directories: dict[str, Path] = {}
    models = manifest.get("models")
    if not isinstance(models, dict) or set(models) != set(MODEL_KEYS):
        raise ValueError("The active generation must name all five model artifacts.")
    for model_id in MODEL_KEYS:
        relative_path = models[model_id]
        if not isinstance(relative_path, str):
            raise ValueError(f"The {model_id} artifact path is invalid.")
        directory = (generation_root / relative_path).resolve()
        if not directory.is_relative_to(generation_root) or not directory.is_dir():
            raise FileNotFoundError(
                f"The active {model_id} artifact directory is missing."
            )
        missing = [
            name
            for name in MODEL_REQUIRED_FILES[model_id]
            if not (directory / name).is_file()
        ]
        if missing:
            raise FileNotFoundError(
                f"The active {model_id} artifacts are missing: {', '.join(missing)}."
            )
        metadata = _read_json(directory / "metadata.json")
        if metadata.get("generation_id") != generation_id:
            raise ValueError(
                f"The active {model_id} metadata has a different generation ID."
            )
        model_directories[model_id] = directory

    return {
        "project_root": project_root,
        "pointer_path": pointer_path,
        "pointer": pointer,
        "generation_id": generation_id,
        "generation_root": generation_root,
        "manifest_path": manifest_path,
        "manifest": manifest,
        "model_directories": model_directories,
    }


def _count_from_matrix(matrix: list[list[int]] | None) -> int | None:
    if not matrix or len(matrix) != 2 or any(len(row) != 2 for row in matrix):
        return None
    return int(sum(sum(row) for row in matrix))


def _normalize_metrics(
    metrics: dict[str, Any] | None,
    *,
    sample_count: int | None = None,
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if metrics is None:
        return {
            "available": False,
            "evaluated_examples": None,
            "accuracy": None,
            "precision": None,
            "recall": None,
            "f1": None,
            "confusion_matrix": None,
            "confusion_matrix_labels": [0, 1],
            "unavailable_reason": "The source evaluation did not include this model.",
        }
    matrix = metrics.get("confusion_matrix")
    if matrix is None and all(
        key in metrics
        for key in (
            "true_negative",
            "false_positive",
            "false_negative",
            "true_positive",
        )
    ):
        matrix = [
            [metrics["true_negative"], metrics["false_positive"]],
            [metrics["false_negative"], metrics["true_positive"]],
        ]
    count = sample_count or _count_from_matrix(matrix)
    result = {
        "available": True,
        "evaluated_examples": count,
        "accuracy": metrics.get("accuracy"),
        "precision": metrics.get("precision"),
        "recall": metrics.get("recall"),
        "f1": metrics.get("f1"),
        "confusion_matrix": matrix,
        "confusion_matrix_labels": metrics.get("confusion_matrix_labels", [0, 1]),
        "unavailable_reason": None,
    }
    standard_fields = {
        "accuracy",
        "precision",
        "recall",
        "f1",
        "confusion_matrix",
        "confusion_matrix_labels",
    }
    result.update(
        {key: value for key, value in metrics.items() if key not in standard_fields}
    )
    if extras:
        result.update(extras)
    return result


def _make_track(
    *,
    dataset_id: str,
    split: str,
    timestamp: str | None,
    source_path: Path,
    model_generation: str | None,
    source_run_id: str | None,
    metrics_by_model: dict[str, dict[str, Any] | None],
    sample_count: int | None,
    limitations: list[str] | None = None,
    source_artifacts: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "dataset_id": dataset_id,
        "split": split,
        "experiment_timestamp": timestamp,
        "source_artifact_location": str(source_path.resolve()),
        "source_model_generation": model_generation,
        "source_evaluation_run_id": source_run_id,
        "sample_count": sample_count,
        "label_mapping": {"negative": 0, "positive": 1},
        "metrics_by_model": {
            key: _normalize_metrics(
                metrics_by_model.get(key), sample_count=sample_count
            )
            for key in MODEL_KEYS
        },
        "source_model_artifacts": source_artifacts or {},
        "limitations": limitations or [],
    }


def _latest_phase7(root: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    for results_path in reversed(
        sorted(
            (root / "outputs/final_evaluation").glob(
                "imdb-final-evaluation-*/final_results.json"
            )
        )
    ):
        try:
            results = _read_json(results_path)
            metadata_path = results_path.parent / "metadata.json"
            metadata = _read_json(metadata_path) if metadata_path.is_file() else {}
            if (
                results.get("official_test_evaluated") is True
                and results.get("sample_count") == 25_000
                and {"logistic_regression", "textcnn"}.issubset(
                    results.get("metrics", {})
                )
            ):
                return results_path, results, metadata
        except OSError, TypeError, ValueError, json.JSONDecodeError:
            continue
    raise FileNotFoundError("No saved Phase 7 final evaluation was found.")


def _latest_phase14(root: Path) -> tuple[Path, dict[str, Any]] | None:
    candidates = sorted(
        (root / "outputs/interpretability").glob(
            "imdb-interpretability-*/metadata/run.json"
        )
    )
    for metadata_path in reversed(candidates):
        try:
            metadata = _read_json(metadata_path)
            if (
                metadata.get("phase") == 14
                and metadata.get("validation_only") is True
                and metadata.get("official_test_split_loaded") is False
                and metadata.get("source_artifacts_unchanged_sha256") is True
            ):
                return metadata_path.parent.parent, metadata
        except OSError, TypeError, ValueError, json.JSONDecodeError:
            continue
    return None


def collect_quality_sources(active: dict[str, Any]) -> dict[str, Any]:
    """Load only saved evaluation summaries; never load dataset review text."""
    root = active["project_root"]
    phase12_path, phase12 = _latest_verified_phase12(root)
    phase13_path, phase13, phase13_metadata = _latest_verified_phase13(root)
    phase7_path, phase7, phase7_metadata = _latest_phase7(root)
    phase15_root, _, phase15_metadata = _latest_robustness_sample(root)
    phase15_auto = _read_json(phase15_root / "automatic_perturbations/metrics.json")
    phase15_semantic = _read_json(phase15_root / "semantic_challenge/metrics.json")
    phase14 = _latest_phase14(root)

    phase12_artifacts = {
        key: phase12["source_artifacts"][
            "baseline" if key == "logistic_regression" else key
        ]
        for key in MODEL_KEYS
    }
    phase13_artifacts = {
        key: phase13.get("models", {}).get(key, {}).get("artifact_directory")
        for key in MODEL_KEYS
    }
    phase15_artifacts = phase15_metadata.get("source_artifacts", {})
    phase7_artifacts = {
        "logistic_regression": phase12_artifacts.get("logistic_regression"),
        "textcnn": phase12_artifacts.get("textcnn"),
    }

    phase12_metrics = phase12.get("metrics", {})
    phase13_metrics = phase13.get("metrics", {})
    phase7_metrics = phase7.get("metrics", {})
    current_model_selection = _read_json(
        active["generation_root"] / "model_selection.json"
    )
    current_selection_metrics = {
        key: current_model_selection.get(key, {})
        .get("selected", {})
        .get("full_validation_metrics")
        for key in MODEL_KEYS
    }
    active_test_metrics = _read_json(
        active["generation_root"] / "official_test_metrics.json"
    )
    active_external = _read_json(
        active["generation_root"] / "external_short_text_evaluation.json"
    )
    external_splits = active_external.get("validation_and_test_metrics", {})
    manifest_external = active["manifest"].get("external_training_data", {})
    partition_counts = manifest_external.get("partition_counts", {})

    generation_id = active["generation_id"]
    generation_timestamp = active["manifest"].get("created_at_utc")
    tracks = {
        "imdb_validation_recovery_selection": _make_track(
            dataset_id="aclImdb v1.0 / official labeled training corpus",
            split="group-aware IMDb train validation used for model selection",
            timestamp=generation_timestamp,
            source_path=active["generation_root"] / "model_selection.json",
            model_generation=generation_id,
            source_run_id=generation_id,
            metrics_by_model=current_selection_metrics,
            sample_count=5_000,
            limitations=[
                "Metrics describe selected candidate checkpoints before final refit; "
                "the active frozen artifacts were refit on the full training corpus."
            ],
            source_artifacts={
                key: str(path) for key, path in active["model_directories"].items()
            },
        ),
        "imdb_validation_phase12_historical": _make_track(
            dataset_id="aclImdb v1.0 / official labeled training corpus",
            split="Phase 12 saved validation split",
            timestamp=phase12.get("created_at_utc"),
            source_path=phase12_path,
            model_generation=None,
            source_run_id=phase12.get("comparison_id", phase12_path.parent.name),
            metrics_by_model=phase12_metrics,
            sample_count=phase12.get("sample_count"),
            limitations=[
                "Historical Phase 12 checkpoint generation; not active recovery "
                "weights."
            ],
            source_artifacts=phase12_artifacts,
        ),
        "imdb_official_test_active_generation_saved": _make_track(
            dataset_id="aclImdb v1.0 / official test partition",
            split="official test; previously evaluated and reused from saved metrics",
            timestamp=generation_timestamp,
            source_path=active["generation_root"] / "official_test_metrics.json",
            model_generation=generation_id,
            source_run_id=generation_id,
            metrics_by_model={key: active_test_metrics.get(key) for key in MODEL_KEYS},
            sample_count=active_test_metrics.get("sample_count"),
            limitations=[
                "The official test set has prior use in this project. These saved "
                "metrics are not a pristine independent test and were not rerun or "
                "used for model selection in Phase 16."
            ],
            source_artifacts={
                key: str(path) for key, path in active["model_directories"].items()
            },
        ),
        "imdb_official_test_phase13_historical": _make_track(
            dataset_id="aclImdb v1.0 / official test partition",
            split="Phase 13 official test; saved historical evaluation",
            timestamp=phase13_metadata.get("created_at_utc"),
            source_path=phase13_path,
            model_generation=None,
            source_run_id=phase13.get("evaluation_id"),
            metrics_by_model=phase13_metrics,
            sample_count=phase13.get("sample_count"),
            limitations=[
                "Historical Phase 13 checkpoints; the official test set has prior "
                "project use and these scores were not used for model selection."
            ],
            source_artifacts=phase13_artifacts,
        ),
        "imdb_official_test_phase7_historical": _make_track(
            dataset_id="aclImdb v1.0 / official test partition",
            split="Phase 7 official test; saved historical evaluation",
            timestamp=phase7_metadata.get("created_at_utc")
            or phase7.get("evaluation_id"),
            source_path=phase7_path,
            model_generation=None,
            source_run_id=phase7.get("evaluation_id"),
            metrics_by_model={key: phase7_metrics.get(key) for key in MODEL_KEYS},
            sample_count=phase7.get("sample_count"),
            limitations=[
                "Phase 7 evaluated only Logistic Regression and TextCNN; other "
                "model measurements are explicitly unavailable. This is a saved "
                "historical result on an already-used official test set."
            ],
            source_artifacts=phase7_artifacts,
        ),
        "uci_short_text_validation_active_generation_saved": _make_track(
            dataset_id=active_external.get(
                "dataset", "UCI Sentiment Labelled Sentences"
            ),
            split="Amazon/Yelp validation",
            timestamp=generation_timestamp,
            source_path=active["generation_root"]
            / "external_short_text_evaluation.json",
            model_generation=generation_id,
            source_run_id=generation_id,
            metrics_by_model=external_splits.get("validation", {}),
            sample_count=partition_counts.get("validation", {}).get("rows"),
            limitations=active_external.get("limitations", []),
            source_artifacts={
                key: str(path) for key, path in active["model_directories"].items()
            },
        ),
        "uci_short_text_test_active_generation_saved": _make_track(
            dataset_id=active_external.get(
                "dataset", "UCI Sentiment Labelled Sentences"
            ),
            split="Amazon/Yelp test; saved and not used for selection",
            timestamp=generation_timestamp,
            source_path=active["generation_root"]
            / "external_short_text_evaluation.json",
            model_generation=generation_id,
            source_run_id=generation_id,
            metrics_by_model=external_splits.get("test", {}),
            sample_count=partition_counts.get("test", {}).get("rows"),
            limitations=active_external.get("limitations", []),
            source_artifacts={
                key: str(path) for key, path in active["model_directories"].items()
            },
        ),
    }

    auto_review_count = phase15_auto.get("unique_source_reviews")
    auto_variant_count = phase15_auto.get("transformation_type_rows")
    tracks["phase15_automatic_robustness_historical"] = _make_track(
        dataset_id="IMDb Phase 15 label-preserving perturbation variants",
        split="paired per-review majority vote over automatic variants",
        timestamp=phase15_metadata.get("created_at_utc"),
        source_path=phase15_root / "automatic_perturbations/metrics.json",
        model_generation=None,
        source_run_id=phase15_root.name,
        metrics_by_model={
            key: {
                "accuracy": phase15_auto.get("overall_review_level", {})
                .get(key, {})
                .get("robustness_accuracy_review_level"),
                "precision": phase15_auto.get("overall_review_level", {})
                .get(key, {})
                .get("precision_review_level"),
                "recall": phase15_auto.get("overall_review_level", {})
                .get(key, {})
                .get("recall_review_level"),
                "f1": phase15_auto.get("overall_review_level", {})
                .get(key, {})
                .get("robustness_f1_review_level"),
                "true_negative": phase15_auto.get("overall_review_level", {})
                .get(key, {})
                .get("true_negative"),
                "false_positive": phase15_auto.get("overall_review_level", {})
                .get(key, {})
                .get("false_positive"),
                "false_negative": phase15_auto.get("overall_review_level", {})
                .get(key, {})
                .get("false_negative"),
                "true_positive": phase15_auto.get("overall_review_level", {})
                .get(key, {})
                .get("true_positive"),
            }
            for key in MODEL_KEYS
        },
        sample_count=auto_review_count,
        limitations=[
            "Metrics are paired review-level majorities; variants are not treated as "
            "independent reviews.",
            "The Phase 15 source artifacts predate the active recovery generation.",
            "Automatic edits were screened but are not guaranteed to preserve meaning.",
        ],
        source_artifacts=phase15_artifacts,
    )
    for key in MODEL_KEYS:
        robustness_metric = tracks["phase15_automatic_robustness_historical"][
            "metrics_by_model"
        ][key]
        robustness_metric["variant_count"] = auto_variant_count
        robustness_metric["prediction_consistency"] = (
            phase15_auto.get("overall_review_level", {})
            .get(key, {})
            .get("prediction_consistency")
        )
        robustness_metric["prediction_flip_rate"] = (
            phase15_auto.get("overall_review_level", {})
            .get(key, {})
            .get("prediction_flip_rate")
        )

    semantic_models = phase15_semantic.get("models", {})
    tracks["phase15_semantic_challenge_historical"] = _make_track(
        dataset_id="OpenBench author-created semantic challenge set",
        split="Phase 15 separate challenge set",
        timestamp=phase15_metadata.get("created_at_utc"),
        source_path=phase15_root / "semantic_challenge/metrics.json",
        model_generation=None,
        source_run_id=phase15_root.name,
        metrics_by_model=semantic_models,
        sample_count=phase15_semantic.get("dataset_summary", {}).get("sample_count"),
        limitations=[
            "Labels are author-intended and have not been independently adjudicated.",
            "The Phase 15 source artifacts predate the active recovery generation.",
        ],
        source_artifacts=phase15_artifacts,
    )

    sources = {
        "phase7": {
            "path": str(phase7_path.resolve()),
            "timestamp": phase7_metadata.get("created_at_utc"),
        },
        "phase12": {
            "path": str(phase12_path.resolve()),
            "timestamp": phase12.get("created_at_utc"),
        },
        "phase13": {
            "path": str(phase13_path.resolve()),
            "timestamp": phase13_metadata.get("created_at_utc"),
        },
        "phase14": None
        if phase14 is None
        else {
            "path": str((phase14[0] / "metadata/run.json").resolve()),
            "timestamp": phase14[1].get("created_at_utc"),
            "metrics_available": False,
            "validation_only": phase14[1].get("validation_only"),
        },
        "phase15": {
            "path": str((phase15_root / "metrics.json").resolve()),
            "timestamp": phase15_metadata.get("created_at_utc"),
        },
    }
    return {
        "generation_id": generation_id,
        "tracks": tracks,
        "source_inventory": sources,
    }


def add_diagnostic_track(
    quality: dict[str, Any],
    prediction_rows: list[dict[str, Any]],
    active: dict[str, Any],
) -> dict[str, Any]:
    """Add current active-generation diagnostics without scoring semantic phrases."""
    basic = [row for row in prediction_rows if row["example_group"] == "basic"]
    model_metrics = {}
    for model_id in MODEL_KEYS:
        rows = [row for row in basic if row["model_id"] == model_id]
        metric = evaluate_binary_predictions(
            [row["expected_label"] for row in rows],
            [row["predicted_label"] for row in rows],
        )
        metric.update(
            {
                "correct_count": sum(
                    row["expected_label"] == row["predicted_label"] for row in rows
                ),
                "error_count": sum(
                    row["expected_label"] != row["predicted_label"] for row in rows
                ),
            }
        )
        model_metrics[model_id] = metric
    quality["tracks"]["short_text_diagnostic_active_generation"] = _make_track(
        dataset_id="OpenBench fixed short-text diagnostic suite",
        split=(
            "12 basic examples; four semantic examples reported separately "
            "without scoring"
        ),
        timestamp=None,
        source_path=active["generation_root"] / "diagnostic_predictions.csv",
        model_generation=active["generation_id"],
        source_run_id=active["generation_id"],
        metrics_by_model=model_metrics,
        sample_count=len(basic) // len(MODEL_KEYS),
        limitations=[
            "Small diagnostic set, not a model-selection set or estimate of "
            "generalization.",
            "Semantic example labels are intentionally left unscored in this track.",
        ],
        source_artifacts={
            key: str(path) for key, path in active["model_directories"].items()
        },
    )
    return quality


def summarize_quality(quality: dict[str, Any]) -> dict[str, Any]:
    """Create track-separated leaderboards with explicit ranking criteria."""
    tracks = quality["tracks"]

    def ranking(
        track_id: str, metric_name: str, *, minimize: bool = False
    ) -> dict[str, Any]:
        track = tracks[track_id]
        values = {
            key: track["metrics_by_model"][key].get(metric_name)
            for key in MODEL_KEYS
            if track["metrics_by_model"][key].get("available")
            and track["metrics_by_model"][key].get(metric_name) is not None
        }
        if not values:
            return {"track_id": track_id, "metric": metric_name, "leaders": []}
        best = min(values.values()) if minimize else max(values.values())
        return {
            "track_id": track_id,
            "metric": metric_name,
            "ranking_direction": "minimize" if minimize else "maximize",
            "value": best,
            "leaders": [key for key, value in values.items() if value == best],
            "source_generation": track.get("source_model_generation"),
            "source_run_id": track.get("source_evaluation_run_id"),
            "dataset_id": track["dataset_id"],
            "split": track["split"],
        }

    return {
        "generation_id": quality.get("generation_id"),
        "ranking_method": (
            "Track-specific recorded metric only. No score combines datasets; "
            "the official IMDb test is descriptive and is not used for selection."
        ),
        "best_imdb_validation_accuracy": ranking(
            "imdb_validation_recovery_selection", "accuracy"
        ),
        "best_recorded_imdb_test_accuracy": ranking(
            "imdb_official_test_active_generation_saved", "accuracy"
        ),
        "best_uci_short_text_test_f1": ranking(
            "uci_short_text_test_active_generation_saved", "f1"
        ),
        "best_semantic_challenge_f1": ranking(
            "phase15_semantic_challenge_historical", "f1"
        ),
        "fewest_short_text_diagnostic_errors": ranking(
            "short_text_diagnostic_active_generation", "error_count", minimize=True
        ),
        "most_consistent_automatic_robustness": ranking(
            "phase15_automatic_robustness_historical", "prediction_consistency"
        ),
        "unavailable_values_are_null": True,
        "semantic_labels_independently_adjudicated": False,
        "official_test_is_pristine": False,
    }
