"""Consistent, inference-only efficiency measurements for frozen Phase 12 models."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import platform
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import psutil
import sklearn
import torch

from deeptext.inference.predictor import (
    load_recurrent_predictor,
    load_textcnn_predictor,
)
from deeptext.models.baseline import (
    load_baseline_artifacts,
    predict_baseline_probabilities,
)
from deeptext.models.cnn import TextCNN, TextCNNConfig
from deeptext.models.recurrent import RECURRENT_MODELS, RecurrentConfig
from deeptext.preprocessing.encoding import encode_text

MODEL_KEYS = ("logistic_regression", "textcnn", "rnn", "lstm", "gru")
BATCH_SIZES = (1, 8, 16, 32, 64, 128)
COLORS = ("#159B62", "#F28E2B", "#E45756", "#39A99B", "#C49A2A")
RECURRENT_TYPES = {
    "rnn": RECURRENT_MODELS["VanillaRNN"],
    "lstm": RECURRENT_MODELS["LSTM"],
    "gru": RECURRENT_MODELS["GRU"],
}


@dataclass(frozen=True)
class BenchmarkConfig:
    """Validated settings shared by every model and supported device."""

    seed: int = 42
    sample_limit: int = 128
    batch_sizes: tuple[int, ...] = BATCH_SIZES
    warmup_batches: int = 5
    timed_repetitions: int = 25
    single_review_repetitions: int = 100
    devices: tuple[str, ...] = ("cpu", "cuda")

    def __post_init__(self) -> None:
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ValueError("seed must be an integer.")
        if (
            isinstance(self.sample_limit, bool)
            or not isinstance(self.sample_limit, int)
            or self.sample_limit < 128
        ):
            raise ValueError("sample_limit must be an integer of at least 128.")
        if self.batch_sizes != BATCH_SIZES:
            raise ValueError(f"batch_sizes must be exactly {BATCH_SIZES}.")
        if self.warmup_batches < 1 or self.timed_repetitions < 20:
            raise ValueError("Use at least one warm-up and 20 timed repetitions.")
        if self.single_review_repetitions < 20:
            raise ValueError("Use at least 20 single-review latency observations.")
        if not self.devices or any(
            device not in {"cpu", "cuda"} for device in self.devices
        ):
            raise ValueError("devices must contain cpu and/or cuda.")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_hashes(sources: dict[str, Path]) -> dict[str, dict[str, str]]:
    return {
        key: {
            path.relative_to(directory).as_posix(): _sha256(path)
            for path in sorted(directory.rglob("*"))
            if path.is_file()
        }
        for key, directory in sources.items()
    }


def _latest_verified_phase12(root: Path) -> tuple[Path, dict[str, Any]]:
    candidates = sorted(
        (root / "outputs/neural_comparison").glob(
            "imdb-neural-comparison-*/comparison.json"
        )
    )
    for path in reversed(candidates):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if (
                data.get("validation_only") is True
                and data.get("official_test_evaluated") is False
                and set(data.get("models", ())) == set(MODEL_KEYS)
                and set(data.get("source_artifacts", {}))
                >= {"baseline", "textcnn", "rnn", "lstm", "gru"}
            ):
                sources = {
                    key: Path(
                        data["source_artifacts"][
                            "baseline" if key == "logistic_regression" else key
                        ]
                    ).resolve()
                    for key in MODEL_KEYS
                }
                output_root = (root / "outputs").resolve()
                if all(
                    path.is_relative_to(output_root) for path in sources.values()
                ) and all(path.is_dir() for path in sources.values()):
                    return path, data
        except OSError, KeyError, TypeError, json.JSONDecodeError:
            continue
    raise FileNotFoundError("No complete, validation-only Phase 12 run was found.")


def _latest_verified_phase13(root: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    candidates = sorted(
        (root / "outputs/final_evaluation").glob(
            "imdb-phase13-final-evaluation-*/comparison.json"
        )
    )
    for comparison_path in reversed(candidates):
        try:
            directory = comparison_path.parent
            comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
            metadata = json.loads(
                (directory / "metadata.json").read_text(encoding="utf-8")
            )
            if (
                set(comparison.get("models", ())) == set(MODEL_KEYS)
                and set(comparison.get("metrics", {})) == set(MODEL_KEYS)
                and comparison.get("sample_count") == 25_000
                and metadata.get("official_test_evaluated") is True
                and metadata.get("model_training_started") is False
                and metadata.get("preprocessing_refitted") is False
                and metadata.get("artifacts_unchanged") is True
                and metadata.get("artifact_sha256_before")
                == metadata.get("artifact_sha256_after")
            ):
                return comparison_path, comparison, metadata
        except OSError, KeyError, TypeError, json.JSONDecodeError:
            continue
    raise FileNotFoundError("No integrity-verified five-model Phase 13 run was found.")


def _latest_robustness_sample(root: Path) -> tuple[Path, pd.DataFrame, dict[str, Any]]:
    candidates = sorted(
        (root / "outputs/robustness").glob("imdb-robustness-*/metadata.json")
    )
    for metadata_path in reversed(candidates):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            run = metadata_path.parent
            sample = pd.read_csv(
                run / "automatic_perturbations/original_predictions.csv"
            )
            if (
                metadata.get("phase") == 15
                and metadata.get("official_test_dataset_loaded") is False
                and metadata.get("source_artifacts_unchanged") is True
                and len(sample) >= 128
                and sample.review_id.is_unique
                and set(sample.true_label.unique()) == {0, 1}
                and sample.text.map(
                    lambda text: isinstance(text, str) and bool(text.strip())
                ).all()
            ):
                return run, sample, metadata
        except OSError, ValueError, KeyError, TypeError, json.JSONDecodeError:
            continue
    raise FileNotFoundError("No complete Phase 15 validation sample was found.")


def select_benchmark_sample(
    records: pd.DataFrame, sample_limit: int = 128, seed: int = 42
) -> pd.DataFrame:
    """Select repeatable, class-balanced validation records without replacement."""
    required = {"review_id", "true_label", "text"}
    if not isinstance(records, pd.DataFrame) or not required.issubset(records.columns):
        raise ValueError("records must contain review_id, true_label, and text.")
    if records.empty or records.review_id.duplicated().any():
        raise ValueError("records must be nonempty and have unique review IDs.")
    if (
        isinstance(sample_limit, bool)
        or not isinstance(sample_limit, int)
        or sample_limit < 1
        or sample_limit > len(records)
    ):
        raise ValueError("sample_limit must be within the available row count.")
    if not records.true_label.isin((0, 1)).all():
        raise ValueError("true_label must contain only binary labels.")
    rng = np.random.default_rng(seed)
    class_count = sample_limit // 2
    selected = []
    for label in (0, 1):
        indexes = np.flatnonzero(records.true_label.to_numpy() == label)
        take = class_count if label == 0 else sample_limit - class_count
        if len(indexes) < take:
            raise ValueError("The source records do not support a balanced sample.")
        selected.extend(rng.choice(indexes, size=take, replace=False).tolist())
    return records.iloc[sorted(selected)].reset_index(drop=True)


def summarize_latencies(milliseconds: list[float]) -> dict[str, float | int | None]:
    """Summarize individual timed calls without mixing batch load time."""
    values = np.asarray(milliseconds, dtype=np.float64)
    if values.ndim != 1 or values.size == 0 or not np.isfinite(values).all():
        raise ValueError("latencies must contain at least one finite observation.")
    return {
        "observations": int(values.size),
        "mean_ms": float(values.mean()),
        "median_ms": float(np.median(values)),
        "minimum_ms": float(values.min()),
        "maximum_ms": float(values.max()),
        "p95_ms": float(np.percentile(values, 95, method="higher"))
        if values.size >= 20
        else None,
    }


def summarize_batch_measurements(
    *,
    batch_size: int,
    sample_count: int,
    timed_repetitions: int,
    batch_latencies_ms: list[float],
) -> dict[str, Any]:
    """Summarize complete sample passes, including each final partial batch."""
    if batch_size < 1 or sample_count < batch_size or timed_repetitions < 1:
        raise ValueError("batch size, sample count, and repetitions must be positive.")
    batches_per_pass = (sample_count + batch_size - 1) // batch_size
    if len(batch_latencies_ms) != batches_per_pass * timed_repetitions:
        raise ValueError("Batch observations do not match the configured full passes.")
    summary = summarize_latencies(batch_latencies_ms)
    total_seconds = sum(batch_latencies_ms) / 1000
    total_reviews = sample_count * timed_repetitions
    return {
        **summary,
        "timed_repetitions": timed_repetitions,
        "timed_batches": len(batch_latencies_ms),
        "total_inference_seconds": total_seconds,
        "total_reviews": total_reviews,
        "mean_batch_latency_ms": float(np.mean(batch_latencies_ms)),
        "mean_review_latency_ms": total_seconds * 1000 / total_reviews,
        "reviews_per_second": total_reviews / total_seconds,
        "final_partial_batch_size": sample_count % batch_size or None,
    }


def _load_model(key: str, source: Path, device: torch.device) -> tuple[Any, Any | None]:
    if key == "logistic_regression":
        return load_baseline_artifacts(source)[0], None
    if key == "textcnn":
        predictor = load_textcnn_predictor(source, device)
    else:
        predictor = load_recurrent_predictor(source, device)
    return predictor, predictor.model


def _initialization_seconds(key: str, profiles: dict[str, Any]) -> float | None:
    if key == "logistic_regression":
        return None
    configuration = profiles[key]["model_config"]
    started = time.perf_counter()
    if key == "textcnn":
        TextCNN(TextCNNConfig.from_dict(configuration))
    else:
        RECURRENT_TYPES[key](RecurrentConfig.from_dict(configuration))
    return time.perf_counter() - started


def _predict_batch(
    key: str, predictor: Any, texts: list[str], batch_size: int
) -> list[dict[str, Any]]:
    if key == "logistic_regression":
        probabilities = predict_baseline_probabilities(predictor, texts)
        labels = probabilities.argmax(axis=1).astype(int).tolist()
        return [
            {
                "predicted_class": label,
                "negative_probability": float(probability[0]),
                "positive_probability": float(probability[1]),
            }
            for label, probability in zip(labels, probabilities, strict=True)
        ]
    return predictor.predict_batch(texts, batch_size=batch_size)


def _synchronize(device: torch.device) -> None:
    if device.type == "cuda":
        torch.cuda.synchronize(device)


def _measure_forward_only(
    key: str,
    predictor: Any,
    model: Any | None,
    texts: list[str],
    repetitions: int,
    device: torch.device,
) -> dict[str, float | int | None]:
    """Time classifier/model execution after saved preprocessing is prepared."""
    if key == "logistic_regression":
        features = predictor[:-1].transform(texts)
        classifier = predictor.steps[-1][1]

        def forward(index: int) -> None:
            classifier.predict_proba(features[index : index + 1])

    else:
        encoded = [
            encode_text(
                text,
                predictor.vocabulary,
                max_length=model.config.max_sequence_length,
            )
            for text in texts
        ]
        input_ids = torch.tensor(
            [row[0] for row in encoded], dtype=torch.long, device=device
        )
        lengths = torch.tensor(
            [row[1] for row in encoded], dtype=torch.long, device=device
        )

        def forward(index: int) -> None:
            with torch.inference_mode():
                model(input_ids[index : index + 1], lengths[index : index + 1])

    timings = []
    with torch.inference_mode():
        for index in range(repetitions):
            _synchronize(device)
            started = time.perf_counter()
            forward(index % len(texts))
            _synchronize(device)
            timings.append((time.perf_counter() - started) * 1000)
    return summarize_latencies(timings)


def _measure_device_model(
    key: str,
    source: Path,
    texts: list[str],
    config: BenchmarkConfig,
    device_name: str,
    initialization_seconds: float | None,
    diagnostic_texts: list[str] | None = None,
) -> dict[str, Any]:
    device = torch.device(device_name)
    process = psutil.Process()
    rss_before = process.memory_info().rss
    load_started = time.perf_counter()
    predictor, model = _load_model(key, source, device)
    load_seconds = time.perf_counter() - load_started
    rss_after = process.memory_info().rss

    def predict(
        batch: list[str], model_predictor: Any = predictor
    ) -> list[dict[str, Any]]:
        result = _predict_batch(key, model_predictor, batch, len(batch))
        _synchronize(device)
        return result

    _synchronize(device)
    cold_start = time.perf_counter()
    predict([texts[0]])
    cold_prediction_ms = (time.perf_counter() - cold_start) * 1000
    diagnostics = (
        _predict_batch(key, predictor, diagnostic_texts, len(diagnostic_texts))
        if diagnostic_texts
        else []
    )
    _synchronize(device)
    if device.type == "cuda":
        torch.cuda.reset_peak_memory_stats(device)

    forward_only = _measure_forward_only(
        key,
        predictor,
        model,
        texts,
        config.single_review_repetitions,
        device,
    )

    for index in range(config.warmup_batches):
        predict([texts[index % len(texts)]])

    single_latencies: list[float] = []
    peak_rss_during = process.memory_info().rss
    for index in range(config.single_review_repetitions):
        review = texts[index % len(texts)]
        _synchronize(device)
        started = time.perf_counter()
        predict([review])
        single_latencies.append((time.perf_counter() - started) * 1000)
        peak_rss_during = max(peak_rss_during, process.memory_info().rss)

    batch_results = []
    cuda_oom_batch_size = None
    for batch_size in config.batch_sizes:
        sample_batches = [
            texts[start : start + batch_size]
            for start in range(0, len(texts), batch_size)
        ]
        if cuda_oom_batch_size is not None:
            batch_results.append(
                {
                    "batch_size": batch_size,
                    "available": False,
                    "unavailable_reason": (
                        "Skipped after CUDA out of memory at smaller batch size "
                        f"{cuda_oom_batch_size}."
                    ),
                    "timed_repetitions": 0,
                    "timed_batches": 0,
                    "total_inference_seconds": None,
                    "total_reviews": 0,
                    "mean_batch_latency_ms": None,
                    "mean_review_latency_ms": None,
                    "reviews_per_second": None,
                    "final_partial_batch_size": len(texts) % batch_size or None,
                }
            )
            continue
        try:
            for _ in range(config.warmup_batches):
                for batch in sample_batches:
                    predict(batch)
            observations = []
            peak_rss_during = max(peak_rss_during, process.memory_info().rss)
            for _ in range(config.timed_repetitions):
                for batch in sample_batches:
                    _synchronize(device)
                    started = time.perf_counter()
                    predict(batch)
                    observations.append((time.perf_counter() - started) * 1000)
                    peak_rss_during = max(peak_rss_during, process.memory_info().rss)
            batch_results.append(
                {
                    "batch_size": batch_size,
                    "available": True,
                    **summarize_batch_measurements(
                        batch_size=batch_size,
                        sample_count=len(texts),
                        timed_repetitions=config.timed_repetitions,
                        batch_latencies_ms=observations,
                    ),
                }
            )
        except torch.cuda.OutOfMemoryError as error:
            if device.type != "cuda":
                raise
            cuda_oom_batch_size = batch_size
            torch.cuda.empty_cache()
            batch_results.append(
                {
                    "batch_size": batch_size,
                    "available": False,
                    "unavailable_reason": (
                        f"CUDA out of memory at batch size {batch_size}: "
                        f"{type(error).__name__}."
                    ),
                    "timed_repetitions": 0,
                    "timed_batches": 0,
                    "total_inference_seconds": None,
                    "total_reviews": 0,
                    "mean_batch_latency_ms": None,
                    "mean_review_latency_ms": None,
                    "reviews_per_second": None,
                    "final_partial_batch_size": len(texts) % batch_size or None,
                }
            )

    if device.type == "cuda":
        gpu_memory = {
            "peak_allocated_bytes": int(torch.cuda.max_memory_allocated(device)),
            "peak_reserved_bytes": int(torch.cuda.max_memory_reserved(device)),
            "device_name": torch.cuda.get_device_name(device),
        }
    else:
        gpu_memory = None
    parameter_count = (
        sum(parameter.numel() for parameter in model.parameters())
        if model is not None
        else None
    )
    trainable_parameter_count = (
        sum(
            parameter.numel()
            for parameter in model.parameters()
            if parameter.requires_grad
        )
        if model is not None
        else None
    )
    result = {
        "device": device_name,
        "model_initialization_seconds": initialization_seconds,
        "model_initialization_unavailable_reason": (
            "The fitted sklearn pipeline has no separate untrained "
            "initialization stage."
            if key == "logistic_regression"
            else None
        ),
        "artifact_loading_seconds": load_seconds,
        "preprocessing_artifact_loading_included": True,
        "first_prediction_cold_start_latency_ms": cold_prediction_ms,
        "single_review_latency": summarize_latencies(single_latencies),
        "forward_only_single_review_latency": forward_only,
        "diagnostic_predictions": diagnostics,
        "batch_inference": batch_results,
        "process_memory": {
            "rss_before_load_bytes": int(rss_before),
            "rss_after_load_bytes": int(rss_after),
            "rss_peak_during_inference_bytes": int(peak_rss_during),
        },
        "gpu_memory": gpu_memory,
        "parameter_count": int(parameter_count)
        if parameter_count is not None
        else None,
        "parameter_count_unavailable_reason": (
            "Parameter counting is not directly comparable for the fitted "
            "TF-IDF and Logistic Regression pipeline."
            if key == "logistic_regression"
            else None
        ),
        "trainable_parameter_count": int(trainable_parameter_count)
        if trainable_parameter_count is not None
        else None,
    }
    del model, predictor
    gc.collect()
    if device.type == "cuda":
        torch.cuda.empty_cache()
    return result


def _save_efficiency_plots(
    output: Path,
    benchmark_rows: pd.DataFrame,
    batch_rows: pd.DataFrame,
    scores: pd.DataFrame,
) -> list[str]:
    plots = output / "plots"
    files: list[str] = []
    cpu = benchmark_rows[benchmark_rows.device == "cpu"]
    cpu_scores = scores.merge(cpu, on="model", validate="one_to_one")
    scatter_charts = [
        (
            "accuracy_vs_latency.png",
            "test_accuracy",
            "single_median_ms",
            "Official test accuracy",
            "Median single-review latency (ms)",
            "cpu_scores",
        ),
        (
            "f1_vs_latency.png",
            "test_f1",
            "single_median_ms",
            "Official test F1",
            "Median single-review latency (ms)",
            "cpu_scores",
        ),
        (
            "throughput_vs_memory.png",
            "throughput_batch_128_reviews_per_second",
            "rss_peak_during_inference_bytes",
            "CPU throughput (reviews/s), batch 128",
            "Peak process RSS (bytes)",
            "cpu_scores",
        ),
        (
            "model_size_vs_f1.png",
            "test_f1",
            "artifact_size_bytes",
            "Official test F1",
            "Artifact directory size (bytes)",
            "scores",
        ),
        (
            "loading_vs_inference.png",
            "artifact_loading_seconds",
            "single_median_ms",
            "Artifact loading time (s)",
            "Median single-review latency (ms)",
            "cpu_scores",
        ),
    ]
    for filename, x_key, y_key, x_label, y_label, frame_name in scatter_charts:
        frame = cpu_scores if frame_name == "cpu_scores" else scores
        fig, ax = plt.subplots(figsize=(8, 5), constrained_layout=True)
        for index, row in enumerate(frame.itertuples(index=False)):
            x_value = getattr(row, x_key)
            y_value = getattr(row, y_key)
            ax.scatter(x_value, y_value, color=COLORS[index % len(COLORS)], s=70)
            ax.annotate(
                row.model, (x_value, y_value), xytext=(5, 4), textcoords="offset points"
            )
        ax.set(
            xlabel=x_label,
            ylabel=y_label,
            title=filename.removesuffix(".png").replace("_", " ").title(),
        )
        ax.grid(alpha=0.16)
        fig.savefig(plots / filename, dpi=150)
        plt.close(fig)
        files.append(filename)

    fig, ax = plt.subplots(figsize=(9, 5), constrained_layout=True)
    for index, model in enumerate(MODEL_KEYS):
        part = batch_rows[batch_rows.model == model]
        ax.plot(
            part.batch_size,
            part.cpu_reviews_per_second,
            marker="o",
            color=COLORS[index],
            label=f"{model} CPU",
        )
        if part.cuda_reviews_per_second.notna().any():
            ax.plot(
                part.batch_size,
                part.cuda_reviews_per_second,
                marker="s",
                linestyle="--",
                color=COLORS[index],
                label=f"{model} CUDA",
            )
    ax.set(
        xlabel="Batch size",
        ylabel="Reviews per second",
        title="Batch-size throughput scaling",
    )
    ax.legend(fontsize=7, ncol=2)
    ax.grid(alpha=0.16)
    fig.savefig(plots / "batch_size_scaling.png", dpi=150)
    plt.close(fig)
    files.append("batch_size_scaling.png")

    fig, ax = plt.subplots(figsize=(8, 5), constrained_layout=True)
    cpu_32 = batch_rows[batch_rows.batch_size == 32]
    x = np.arange(len(MODEL_KEYS))
    ax.bar(x - 0.18, cpu_32.cpu_reviews_per_second, 0.36, color=COLORS[0], label="CPU")
    if cpu_32.cuda_reviews_per_second.notna().any():
        ax.bar(
            x + 0.18,
            cpu_32.cuda_reviews_per_second,
            0.36,
            color=COLORS[1],
            label="CUDA",
        )
    ax.set(
        xticks=x,
        xticklabels=MODEL_KEYS,
        ylabel="Reviews per second",
        title="CPU versus CUDA throughput (batch size 32)",
    )
    ax.legend()
    ax.grid(axis="y", alpha=0.16)
    fig.savefig(plots / "cpu_vs_cuda_throughput.png", dpi=150)
    plt.close(fig)
    files.append("cpu_vs_cuda_throughput.png")
    return files


def _summary_rows(
    model_results: dict[str, dict[str, Any]],
    phase12: dict[str, Any],
    phase13: dict[str, Any],
    artifact_sizes: dict[str, int],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    summary_rows = []
    batch_rows = []
    scores = []
    for key in MODEL_KEYS:
        validation_metrics = phase12["metrics"][key]
        test_metrics = phase13["metrics"][key]
        scores.append(
            {
                "model": key,
                "validation_accuracy": validation_metrics["accuracy"],
                "validation_f1": validation_metrics["f1"],
                "validation_source": "Phase 12 validation split",
                "test_accuracy": test_metrics["accuracy"],
                "test_f1": test_metrics["f1"],
                "test_source": "Phase 13 official test evaluation",
                "artifact_size_bytes": artifact_sizes[key],
                "parameter_count": model_results[key]
                .get("cpu", {})
                .get(
                    "parameter_count",
                    phase12["model_profiles"].get(key, {}).get("parameter_count"),
                ),
                "historical_training_duration_seconds": phase12["model_profiles"]
                .get(key, {})
                .get("training_duration_seconds"),
            }
        )
        for device_name, device_result in model_results[key].items():
            if not device_result.get("available"):
                summary_rows.append(
                    {
                        "model": key,
                        "device": device_name,
                        "available": False,
                        "unavailable_reason": device_result.get("unavailable_reason"),
                    }
                )
                continue
            latency = device_result["single_review_latency"]
            throughput_128 = next(
                row
                for row in device_result["batch_inference"]
                if row["batch_size"] == 128
            )
            summary_rows.append(
                {
                    "model": key,
                    "device": device_name,
                    "available": True,
                    "artifact_loading_seconds": device_result[
                        "artifact_loading_seconds"
                    ],
                    "model_initialization_seconds": device_result[
                        "model_initialization_seconds"
                    ],
                    "cold_start_latency_ms": device_result[
                        "first_prediction_cold_start_latency_ms"
                    ],
                    "single_mean_ms": latency["mean_ms"],
                    "single_median_ms": latency["median_ms"],
                    "single_p95_ms": latency["p95_ms"],
                    "throughput_batch_128_reviews_per_second": throughput_128[
                        "reviews_per_second"
                    ],
                    "rss_before_load_bytes": device_result["process_memory"][
                        "rss_before_load_bytes"
                    ],
                    "rss_after_load_bytes": device_result["process_memory"][
                        "rss_after_load_bytes"
                    ],
                    "rss_peak_during_inference_bytes": device_result["process_memory"][
                        "rss_peak_during_inference_bytes"
                    ],
                    "gpu_peak_allocated_bytes": (device_result["gpu_memory"] or {}).get(
                        "peak_allocated_bytes"
                    ),
                    "gpu_peak_reserved_bytes": (device_result["gpu_memory"] or {}).get(
                        "peak_reserved_bytes"
                    ),
                    "parameter_count": device_result["parameter_count"],
                }
            )
            for row in device_result["batch_inference"]:
                batch_rows.append({"model": key, "device": device_name, **row})
    return pd.DataFrame(summary_rows), pd.DataFrame(batch_rows), pd.DataFrame(scores)


def _records_without_nan(frame: pd.DataFrame) -> list[dict[str, Any]]:
    cleaned = frame.astype(object).where(pd.notna(frame), None)
    return cleaned.to_dict(orient="records")


def run_efficiency_benchmark(
    *,
    output_root: str | Path = "outputs/efficiency",
    sample_limit: int = 128,
    seed: int = 42,
    devices: tuple[str, ...] = ("cpu", "cuda"),
    warmup_batches: int = 5,
    timed_repetitions: int = 25,
    single_review_repetitions: int = 100,
) -> dict[str, Any]:
    """Measure frozen models on a saved validation sample; never load test reviews."""
    config = BenchmarkConfig(
        sample_limit=sample_limit,
        seed=seed,
        devices=devices,
        warmup_batches=warmup_batches,
        timed_repetitions=timed_repetitions,
        single_review_repetitions=single_review_repetitions,
    )
    root = Path(__file__).resolve().parents[3]
    phase12_path, phase12 = _latest_verified_phase12(root)
    phase13_path, phase13, phase13_metadata = _latest_verified_phase13(root)
    robustness_run, source_records, phase15_metadata = _latest_robustness_sample(root)
    sources = {
        key: Path(
            phase12["source_artifacts"][
                "baseline" if key == "logistic_regression" else key
            ]
        ).resolve()
        for key in MODEL_KEYS
    }
    output_root_path = (root / "outputs").resolve()
    if not all(path.is_relative_to(output_root_path) for path in sources.values()):
        raise ValueError("Phase 12 model paths must remain under project outputs.")

    sample = select_benchmark_sample(source_records, config.sample_limit, config.seed)
    texts = sample.text.tolist()
    id_fingerprint = hashlib.sha256(
        "\n".join(sample.review_id.astype(str)).encode()
    ).hexdigest()
    hashes_before = _directory_hashes(sources)
    process = psutil.Process()
    process_memory_before = int(process.memory_info().rss)
    original_threads = torch.get_num_threads()
    if "cpu" in config.devices:
        torch.set_num_threads(1)

    cuda_requested = "cuda" in config.devices
    cuda_available = cuda_requested and torch.cuda.is_available()
    available_devices = [device for device in config.devices if device == "cpu"]
    if cuda_available:
        available_devices.append("cuda")
    initialization = {
        key: _initialization_seconds(key, phase12["model_profiles"])
        for key in MODEL_KEYS
    }
    model_results: dict[str, dict[str, Any]] = {key: {} for key in MODEL_KEYS}
    try:
        for key in MODEL_KEYS:
            for device_name in available_devices:
                if key == "logistic_regression" and device_name == "cuda":
                    model_results[key][device_name] = {
                        "available": False,
                        "unavailable_reason": (
                            "The sklearn TF-IDF pipeline supports CPU inference only."
                        ),
                    }
                    continue
                try:
                    measured = _measure_device_model(
                        key,
                        sources[key],
                        texts,
                        config,
                        device_name,
                        initialization[key],
                    )
                    measured["available"] = True
                    model_results[key][device_name] = measured
                except RuntimeError as error:
                    if device_name != "cuda":
                        raise
                    torch.cuda.empty_cache()
                    model_results[key][device_name] = {
                        "available": False,
                        "unavailable_reason": (
                            f"CUDA inference unavailable ({type(error).__name__})."
                        ),
                    }
            if cuda_requested and not cuda_available:
                model_results[key]["cuda"] = {
                    "available": False,
                    "unavailable_reason": "CUDA is not available on this machine.",
                }
    finally:
        torch.set_num_threads(original_threads)

    hashes_after = _directory_hashes(sources)
    if hashes_before != hashes_after:
        raise RuntimeError("Frozen model artifact hashes changed during benchmarking.")
    artifact_sizes = {
        key: sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())
        for key, directory in sources.items()
    }
    summary, batches, scores = _summary_rows(
        model_results, phase12, phase13, artifact_sizes
    )
    batch_comparison_rows = []
    for key in MODEL_KEYS:
        for batch_size in BATCH_SIZES:
            row: dict[str, Any] = {"model": key, "batch_size": batch_size}
            for device_name in ("cpu", "cuda"):
                device_rows = batches[
                    (batches.model == key)
                    & (batches.device == device_name)
                    & (batches.batch_size == batch_size)
                ]
                row[f"{device_name}_reviews_per_second"] = (
                    float(device_rows.reviews_per_second.iloc[0])
                    if not device_rows.empty
                    else None
                )
            batch_comparison_rows.append(row)
    batch_comparison = pd.DataFrame(batch_comparison_rows)

    destination = Path(output_root)
    if not destination.is_absolute():
        destination = root / destination
    output = destination.resolve() / (
        f"imdb-efficiency-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S.%fZ')}"
    )
    (output / "plots").mkdir(parents=True, exist_ok=False)
    summary.to_csv(output / "model_summary.csv", index=False)
    batches.to_csv(output / "batch_summary.csv", index=False)
    scores.to_csv(output / "scores_by_split.csv", index=False)
    plots = _save_efficiency_plots(output, summary, batch_comparison, scores)

    config_json = asdict(config)
    config_json["batch_sizes"] = list(config.batch_sizes)
    config_json["devices"] = list(config.devices)
    benchmark = {
        "configuration": config_json,
        "timing_method": {
            "inputs": (
                "Seeded class-balanced subset of Phase 15 validation data; "
                "the same review texts are used for every model."
            ),
            "preprocessing": (
                "End-to-end raw text inference, including existing preprocessing."
            ),
            "warmup": (
                "Warm-up calls precede timed calls; cold-start is recorded separately."
            ),
            "steady_state": (
                "Calls use perf_counter; CUDA is synchronized around each timed call."
            ),
            "latency_statistics": (
                "Mean, median and P95; P95 requires at least 20 observations."
            ),
            "throughput": ("Reviews processed divided by summed timed-batch duration."),
            "model_loading": (
                "Artifact loading and architecture initialization are separate "
                "from first-prediction latency."
            ),
            "cpu": (
                "CPU measurements use one PyTorch thread; the prior count is "
                "restored afterwards."
            ),
        },
        "sample": {
            "count": len(sample),
            "class_distribution": {
                str(label): int(count)
                for label, count in sample.true_label.value_counts()
                .sort_index()
                .items()
            },
            "source": "Phase 15 saved validation-only original review sample",
            "review_ids_sha256": id_fingerprint,
        },
        "models": model_results,
    }
    metadata = {
        "phase": 16,
        "benchmark_type": "new consistent inference-only measurement",
        "created_at_utc": datetime.now(UTC).isoformat(),
        "output_directory": str(output),
        "dataset_split_used_for_timing": (
            "Phase 12 validation sample via Phase 15 saved validation rows"
        ),
        "official_test_reviews_loaded": False,
        "official_test_scores_are_historical_phase13_metrics": True,
        "model_training_or_tuning_performed": False,
        "preprocessing_refit": False,
        "checkpoints_modified": False,
        "source_artifacts_unchanged": hashes_before == hashes_after,
        "source_artifact_hashes_before": hashes_before,
        "source_artifacts": {key: str(path) for key, path in sources.items()},
        "source_phase12_comparison": str(phase12_path),
        "source_phase13_comparison": str(phase13_path),
        "source_phase13_metadata_verified": phase13_metadata["artifacts_unchanged"],
        "source_phase15_run": str(robustness_run),
        "source_phase15_artifact_integrity_verified": phase15_metadata[
            "source_artifacts_unchanged"
        ],
        "process_rss_before_benchmark_bytes": process_memory_before,
        "hardware": {
            "platform": platform.platform(),
            "processor": platform.processor() or None,
            "cpu_logical_count": psutil.cpu_count(logical=True),
            "cuda_available": bool(torch.cuda.is_available()),
            "cuda_device": torch.cuda.get_device_name(0)
            if torch.cuda.is_available()
            else None,
        },
        "software_versions": {
            "python": platform.python_version(),
            "torch": torch.__version__,
            "numpy": np.__version__,
            "pandas": pd.__version__,
            "scikit_learn": sklearn.__version__,
        },
        "plot_files": plots,
        "limitations": [
            "Latencies are local measurements and vary with background system load.",
            (
                "The balanced validation sample standardizes inputs; it does not "
                "estimate population latency distributions."
            ),
            (
                "Phase 13 test accuracy/F1 are read from its verified saved summary; "
                "no test reviews were loaded or used for tuning."
            ),
            (
                "The sklearn baseline has no CUDA implementation or separate "
                "untrained initialization stage."
            ),
        ],
    }
    comparison = {
        "models": list(MODEL_KEYS),
        "evaluation_metrics_by_split": {
            "validation": {key: phase12["metrics"][key] for key in MODEL_KEYS},
            "official_test": {key: phase13["metrics"][key] for key in MODEL_KEYS},
        },
        "metric_sources": {
            "validation": "Phase 12 saved validation-only comparison",
            "official_test": (
                "Phase 13 saved final evaluation; no test reviews loaded in Phase 16"
            ),
        },
        "benchmark_rows": _records_without_nan(summary),
        "batch_throughput": _records_without_nan(batch_comparison),
        "historical_phase12_phase13_timing_mixed_with_new_measurement": False,
    }
    payloads = {
        "metrics.json": {
            "validation": comparison["evaluation_metrics_by_split"]["validation"],
            "official_test": comparison["evaluation_metrics_by_split"]["official_test"],
            "metric_sources": comparison["metric_sources"],
        },
        "benchmark.json": benchmark,
        "metadata.json": metadata,
        "comparison.json": comparison,
    }
    for filename, payload in payloads.items():
        (output / filename).write_text(
            json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
    return {
        "output_directory": str(output),
        "metadata": metadata,
        "comparison": comparison,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", default="outputs/efficiency")
    parser.add_argument("--sample-limit", type=int, default=128)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--device", choices=("auto", "cpu", "both"), default="auto")
    args = parser.parse_args()
    devices = (
        ("cpu", "cuda")
        if args.device in {"auto", "both"} and torch.cuda.is_available()
        else ("cpu",)
    )
    result = run_efficiency_benchmark(
        output_root=args.output_root,
        sample_limit=args.sample_limit,
        seed=args.seed,
        devices=devices,
    )
    print(
        json.dumps(
            {"output_directory": result["output_directory"], "devices": devices},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
