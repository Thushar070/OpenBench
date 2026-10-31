"""Compare the four neural architectures with the Logistic Regression baseline."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import sklearn
import torch
from threadpoolctl import threadpool_limits

from deeptext.data.dataset import load_imdb_split, split_training_data
from deeptext.evaluation.comparison import (
    benchmark_batch_throughput,
    benchmark_single_review_latency,
    compare_error_categories,
    evaluate_comparison_model,
    required_artifact_sizes,
)
from deeptext.inference.predictor import (
    load_recurrent_predictor,
    load_textcnn_predictor,
)
from deeptext.models.baseline import (
    load_baseline_artifacts,
    predict_baseline,
)

SEED = 42
VALIDATION_SIZE = 0.2
BENCHMARK_SAMPLE_COUNT = 100
MODEL_KEYS = (
    "logistic_regression",
    "textcnn",
    "rnn",
    "lstm",
    "gru",
)
RECURRENT_TYPES = {"rnn": "vanilla_rnn", "lstm": "lstm", "gru": "gru"}


def _latest_run(root: Path, prefix: str) -> Path:
    runs = []
    for directory in root.glob(f"{prefix}*"):
        metadata_path = directory / "metadata.json"
        if directory.is_dir() and metadata_path.is_file():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            created_at = metadata.get("created_at_utc")
            if isinstance(created_at, str):
                runs.append((created_at, directory))
    if not runs:
        raise FileNotFoundError(f"No completed {prefix} run under {root}.")
    return max(runs, key=lambda item: item[0])[1]


def _latest_phase6_comparison(root: Path) -> Path:
    runs = []
    for directory in root.glob("imdb-comparison-*"):
        comparison_path = directory / "comparison.json"
        if directory.is_dir() and comparison_path.is_file():
            metadata = json.loads(comparison_path.read_text(encoding="utf-8"))
            created_at = metadata.get("created_at_utc")
            if isinstance(created_at, str):
                runs.append((created_at, directory))
    if not runs:
        raise FileNotFoundError(f"No Phase 6 comparison artifact under {root}.")
    return max(runs, key=lambda item: item[0])[1]


def _fingerprint(records: pd.DataFrame) -> str:
    digest = hashlib.sha256()
    for review_id, label, text in records[["review_id", "label", "text"]].itertuples(
        index=False, name=None
    ):
        text_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        digest.update(f"{review_id}\0{int(label)}\0{text_digest}\n".encode("utf-8"))
    return digest.hexdigest()


def _read_predictions(
    path: Path, expected_ids: list[str], expected_labels: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    frame = pd.read_csv(path)
    required = {"review_id", "true_label", "predicted_label", "positive_probability"}
    if not required.issubset(frame.columns):
        raise ValueError(f"Prediction artifact has missing columns: {path}")
    if frame["review_id"].tolist() != expected_ids:
        raise ValueError(
            f"Prediction order does not match the shared validation split: {path}"
        )
    labels = frame["true_label"].to_numpy(dtype=np.int64)
    if not np.array_equal(labels, expected_labels):
        raise ValueError(
            f"Saved labels differ from the shared validation split: {path}"
        )
    predictions = frame["predicted_label"].to_numpy(dtype=np.int64)
    probabilities = frame["positive_probability"].to_numpy(dtype=np.float64)
    if (
        not np.isfinite(probabilities).all()
        or ((probabilities < 0) | (probabilities > 1)).any()
    ):
        raise ValueError(f"Invalid positive probabilities in {path}")
    return predictions, probabilities


def _validate_run(
    metadata: dict[str, Any],
    key: str,
    training_count: int,
    validation_count: int,
    training_fingerprint: str,
    validation_fingerprint: str,
) -> None:
    split = metadata.get("split", {})
    recorded_training_fingerprint = split.get(
        "training_partition_sha256",
        metadata.get("training_review_fingerprint_sha256"),
    )
    recorded_validation_fingerprint = split.get(
        "validation_partition_sha256",
        metadata.get("validation_review_fingerprint_sha256"),
    )
    if (
        split.get("source") != "official labeled training split only"
        or split.get("random_state", metadata.get("seed")) != SEED
        or split.get("validation_fraction") != VALIDATION_SIZE
        or split.get("training_rows") != training_count
        or split.get("validation_rows") != validation_count
        or metadata.get("official_test_evaluated") is not False
        or metadata.get("unlabeled_reviews_used") is not False
        or (
            recorded_training_fingerprint is not None
            and recorded_training_fingerprint != training_fingerprint
        )
        or (
            recorded_validation_fingerprint is not None
            and recorded_validation_fingerprint != validation_fingerprint
        )
    ):
        raise ValueError(f"{key} run metadata does not match the audited split.")


def run_neural_comparison(
    *,
    dataset_directory: str | Path = "datasets/aclImdb",
    baseline_run_directory: str | Path | None = None,
    textcnn_run_directory: str | Path | None = None,
    recurrent_run_directories: dict[str, str | Path] | None = None,
    phase6_comparison_directory: str | Path | None = None,
    output_root: str | Path = "outputs/neural_comparison",
    benchmark: bool = True,
) -> dict[str, Any]:
    """Compare saved validation predictions and optionally benchmark inference."""
    root = Path.cwd().resolve()
    baseline_run = (
        Path(baseline_run_directory).resolve()
        if baseline_run_directory
        else _latest_run(root / "outputs/baseline", "imdb-baseline-")
    )
    textcnn_run = (
        Path(textcnn_run_directory).resolve()
        if textcnn_run_directory
        else _latest_run(root / "outputs/textcnn", "imdb-textcnn-")
    )
    phase6_run = (
        Path(phase6_comparison_directory).resolve()
        if phase6_comparison_directory
        else _latest_phase6_comparison(root / "outputs/comparison")
    )
    recurrent_runs = {
        key: Path(
            (recurrent_run_directories or {}).get(
                key, _latest_run(root / "outputs/recurrent" / key, f"imdb-{key}-")
            )
        ).resolve()
        for key in RECURRENT_KEYS
    }

    original_training = load_imdb_split(dataset_directory, "train")
    training, validation = split_training_data(
        original_training, validation_size=VALIDATION_SIZE, seed=SEED
    )
    training_fingerprint = _fingerprint(training)
    validation_fingerprint = _fingerprint(validation)
    expected_ids = validation["review_id"].tolist()
    labels = validation["label"].to_numpy(dtype=np.int64)

    baseline_model, baseline_metadata = load_baseline_artifacts(baseline_run)
    cnn_metadata = json.loads(
        (textcnn_run / "metadata.json").read_text(encoding="utf-8")
    )
    _validate_run(
        baseline_metadata,
        "Logistic Regression",
        len(training),
        len(validation),
        training_fingerprint,
        validation_fingerprint,
    )
    _validate_run(
        cnn_metadata,
        "TextCNN",
        len(training),
        len(validation),
        training_fingerprint,
        validation_fingerprint,
    )
    phase6 = json.loads((phase6_run / "comparison.json").read_text(encoding="utf-8"))
    if (
        phase6.get("official_test_evaluated") is not False
        or phase6.get("validation_only") is not True
    ):
        raise ValueError("Phase 6 comparison artifact is not validation-only.")
    phase6_predictions = phase6_run / phase6["artifacts"]["validation_predictions"]
    saved = pd.read_csv(phase6_predictions)
    if saved["review_id"].tolist() != expected_ids or not np.array_equal(
        saved["true_label"].to_numpy(dtype=np.int64), labels
    ):
        raise ValueError(
            "Phase 6 predictions do not align with the shared validation split."
        )
    prediction_arrays: dict[str, tuple[np.ndarray, np.ndarray]] = {
        "logistic_regression": (
            saved["baseline_prediction"].to_numpy(dtype=np.int64),
            saved["baseline_positive_probability"].to_numpy(dtype=np.float64),
        ),
        "textcnn": (
            saved["textcnn_prediction"].to_numpy(dtype=np.int64),
            saved["textcnn_positive_probability"].to_numpy(dtype=np.float64),
        ),
    }
    for key, run in recurrent_runs.items():
        metadata = json.loads((run / "metadata.json").read_text(encoding="utf-8"))
        _validate_run(
            metadata,
            key.upper(),
            len(training),
            len(validation),
            training_fingerprint,
            validation_fingerprint,
        )
        expected_names = {"rnn": "VanillaRNN", "lstm": "LSTM", "gru": "GRU"}
        if metadata.get("model_name") != expected_names[key]:
            raise ValueError(f"Unexpected architecture recorded for {key}.")
        prediction_arrays[key] = _read_predictions(
            run / "validation_predictions.csv", expected_ids, labels
        )

    metrics = {
        key: evaluate_comparison_model(labels, *prediction_arrays[key])
        for key in MODEL_KEYS
    }
    history = {
        "phase6_logistic_regression": metrics["logistic_regression"],
        "phase6_textcnn": metrics["textcnn"],
    }
    for key, saved_metrics in history.items():
        source_key = (
            "logistic_regression" if key.endswith("logistic_regression") else "textcnn"
        )
        recorded = phase6["metrics"][source_key]
        if any(
            not np.isclose(
                metrics[source_key][name], recorded[name], rtol=0, atol=1e-12
            )
            for name in ("accuracy", "precision", "recall", "f1")
        ):
            raise RuntimeError(
                f"{source_key} metrics do not reproduce the Phase 6 record."
            )

    comparisons = {}
    for left_index, left in enumerate(MODEL_KEYS):
        for right in MODEL_KEYS[left_index + 1 :]:
            comparisons[f"{left}_vs_{right}"] = compare_error_categories(
                labels, prediction_arrays[left][0], prediction_arrays[right][0]
            )

    artifacts: dict[str, dict[str, Any]] = {
        "logistic_regression": required_artifact_sizes(
            {
                "baseline.joblib": baseline_run / "baseline.joblib",
                "metadata.json": baseline_run / "metadata.json",
            }
        ),
        "textcnn": required_artifact_sizes(
            {
                "best_model.pt": textcnn_run / "best_model.pt",
                "metadata.json": textcnn_run / "metadata.json",
                "vocabulary.json": textcnn_run / "vocabulary.json",
            }
        ),
    }
    for key, run in recurrent_runs.items():
        artifacts[key] = required_artifact_sizes(
            {
                "best_model.pt": run / "best_model.pt",
                "metadata.json": run / "metadata.json",
                "vocabulary.json": run / "vocabulary.json",
            }
        )

    neural_runs = {"textcnn": textcnn_run, **recurrent_runs}
    profiles: dict[str, dict[str, Any]] = {}
    for key, run in neural_runs.items():
        metadata = json.loads((run / "metadata.json").read_text(encoding="utf-8"))
        if key == "textcnn":
            profile_predictor = load_textcnn_predictor(run, "cpu")
            parameter_count = sum(
                parameter.numel() for parameter in profile_predictor.model.parameters()
            )
        else:
            parameter_count = metadata["parameter_count"]
        profiles[key] = {
            "parameter_count": int(parameter_count),
            "training_duration_seconds": metadata.get("training_duration_seconds"),
            "training_device": metadata.get("device_name"),
            "peak_cuda_memory": metadata.get("peak_cuda_memory"),
            "model_config": metadata.get("model_config"),
        }

    benchmark_results: dict[str, Any] = {}
    if benchmark:
        validation_texts = validation["text"].tolist()[:BENCHMARK_SAMPLE_COUNT]

        def baseline_predict_one(text: str) -> int:
            return predict_baseline(baseline_model, [text])[0]

        def baseline_predict_batch(texts: list[str]) -> list[int]:
            return predict_baseline(baseline_model, texts)

        with threadpool_limits(limits=1):
            benchmark_results["logistic_regression"] = {
                "cpu": _benchmark_predictors(
                    baseline_predict_one,
                    baseline_predict_batch,
                    validation_texts,
                    "CPU (single BLAS thread)",
                )
            }
        benchmark_results["textcnn"] = _benchmark_neural_run(
            textcnn_run, validation_texts, recurrent=False
        )
        for key, run in recurrent_runs.items():
            benchmark_results[key] = _benchmark_neural_run(
                run, validation_texts, recurrent=True
            )

    output_directory = (
        Path(output_root).resolve()
        / f"imdb-neural-comparison-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S.%fZ')}"
    )
    output_directory.mkdir(parents=True, exist_ok=False)
    output = {
        "comparison_id": output_directory.name,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "models": list(MODEL_KEYS),
        "sample_count": len(validation),
        "validation_only": True,
        "official_test_evaluated": False,
        "unlabeled_reviews_used": False,
        "split": {
            "source": "official labeled training split only",
            "seed": SEED,
            "validation_fraction": VALIDATION_SIZE,
            "training_rows": len(training),
            "validation_rows": len(validation),
            "training_review_fingerprint_sha256": training_fingerprint,
            "validation_review_fingerprint_sha256": validation_fingerprint,
        },
        "metrics": metrics,
        "model_profiles": profiles,
        "pairwise_error_categories": comparisons,
        "artifact_sizes": artifacts,
        "benchmarks": benchmark_results,
        "source_artifacts": {
            "phase6_comparison": str(phase6_run),
            "baseline": str(baseline_run),
            "textcnn": str(textcnn_run),
            **{key: str(path) for key, path in recurrent_runs.items()},
        },
        "software_versions": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "pandas": pd.__version__,
            "scikit_learn": sklearn.__version__,
            "torch": torch.__version__,
        },
    }
    (output_directory / "comparison.json").write_text(
        json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return output


def _benchmark_predictors(
    predict_one: Any, predict_batch: Any, reviews: list[str], device: str
) -> dict[str, Any]:
    synchronize = torch.cuda.synchronize if device.startswith("cuda") else None
    return {
        "single_review_latency": benchmark_single_review_latency(
            predict_one,
            reviews,
            warmup_count=5,
            repetitions=3,
            device=device,
            synchronize=synchronize,
        ),
        "batch_throughput": benchmark_batch_throughput(
            predict_batch,
            reviews,
            batch_size=32,
            warmup_count=1,
            repetitions=3,
            device=device,
            synchronize=synchronize,
        ),
    }


def _benchmark_neural_run(
    run: Path, reviews: list[str], *, recurrent: bool
) -> dict[str, Any]:
    results = {}
    for device in ("cpu", "cuda") if torch.cuda.is_available() else ("cpu",):
        predictor = (
            load_recurrent_predictor(run, device)
            if recurrent
            else load_textcnn_predictor(run, device)
        )
        label = "CPU" if device == "cpu" else torch.cuda.get_device_name()
        if device == "cpu":
            previous_threads = torch.get_num_threads()
            torch.set_num_threads(1)
            try:
                with threadpool_limits(limits=1):
                    results[device] = _benchmark_predictors(
                        lambda text, p=predictor: p.predict(text),
                        lambda texts, p=predictor: p.predict_batch(
                            texts, batch_size=32
                        ),
                        reviews,
                        "CPU (single PyTorch/BLAS thread)",
                    )
            finally:
                torch.set_num_threads(previous_threads)
        else:
            results[device] = _benchmark_predictors(
                lambda text, p=predictor: p.predict(text),
                lambda texts, p=predictor: p.predict_batch(texts, batch_size=32),
                reviews,
                label,
            )
    return results


RECURRENT_KEYS = tuple(RECURRENT_TYPES)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset", default="datasets/aclImdb", help="Extracted IMDb dataset directory"
    )
    parser.add_argument(
        "--output-root",
        default="outputs/neural_comparison",
        help="Comparison output directory",
    )
    parser.add_argument(
        "--no-benchmark",
        action="store_true",
        help="Skip inference performance benchmarks",
    )
    args = parser.parse_args()
    result = run_neural_comparison(
        dataset_directory=args.dataset,
        output_root=args.output_root,
        benchmark=not args.no_benchmark,
    )
    print(f"Comparison saved: {result['comparison_id']}")
    for key in MODEL_KEYS:
        values = result["metrics"][key]
        print(f"{key}: accuracy={values['accuracy']:.4f}, F1={values['f1']:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
