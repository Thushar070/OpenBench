"""Read-only, integrity-aware access to OpenBench experiment summaries."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, Callable

import pandas as pd

MODEL_KEYS = ("logistic_regression", "textcnn", "rnn", "lstm", "gru")
MODEL_LABELS = {
    "logistic_regression": "TF-IDF + Logistic Regression",
    "textcnn": "TextCNN",
    "rnn": "Vanilla RNN",
    "lstm": "LSTM",
    "gru": "GRU",
}


def _json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path.name}")
    return value


def _records(path: Path, *, limit: int = 100) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    frame = pd.read_csv(path, nrows=limit)
    frame = frame.astype(object).where(pd.notna(frame), None)
    return frame.to_dict(orient="records")


class ArtifactRepository:
    """Discover verified experiment summaries beneath one fixed project root."""

    def __init__(self, project_root: str | Path | None = None) -> None:
        self.root = (
            Path(project_root).resolve()
            if project_root is not None
            else Path(__file__).resolve().parents[3]
        )
        self.outputs = self.root / "outputs"
        self._cache: dict[str, Any] = {}
        self._lock = threading.RLock()

    def refresh(self) -> None:
        """Clear read-only summaries so local artifact changes become visible."""
        with self._lock:
            self._cache.clear()

    def _cached(self, key: str, loader: Callable[[], Any]) -> Any:
        with self._lock:
            if key not in self._cache:
                self._cache[key] = loader()
            return self._cache[key]

    def _phase12(self) -> tuple[Path, dict[str, Any]]:
        candidates = sorted(
            (self.outputs / "neural_comparison").glob(
                "imdb-neural-comparison-*/comparison.json"
            )
        )
        for path in reversed(candidates):
            try:
                data = _json_object(path)
                artifacts = data.get("source_artifacts", {})
                paths = [
                    Path(
                        artifacts["baseline" if key == "logistic_regression" else key]
                    ).resolve()
                    for key in MODEL_KEYS
                ]
                if (
                    data.get("validation_only") is True
                    and data.get("official_test_evaluated") is False
                    and set(data.get("models", ())) == set(MODEL_KEYS)
                    and all(
                        item.is_relative_to(self.outputs.resolve()) for item in paths
                    )
                    and all(item.is_dir() for item in paths)
                ):
                    return path, data
            except OSError, KeyError, TypeError, json.JSONDecodeError, ValueError:
                continue
        raise FileNotFoundError("No complete Phase 12 validation comparison was found.")

    def _phase13(self) -> tuple[Path, dict[str, Any], dict[str, Any]]:
        candidates = sorted(
            (self.outputs / "final_evaluation").glob(
                "imdb-phase13-final-evaluation-*/comparison.json"
            )
        )
        for comparison_path in reversed(candidates):
            try:
                directory = comparison_path.parent
                comparison = _json_object(comparison_path)
                metadata = _json_object(directory / "metadata.json")
                if (
                    set(comparison.get("models", ())) == set(MODEL_KEYS)
                    and set(comparison.get("metrics", {})) == set(MODEL_KEYS)
                    and comparison.get("sample_count") == 25_000
                    and metadata.get("official_test_evaluated") is True
                    and metadata.get("artifacts_unchanged") is True
                    and metadata.get("artifact_sha256_before")
                    == metadata.get("artifact_sha256_after")
                    and metadata.get("model_training_started") is False
                    and metadata.get("preprocessing_refitted") is False
                ):
                    return comparison_path, comparison, metadata
            except OSError, KeyError, TypeError, json.JSONDecodeError, ValueError:
                continue
        raise FileNotFoundError("No integrity-verified Phase 13 evaluation was found.")

    def _phase14(self) -> tuple[Path, dict[str, Any]] | None:
        candidates = sorted(
            (self.outputs / "interpretability").glob(
                "imdb-interpretability-*/metadata/run.json"
            )
        )
        for path in reversed(candidates):
            try:
                metadata = _json_object(path)
                if (
                    metadata.get("phase") == 14
                    and metadata.get("validation_only") is True
                    and metadata.get("official_test_split_loaded") is False
                    and metadata.get("model_training_performed") is False
                    and metadata.get("source_artifacts_unchanged_sha256") is True
                ):
                    return path.parent.parent, metadata
            except OSError, TypeError, json.JSONDecodeError, ValueError:
                continue
        return None

    def _phase15(self) -> tuple[Path, dict[str, Any]] | None:
        candidates = sorted(
            (self.outputs / "robustness").glob("imdb-robustness-*/metadata.json")
        )
        for path in reversed(candidates):
            try:
                metadata = _json_object(path)
                run = path.parent
                if (
                    metadata.get("phase") == 15
                    and metadata.get("official_test_dataset_loaded") is False
                    and metadata.get("source_artifacts_unchanged") is True
                    and (run / "metrics.json").is_file()
                    and (
                        run / "automatic_perturbations/transformation_summary.csv"
                    ).is_file()
                    and (run / "semantic_challenge/category_summary.csv").is_file()
                ):
                    return run, metadata
            except OSError, TypeError, json.JSONDecodeError, ValueError:
                continue
        return None

    def _phase16(self) -> tuple[Path, dict[str, Any]] | None:
        candidates = sorted(
            (self.outputs / "efficiency").glob("imdb-efficiency-*/metadata.json")
        )
        for path in reversed(candidates):
            try:
                metadata = _json_object(path)
                run = path.parent
                if metadata.get("phase") != 16:
                    continue
                if metadata.get("source_artifacts_unchanged") is not True:
                    continue
                if metadata.get("official_test_reviews_loaded") is not False:
                    continue

                audit_files = (
                    "benchmark_config.json",
                    "benchmark.json",
                    "metrics.json",
                    "comparison.json",
                    "model_quality_summary.json",
                    "loading_times.csv",
                    "latency_summary.csv",
                    "batch_benchmark.csv",
                    "memory_summary.csv",
                    "efficiency_quality_comparison.csv",
                    "report.md",
                )
                legacy_files = (
                    "benchmark.json",
                    "metrics.json",
                    "comparison.json",
                    "model_summary.csv",
                    "batch_summary.csv",
                )
                if all((run / name).is_file() for name in audit_files) or all(
                    (run / name).is_file() for name in legacy_files
                ):
                    return run, metadata
            except OSError, TypeError, json.JSONDecodeError, ValueError:
                continue
        return None

    def models(self) -> dict[str, Any]:
        return self._cached("models", self._load_models)

    def _load_models(self) -> dict[str, Any]:
        from deeptext.experiments.phase_16_quality import discover_active_generation

        try:
            active = discover_active_generation(self.root)
        except (FileNotFoundError, OSError, TypeError, ValueError) as error:
            return {
                "models": [],
                "count": 0,
                "active_generation_id": None,
                "issue": str(error),
            }

        saved_metrics_path = active["generation_root"] / "official_test_metrics.json"
        try:
            saved_metrics = _json_object(saved_metrics_path)
        except FileNotFoundError, OSError, TypeError, json.JSONDecodeError:
            saved_metrics = {}

        benchmark_models: dict[str, Any] = {}
        selected = self._phase16()
        if selected is not None:
            try:
                benchmark = _json_object(selected[0] / "benchmark.json")
                if benchmark.get("active_generation_id") == active["generation_id"]:
                    benchmark_models = benchmark.get("models", {})
            except OSError, TypeError, json.JSONDecodeError, ValueError:
                benchmark_models = {}

        result = []
        for model_id in MODEL_KEYS:
            artifact = active["model_directories"][model_id]
            metadata = _json_object(artifact / "metadata.json")
            model_benchmark = benchmark_models.get(model_id, {})
            evaluation = saved_metrics.get(model_id, {})
            artifact_size = sum(
                path.stat().st_size for path in artifact.rglob("*") if path.is_file()
            )
            result.append(
                {
                    "id": model_id,
                    "name": MODEL_LABELS[model_id],
                    "architecture": metadata.get("model_name", MODEL_LABELS[model_id]),
                    "generation_id": active["generation_id"],
                    "available": True,
                    "artifact_size_bytes": artifact_size,
                    "parameter_count": model_benchmark.get("parameter_count"),
                    "test_accuracy": evaluation.get("accuracy"),
                    "test_f1": evaluation.get("f1"),
                    "test_metrics_are_saved": bool(evaluation),
                }
            )
        return {
            "models": result,
            "count": len(result),
            "active_generation_id": active["generation_id"],
            "official_test_sample_count": saved_metrics.get("sample_count"),
            "official_test_class_distribution": saved_metrics.get("class_counts", {}),
            "official_test_metric_source": (
                "active_generation/official_test_metrics.json"
                if saved_metrics
                else None
            ),
            "official_test_metrics_are_historical": True,
        }

    def overview(self) -> dict[str, Any]:
        return self._cached("overview", self._load_overview)

    def _load_overview(self) -> dict[str, Any]:
        test_comparison = self.comparison("official_test")
        validation_comparison = self.comparison("validation")
        model_data = self.models()
        models = model_data.get("models", [])
        efficiency = self.efficiency()
        completed = self._completed_phase_count()
        quality_summary = efficiency.get("quality_summary", {})
        validation_best = quality_summary.get("best_imdb_validation_accuracy", {})
        active_test_best = quality_summary.get("best_recorded_imdb_test_accuracy", {})
        saved_test_accuracy = active_test_best.get("value")
        if saved_test_accuracy is None:
            saved_test_accuracy = max(
                (
                    model.get("test_accuracy")
                    for model in models
                    if isinstance(model.get("test_accuracy"), int | float)
                ),
                default=None,
            )
        return {
            "project": {
                "name": "OpenBench",
                "description": "Reproducible IMDb sentiment benchmarking and analysis.",
                "completed_phase_count": completed,
                "phase_8_status": "paused",
            },
            "dataset": {
                "name": "IMDb Large Movie Review Dataset",
                "task": "Binary sentiment classification",
                "official_test_review_count": model_data.get(
                    "official_test_sample_count"
                ),
                "class_distribution": model_data.get(
                    "official_test_class_distribution", {}
                ),
                "official_test_metrics_are_saved_aggregate_results": True,
            },
            "model_count": len(models),
            "experiment_count": len(self.experiments()),
            "best_validation_accuracy": validation_best.get("value"),
            "best_test_accuracy": saved_test_accuracy,
            "models": models,
            "validation_source": validation_comparison["metric_source"],
            "test_source": (
                "Active-generation saved official-test aggregate; previously used"
                if models
                else test_comparison["metric_source"]
            ),
            "efficiency_available": efficiency.get("available", False),
            "active_generation_id": models[0].get("generation_id") if models else None,
            "quality_summary": quality_summary,
            "issues": test_comparison.get("issues", []),
        }

    def _completed_phase_count(self) -> int:
        path = self.root / "docs/plan.md"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return 0
        explicit = re.search(r"\*\*Completed phases:\*\*\s*(\d+)\s*total", text, re.I)
        if explicit:
            return int(explicit.group(1))
        sections = re.split(r"(?=^### Phase \d+\b)", text, flags=re.MULTILINE)
        count = 0
        for section in sections:
            if not section.startswith("### Phase "):
                continue
            if "paused" in section[:500].casefold():
                continue
            if (
                "**Complete**" in section[:1600]
                or "complete" in section[:500].casefold()
            ):
                count += 1
        return count

    def comparison(self, split: str = "official_test") -> dict[str, Any]:
        if split not in {"validation", "official_test"}:
            raise ValueError("split must be validation or official_test.")
        return self._cached(f"comparison:{split}", lambda: self._load_comparison(split))

    def _load_comparison(self, split: str) -> dict[str, Any]:
        try:
            phase12_path, phase12 = self._phase12()
            phase13_path, phase13, _ = self._phase13()
        except FileNotFoundError as error:
            return {"dataset_split": split, "models": [], "issues": [str(error)]}
        if split == "validation":
            report = phase12
            metrics = phase12["metrics"]
            source_path = phase12_path
            source_name = "Phase 12 saved validation-only comparison"
            profiles = phase12.get("model_profiles", {})
            size_data = phase12.get("artifact_sizes", {})
        else:
            report = phase13
            metrics = phase13["metrics"]
            source_path = phase13_path
            source_name = "Phase 13 verified official test evaluation"
            profiles = phase12.get("model_profiles", {})
            size_data = phase12.get("artifact_sizes", {})
        result_models = []
        for key in MODEL_KEYS:
            metric = metrics[key]
            profile = profiles.get(key, {})
            result_models.append(
                {
                    "id": key,
                    "name": MODEL_LABELS[key],
                    "accuracy": metric["accuracy"],
                    "precision": metric["precision"],
                    "recall": metric["recall"],
                    "f1": metric["f1"],
                    "false_positive": metric.get("false_positive"),
                    "false_negative": metric.get("false_negative"),
                    "confusion_matrix": metric.get("confusion_matrix"),
                    "per_class": metric.get("per_class", {}),
                    "parameter_count": profile.get("parameter_count"),
                    "artifact_size_bytes": size_data.get(key, {}).get("total_bytes"),
                    "historical_training_duration_seconds": profile.get(
                        "training_duration_seconds"
                    ),
                    "training_duration_is_historical": True,
                }
            )
        return {
            "dataset_split": split,
            "metric_source": source_name,
            "sample_count": report.get("sample_count"),
            "class_distribution": report.get("class_distribution", {}),
            "models": result_models,
            "pairwise_prediction_agreement": report.get(
                "pairwise_prediction_agreement", {}
            ),
            "validation_and_test_scores_are_separate": True,
            "official_test_raw_reviews_included": False,
            "issue": None,
            "run_id": source_path.parent.name,
        }

    def final_evaluation(self) -> dict[str, Any]:
        return self._cached("final_evaluation", self._load_final_evaluation)

    def _load_final_evaluation(self) -> dict[str, Any]:
        try:
            path, comparison, metadata = self._phase13()
        except FileNotFoundError as error:
            return {"available": False, "issue": str(error), "models": []}
        directory = path.parent
        metrics_file = _json_object(directory / "metrics.json")
        class_distribution = metadata.get("dataset_validation", {}).get(
            "class_distribution", {}
        )
        selected_examples = _records(
            directory / "error_analysis/selected_examples.csv", limit=40
        )
        models = []
        for key in MODEL_KEYS:
            metric = comparison["metrics"][key]
            report = metrics_file.get("classification_reports", {}).get(key, {})
            matrix = directory / "confusion_matrices" / f"{key}.json"
            matrix_data = _json_object(matrix) if matrix.is_file() else {}
            models.append(
                {
                    "id": key,
                    "name": MODEL_LABELS[key],
                    "metrics": metric,
                    "classification_report": report,
                    "confusion_matrix": matrix_data,
                }
            )
        return {
            "available": True,
            "dataset_split": "official_test",
            "sample_count": comparison["sample_count"],
            "class_distribution": class_distribution,
            "models": models,
            "pairwise_prediction_agreement": comparison.get(
                "pairwise_prediction_agreement", {}
            ),
            "selected_error_examples": selected_examples,
            "integrity_verified": metadata["artifacts_unchanged"],
            "test_based_model_selection": metadata["test_based_model_selection"],
            "raw_test_prediction_rows_exposed": False,
            "run_id": path.parent.name,
        }

    def experiments(self) -> list[dict[str, Any]]:
        return self._cached("experiments", self._load_experiments)

    def _load_experiments(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []

        def add_metadata_runs(
            phase: int,
            folder: str,
            pattern: str,
            metadata_relative: str = "metadata.json",
        ) -> None:
            for metadata_path in sorted((self.outputs / folder).glob(pattern)):
                try:
                    metadata = _json_object(metadata_path)
                except OSError, TypeError, json.JSONDecodeError, ValueError:
                    metadata = {}
                directory = metadata_path.parent
                if metadata_relative != "metadata.json":
                    directory = metadata_path.parent.parent
                rows.append(
                    self._experiment_row(phase, directory, metadata, metadata_path.name)
                )

        add_metadata_runs(3, "baseline", "*/metadata.json")
        add_metadata_runs(5, "textcnn", "*/metadata.json")
        for phase, folder, pattern, result_file in (
            (6, "comparison", "imdb-comparison-*", "comparison.json"),
            (12, "neural_comparison", "imdb-neural-comparison-*", "comparison.json"),
            (
                13,
                "final_evaluation",
                "imdb-phase13-final-evaluation-*",
                "comparison.json",
            ),
            (7, "final_evaluation", "imdb-final-evaluation-*", "final_results.json"),
        ):
            for directory in sorted((self.outputs / folder).glob(pattern)):
                path = directory / result_file
                if not path.is_file():
                    continue
                try:
                    result = _json_object(path)
                except OSError, TypeError, json.JSONDecodeError, ValueError:
                    result = {}
                metadata_path = directory / "metadata.json"
                try:
                    metadata = _json_object(metadata_path)
                except OSError, TypeError, json.JSONDecodeError, ValueError:
                    metadata = {}
                rows.append(
                    self._experiment_row(
                        phase, directory, metadata, result_file, result
                    )
                )
        for path in sorted(
            (self.outputs / "interpretability").glob(
                "imdb-interpretability-*/metadata/run.json"
            )
        ):
            try:
                metadata = _json_object(path)
            except OSError, TypeError, json.JSONDecodeError, ValueError:
                metadata = {}
            rows.append(
                self._experiment_row(
                    14, path.parent.parent, metadata, "metadata/run.json"
                )
            )
        for path in sorted(
            (self.outputs / "robustness").glob("imdb-robustness-*/metadata.json")
        ):
            try:
                metadata = _json_object(path)
            except OSError, TypeError, json.JSONDecodeError, ValueError:
                metadata = {}
            rows.append(
                self._experiment_row(15, path.parent, metadata, "metadata.json")
            )
        for path in sorted(
            (self.outputs / "efficiency").glob("imdb-efficiency-*/metadata.json")
        ):
            try:
                metadata = _json_object(path)
            except OSError, TypeError, json.JSONDecodeError, ValueError:
                metadata = {}
            rows.append(
                self._experiment_row(16, path.parent, metadata, "metadata.json")
            )
        return sorted(
            rows, key=lambda row: (row["phase"], row["timestamp"] or ""), reverse=True
        )

    @staticmethod
    def _experiment_row(
        phase: int,
        directory: Path,
        metadata: dict[str, Any],
        primary_artifact: str,
        report: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        report = report or {}
        metrics = report.get("metrics", {})
        if phase == 3:
            metrics = metadata.get("validation_metrics", {})
        elif phase == 5:
            metrics = metadata.get("best_validation_metrics", {})
        elif phase in {6, 12, 13}:
            metrics = report.get("metrics", {})
        if phase in {6, 12, 13} and isinstance(metrics, dict):
            scores = {
                key: {
                    "accuracy": values.get("accuracy"),
                    "f1": values.get("f1"),
                }
                for key, values in metrics.items()
                if isinstance(values, dict)
            }
        else:
            scores = {
                "accuracy": metrics.get("accuracy")
                if isinstance(metrics, dict)
                else None,
                "f1": metrics.get("f1") if isinstance(metrics, dict) else None,
            }
        model = metadata.get("architecture") or metadata.get("model_name")
        if not model:
            model = {
                3: "Logistic Regression",
                5: "TextCNN",
                6: "Logistic Regression, TextCNN",
                7: "Logistic Regression, TextCNN",
                12: "Five model comparison",
                13: "Five frozen models",
                14: "Five model explanations",
                15: "Five model robustness evaluation",
                16: "Five model efficiency benchmark",
            }.get(phase, "Unknown")
        split = metadata.get("dataset_split") or metadata.get("split", {}).get("source")
        if phase in {3, 5, 6, 12, 14, 15}:
            split = "validation"
        elif phase in {7, 13}:
            split = "official_test"
        elif phase == 16:
            split = metadata.get("dataset_split_used_for_timing", "validation")
        available_files = (
            [
                child.name
                for child in directory.iterdir()
                if child.is_file() and child.name != "test_predictions.csv"
            ]
            if directory.is_dir()
            else []
        )
        return {
            "id": directory.name,
            "phase": phase,
            "name": directory.name.replace("-", " "),
            "model": model,
            "dataset_split": split or "Unavailable in saved metadata",
            "timestamp": metadata.get("created_at_utc")
            or metadata.get("evaluation_timestamp_utc"),
            "configuration": metadata.get("model_configuration")
            or metadata.get("model_config")
            or metadata.get("analysis_configuration")
            or metadata.get("benchmark_configuration"),
            "metrics": scores,
            "artifact_availability": {
                "primary_summary": (directory / primary_artifact).is_file(),
                "files": available_files,
            },
            "integrity_verified": metadata.get("artifacts_unchanged")
            or metadata.get("source_artifacts_unchanged")
            or metadata.get("source_artifacts_unchanged_sha256"),
            "missing_metadata_fields": [
                field
                for field in ("created_at_utc", "dataset_split")
                if not metadata.get(field)
            ],
        }

    def experiment_detail(self, run_id: str) -> dict[str, Any] | None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", run_id):
            return None
        return next((row for row in self.experiments() if row["id"] == run_id), None)

    def interpretability(self) -> dict[str, Any]:
        def load() -> dict[str, Any]:
            selected = self._phase14()
            if selected is None:
                return {
                    "available": False,
                    "models": {},
                    "issue": "No verified Phase 14 artifacts found.",
                }
            run, metadata = selected
            files = {
                "logistic_regression": (
                    "global_top_positive.csv",
                    "global_top_negative.csv",
                    "review_feature_contributions.csv",
                ),
                "textcnn": ("max_filter_activations.csv", "token_window_occlusion.csv"),
                "rnn": (
                    "hidden_state_evolution.csv",
                    "hidden_dimension_activations.csv",
                    "token_window_occlusion.csv",
                ),
                "lstm": (
                    "gate_activations.csv",
                    "hidden_state_evolution.csv",
                    "hidden_dimension_activations.csv",
                    "token_window_occlusion.csv",
                ),
                "gru": (
                    "gate_activations.csv",
                    "hidden_state_evolution.csv",
                    "hidden_dimension_activations.csv",
                    "token_window_occlusion.csv",
                ),
            }
            models = {
                model: {
                    name.removesuffix(".csv"): _records(run / model / name, limit=100)
                    for name in names
                }
                for model, names in files.items()
            }
            cross = run / "cross_model"
            return {
                "available": True,
                "dataset_split": "validation",
                "sample_count": metadata.get("sample_count"),
                "models": models,
                "cross_model": {
                    "prediction_comparison": _records(
                        cross / "prediction_comparison.csv", limit=100
                    ),
                    "pairwise_explanation_comparison": _records(
                        cross / "pairwise_explanation_comparison.csv", limit=100
                    ),
                },
                "interpretation_limits": [
                    "Activations describe internals; they are not causal explanations.",
                    "Occlusion measures sensitivity to the saved masking method.",
                ],
                "integrity_verified": metadata.get("source_artifacts_unchanged_sha256"),
                "run_id": run.name,
            }

        return self._cached("interpretability", load)

    def robustness(self) -> dict[str, Any]:
        def load() -> dict[str, Any]:
            selected = self._phase15()
            if selected is None:
                return {
                    "available": False,
                    "issue": "No verified Phase 15 artifacts found.",
                }
            run, metadata = selected
            metrics = _json_object(run / "metrics.json")
            auto = run / "automatic_perturbations"
            semantic = run / "semantic_challenge"
            cross = run / "cross_model"
            return {
                "available": True,
                "dataset_split": (
                    "Validation-derived perturbations and an author-curated "
                    "semantic challenge"
                ),
                "automatic_perturbations": metrics.get("automatic_perturbations", {}),
                "automatic_overall_by_model": [
                    {"model": key, **value}
                    for key, value in metrics.get("automatic_perturbations", {})
                    .get("overall_review_level", {})
                    .items()
                    if isinstance(value, dict)
                ],
                "length_sensitivity_summary": metrics.get("automatic_perturbations", {})
                .get("length_sensitivity", {})
                .get("summary", []),
                "transformation_summary": _records(
                    auto / "transformation_summary.csv", limit=200
                ),
                "review_level_summary": _records(
                    auto / "review_level_summary.csv", limit=100
                ),
                "length_summary": _records(auto / "length_summary.csv", limit=200),
                "semantic_challenge": metrics.get("semantic_challenge", {}),
                "semantic_metrics_by_model": [
                    {"model": key, **value}
                    for key, value in metrics.get("semantic_challenge", {})
                    .get("models", {})
                    .items()
                    if isinstance(value, dict)
                ],
                "semantic_category_summary": _records(
                    semantic / "category_summary.csv", limit=200
                ),
                "all_models_wrong": _records(
                    semantic / "all_models_wrong.csv", limit=40
                ),
                "semantic_failure_examples": _records(
                    semantic / "failure_examples.csv", limit=40
                ),
                "prediction_disagreements": _records(
                    cross / "prediction_disagreements.csv", limit=100
                ),
                "transformation_accuracy_matrix": _records(
                    cross / "transformation_accuracy_matrix.csv", limit=200
                ),
                "curated_challenge_note": (
                    "The semantic challenge is author-curated; its labels "
                    "are not independently verified."
                ),
                "official_test_dataset_loaded": metadata.get(
                    "official_test_dataset_loaded"
                ),
                "integrity_verified": metadata.get("source_artifacts_unchanged"),
                "run_id": run.name,
            }

        return self._cached("robustness", load)

    def efficiency(self) -> dict[str, Any]:
        def load() -> dict[str, Any]:
            selected = self._phase16()
            if selected is None:
                return {
                    "available": False,
                    "issue": "No complete Phase 16 efficiency benchmark found.",
                }
            run, metadata = selected
            benchmark = _json_object(run / "benchmark.json")
            metrics = _json_object(run / "metrics.json")
            comparison = _json_object(run / "comparison.json")
            if (run / "loading_times.csv").is_file():
                quality_summary = _json_object(run / "model_quality_summary.json")
                tracks = metrics.get("tracks", {})
                public_tracks = {
                    track_id: {
                        key: value
                        for key, value in track.items()
                        if key
                        not in {"source_artifact_location", "source_model_artifacts"}
                    }
                    for track_id, track in tracks.items()
                }
                benchmark_models = {
                    model_id: {
                        key: model.get(key)
                        for key in (
                            "name",
                            "architecture",
                            "generation_id",
                            "parameter_count",
                            "trainable_parameter_count",
                            "artifact_size_bytes",
                        )
                    }
                    for model_id, model in benchmark.get("models", {}).items()
                }
                configuration = {
                    key: value
                    for key, value in benchmark.get("configuration", {}).items()
                    if key != "sample_source"
                }
                hardware = {
                    key: value
                    for key, value in benchmark.get("hardware", {}).items()
                    if key != "threadpool_limits_during_measurement"
                }
                return {
                    "available": True,
                    "schema_version": "active-generation-audit-v1",
                    "run_id": run.name,
                    "active_generation_id": benchmark.get("active_generation_id"),
                    "configuration": configuration,
                    "hardware": hardware,
                    "software_versions": benchmark.get("software_versions", {}),
                    "models": benchmark_models,
                    "quality_tracks": public_tracks,
                    "quality_summary": quality_summary,
                    "quality_efficiency_rows": comparison.get(
                        "quality_efficiency_rows", []
                    ),
                    "loading_times": _records(run / "loading_times.csv", limit=100),
                    "latency_summary": _records(run / "latency_summary.csv", limit=100),
                    "batch_benchmark": _records(run / "batch_benchmark.csv", limit=500),
                    "memory_summary": _records(run / "memory_summary.csv", limit=100),
                    "efficiency_awards": comparison.get("efficiency_awards", {}),
                    "plot_files": metadata.get("plot_files", []),
                    "source_artifact_hashes_unchanged": metadata.get(
                        "source_artifacts_unchanged"
                    ),
                    "active_generation_pointer_unchanged": metadata.get(
                        "active_generation_pointer_unchanged"
                    ),
                    "official_test_reviews_loaded": metadata.get(
                        "official_test_reviews_loaded"
                    ),
                    "official_test_results_reused_from_saved_artifacts": metadata.get(
                        "official_test_results_reused_from_saved_artifacts"
                    ),
                }
            return {
                "available": True,
                "configuration": benchmark.get("configuration", {}),
                "models": benchmark.get("models", {}),
                "evaluated_models": self.models().get("models", []),
                "model_summary": _records(run / "model_summary.csv", limit=100),
                "batch_summary": _records(run / "batch_summary.csv", limit=500),
                "evaluation_metrics_by_split": metrics,
                "comparison": comparison,
                "official_test_reviews_loaded": metadata.get(
                    "official_test_reviews_loaded"
                ),
                "source_artifacts_unchanged": metadata.get(
                    "source_artifacts_unchanged"
                ),
                "run_id": run.name,
            }

        return self._cached("efficiency", load)

    def quality(self) -> dict[str, Any]:
        """Return Phase 16 evaluation tracks without combining datasets."""
        result = self.efficiency()
        if not result.get("available"):
            return result
        return {
            "available": True,
            "run_id": result.get("run_id"),
            "active_generation_id": result.get("active_generation_id"),
            "tracks": result.get("quality_tracks", {}),
            "quality_efficiency_rows": result.get("quality_efficiency_rows", []),
            "quality_summary": result.get("quality_summary", {}),
            "separate_tracks": True,
        }

    def efficiency_plot_path(self, filename: str) -> Path | None:
        """Resolve only a plot listed by the selected saved benchmark."""
        if Path(filename).name != filename:
            return None
        selected = self._phase16()
        if selected is None:
            return None
        run, metadata = selected
        if filename not in metadata.get("plot_files", []):
            return None
        plot_directory = (run / "plots").resolve()
        plot_path = (plot_directory / filename).resolve()
        if not plot_path.is_relative_to(plot_directory) or not plot_path.is_file():
            return None
        return plot_path
