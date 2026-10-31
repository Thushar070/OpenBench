"""Validated, read-only access to saved OpenBench experiment summaries."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

METRIC_NAMES = ("accuracy", "precision", "recall", "f1")
MODEL_NAMES = {
    "logistic_regression": "Logistic Regression",
    "textcnn": "TextCNN",
}


@dataclass(frozen=True)
class ExperimentSummary:
    """Small experiment row for dashboard tables; contains no review text."""

    phase: int
    name: str
    model: str
    split: str
    run_id: str
    accuracy: float | None
    f1: float | None


@dataclass(frozen=True)
class DashboardData:
    """Validated values needed by the dashboard presentation layer."""

    models: tuple[str, ...]
    experiments: tuple[ExperimentSummary, ...]
    test_sample_count: int | None
    class_distribution: dict[str, int]
    test_metrics: dict[str, dict[str, float]]
    confusion_matrices: dict[str, list[list[int]]]
    validation_metrics: dict[str, dict[str, float]]
    test_results_verified: bool
    best_test_accuracy: float | None
    result_directory: str | None
    issues: tuple[str, ...]

    @property
    def experiment_count(self) -> int:
        """Count saved runs, grouping model rows from the same comparison."""
        return len({(row.phase, row.run_id) for row in self.experiments})


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}.")
    return value


def _latest_json(directory: Path, filename: str) -> tuple[Path, dict[str, Any]] | None:
    candidates: list[tuple[str, Path, dict[str, Any]]] = []
    for path in directory.glob(f"*/{filename}"):
        try:
            data = _read_object(path)
        except OSError, json.JSONDecodeError, ValueError:
            continue
        timestamp = data.get("created_at_utc", data.get("evaluation_timestamp_utc", ""))
        candidates.append((str(timestamp), path, data))
    if not candidates:
        return None
    _, path, data = max(candidates, key=lambda item: (item[0], item[1].parent.name))
    return path, data


def _metrics(value: Any, name: str) -> dict[str, float]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} metrics are missing.")
    result: dict[str, float] = {}
    for metric_name in METRIC_NAMES:
        raw = value.get(metric_name)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"{name} metric {metric_name!r} is missing or invalid.")
        score = float(raw)
        if not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError(f"{name} metric {metric_name!r} is outside [0, 1].")
        result[metric_name] = score
    return result


def _confusion_matrix(value: Any, name: str, expected_total: int) -> list[list[int]]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(not isinstance(row, list) or len(row) != 2 for row in value)
    ):
        raise ValueError(f"{name} confusion matrix must be 2 by 2.")
    matrix: list[list[int]] = []
    for row in value:
        normalized_row: list[int] = []
        for count in row:
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError(
                    f"{name} confusion counts must be non-negative integers."
                )
            normalized_row.append(count)
        matrix.append(normalized_row)
    if sum(sum(row) for row in matrix) != expected_total:
        raise ValueError(f"{name} confusion matrix does not match the test row count.")
    return matrix


def _experiment(
    phase: int,
    name: str,
    model: str,
    split: str,
    run_id: str,
    raw_metrics: Any,
) -> ExperimentSummary:
    try:
        values = _metrics(raw_metrics, name)
    except ValueError:
        values = {}
    return ExperimentSummary(
        phase=phase,
        name=name,
        model=model,
        split=split,
        run_id=run_id,
        accuracy=values.get("accuracy"),
        f1=values.get("f1"),
    )


def _model_runs(
    outputs: Path,
    folder_name: str,
    model_name: str,
    phase: int,
) -> tuple[str | None, ExperimentSummary | None]:
    discovered = _latest_json(outputs / folder_name, "metadata.json")
    if discovered is None:
        return None, None
    path, metadata = discovered
    run_id = str(metadata.get("experiment_id", path.parent.name))
    metric_key = "validation_metrics" if phase == 3 else "best_validation_metrics"
    summary = _experiment(
        phase=phase,
        name="Baseline Model" if phase == 3 else "TextCNN Training",
        model=model_name,
        split="Validation",
        run_id=run_id,
        raw_metrics=metadata.get(metric_key),
    )
    return run_id, summary


def _evaluation_summary(
    outputs: Path,
) -> tuple[
    Path | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
    str | None,
]:
    discovered = _latest_json(outputs / "final_evaluation", "final_results.json")
    if discovered is None:
        return None, None, None, "No saved Phase 7 final evaluation was found."
    result_path, results = discovered
    metadata_path = result_path.parent / "metadata.json"
    try:
        metadata = _read_object(metadata_path)
        hashes_before = metadata.get("artifact_sha256_before")
        hashes_after = metadata.get("artifact_sha256_after")
        if (
            results.get("official_test_evaluated") is not True
            or metadata.get("official_test_evaluated") is not True
            or metadata.get("training_started") is not False
            or metadata.get("preprocessing_refitted") is not False
            or metadata.get("test_based_model_selection") is not False
            or metadata.get("artifacts_unchanged") is not True
            or not isinstance(hashes_before, dict)
            or not hashes_before
            or hashes_before != hashes_after
            or results.get("evaluation_id") != metadata.get("evaluation_id")
            or metadata.get("dataset_split") != "test"
        ):
            raise ValueError("Phase 7 evaluation integrity metadata did not validate.")
        sample_count = results.get("sample_count")
        if (
            isinstance(sample_count, bool)
            or not isinstance(sample_count, int)
            or sample_count < 1
        ):
            raise ValueError("Phase 7 test sample count is invalid.")
        class_distribution = results.get("class_distribution")
        if (
            not isinstance(class_distribution, dict)
            or sum(class_distribution.values()) != sample_count
        ):
            raise ValueError("Phase 7 test class counts do not match its sample count.")
        dataset_validation = metadata.get("dataset_validation", {})
        alignment = results.get("prediction_alignment", {})
        if (
            not isinstance(dataset_validation, dict)
            or dataset_validation.get("record_source_verified") is not True
            or dataset_validation.get("sample_count") != sample_count
            or not isinstance(alignment, dict)
            or alignment.get("same_test_records") is not True
            or alignment.get("same_ground_truth_labels") is not True
            or alignment.get("same_original_order") is not True
            or alignment.get("baseline_prediction_count") != sample_count
            or alignment.get("textcnn_prediction_count") != sample_count
        ):
            raise ValueError(
                "Phase 7 test record and prediction alignment did not validate."
            )
        metrics_source = results.get("metrics")
        if not isinstance(metrics_source, dict):
            raise ValueError("Phase 7 model metrics are missing.")
        matrices: dict[str, list[list[int]]] = {}
        metrics: dict[str, dict[str, float]] = {}
        for key in MODEL_NAMES:
            if key not in metrics_source:
                raise ValueError(f"Phase 7 metrics for {key} are missing.")
            metrics[key] = _metrics(metrics_source[key], f"Phase 7 {key}")
            matrices[key] = _confusion_matrix(
                metrics_source[key].get("confusion_matrix"),
                f"Phase 7 {key}",
                sample_count,
            )
        return result_path.parent, results, metadata, None
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as error:
        return result_path.parent, None, None, str(error)


def load_dashboard_data(outputs_directory: str | Path = "outputs") -> DashboardData:
    """Discover experiment summaries without loading data or model checkpoints."""
    outputs = Path(outputs_directory)
    issues: list[str] = []
    experiment_rows: list[ExperimentSummary] = []

    baseline_run, baseline_summary = _model_runs(
        outputs, "baseline", MODEL_NAMES["logistic_regression"], 3
    )
    textcnn_run, textcnn_summary = _model_runs(
        outputs, "textcnn", MODEL_NAMES["textcnn"], 5
    )
    experiment_rows.extend(
        row for row in (baseline_summary, textcnn_summary) if row is not None
    )

    comparison = _latest_json(outputs / "comparison", "comparison.json")
    if comparison is not None:
        comparison_path, comparison_data = comparison
        comparison_metrics = comparison_data.get("metrics", {})
        sample_count = comparison_data.get("sample_count")
        if comparison_data.get("validation_only") is True:
            for key, label in MODEL_NAMES.items():
                experiment_rows.append(
                    _experiment(
                        phase=6,
                        name="Model Comparison",
                        model=label,
                        split=f"Validation ({sample_count:,})"
                        if isinstance(sample_count, int)
                        else "Validation",
                        run_id=str(
                            comparison_data.get(
                                "comparison_id", comparison_path.parent.name
                            )
                        ),
                        raw_metrics=comparison_metrics.get(key),
                    )
                )
    else:
        issues.append("No saved Phase 6 model comparison was found.")

    result_directory, result_data, _, evaluation_issue = _evaluation_summary(outputs)
    if evaluation_issue:
        issues.append(evaluation_issue)

    test_metrics: dict[str, dict[str, float]] = {}
    confusion_matrices: dict[str, list[list[int]]] = {}
    class_distribution: dict[str, int] = {}
    test_sample_count: int | None = None
    if result_data is not None:
        test_sample_count = int(result_data["sample_count"])
        class_distribution = {
            str(label): int(count)
            for label, count in result_data["class_distribution"].items()
        }
        for key in MODEL_NAMES:
            test_metrics[key] = _metrics(result_data["metrics"][key], f"Phase 7 {key}")
            confusion_matrices[key] = _confusion_matrix(
                result_data["metrics"][key]["confusion_matrix"],
                f"Phase 7 {key}",
                test_sample_count,
            )
            experiment_rows.append(
                _experiment(
                    phase=7,
                    name="Final Evaluation",
                    model=MODEL_NAMES[key],
                    split=f"Official Test ({test_sample_count:,})",
                    run_id=str(result_data.get("evaluation_id", "Phase 7")),
                    raw_metrics=result_data["metrics"][key],
                )
            )

    validation_metrics: dict[str, dict[str, float]] = {}
    if comparison is not None:
        for key in MODEL_NAMES:
            try:
                validation_metrics[key] = _metrics(
                    comparison[1].get("metrics", {}).get(key),
                    f"Phase 6 {key}",
                )
            except ValueError as error:
                issues.append(str(error))

    accuracies = [values["accuracy"] for values in test_metrics.values()]
    return DashboardData(
        models=tuple(
            name
            for run_id, name in (
                (baseline_run, MODEL_NAMES["logistic_regression"]),
                (textcnn_run, MODEL_NAMES["textcnn"]),
            )
            if run_id is not None
        ),
        experiments=tuple(
            sorted(experiment_rows, key=lambda row: (row.phase, row.model))
        ),
        test_sample_count=test_sample_count,
        class_distribution=class_distribution,
        test_metrics=test_metrics,
        confusion_matrices=confusion_matrices,
        validation_metrics=validation_metrics,
        test_results_verified=result_data is not None,
        best_test_accuracy=max(accuracies) if accuracies else None,
        result_directory=(
            str(result_directory.relative_to(outputs))
            if result_directory is not None
            else None
        ),
        issues=tuple(issues),
    )
