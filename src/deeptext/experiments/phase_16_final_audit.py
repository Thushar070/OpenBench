"""Final source-aware quality audit and efficiency benchmark for frozen models."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
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
from threadpoolctl import threadpool_info, threadpool_limits

from deeptext.dashboard.artifacts import MODEL_LABELS
from deeptext.experiments.efficiency import (
    BATCH_SIZES,
    MODEL_KEYS,
    BenchmarkConfig,
    _directory_hashes,
    _initialization_seconds,
    _latest_verified_phase12,
    _latest_verified_phase13,
    _load_model,
    _measure_device_model,
    _records_without_nan,
)
from deeptext.experiments.phase_16_quality import (
    DIAGNOSTIC_EXAMPLES,
    add_diagnostic_track,
    collect_quality_sources,
    discover_active_generation,
    summarize_quality,
)

PLOT_COLORS = ("#159B62", "#F28E2B", "#E45756", "#39A99B", "#C49A2A")
OUTPUT_JSON_FILES = (
    "metadata.json",
    "benchmark_config.json",
    "benchmark.json",
    "metrics.json",
    "comparison.json",
    "model_quality_summary.json",
)
OUTPUT_CSV_COLUMNS = {
    "loading_times.csv": {
        "model_id",
        "device",
        "available",
        "artifact_loading_seconds",
        "first_prediction_cold_start_latency_ms",
    },
    "latency_summary.csv": {
        "model_id",
        "device",
        "single_review_mean_ms",
        "single_review_median_ms",
        "single_review_p95_ms",
        "forward_only_median_ms",
    },
    "batch_benchmark.csv": {
        "model_id",
        "device",
        "batch_size",
        "available",
        "total_reviews",
        "mean_batch_latency_ms",
        "mean_review_latency_ms",
        "reviews_per_second",
        "final_partial_batch_size",
    },
    "memory_summary.csv": {
        "model_id",
        "device",
        "rss_before_load_bytes",
        "rss_peak_during_inference_bytes",
    },
    "efficiency_quality_comparison.csv": {
        "model_id",
        "quality_track_id",
        "quality_dataset_id",
        "quality_split",
        "quality_source_generation",
        "quality_source_run_id",
        "accuracy",
        "f1",
        "latency_device",
        "single_review_median_ms",
        "artifact_size_bytes",
    },
}


def select_representative_sample(
    records: pd.DataFrame, sample_limit: int = 130, seed: int = 42
) -> pd.DataFrame:
    """Choose balanced, repeatable workload rows spanning short and long texts."""
    required = {"review_id", "true_label", "text"}
    if not isinstance(records, pd.DataFrame) or not required.issubset(records.columns):
        raise ValueError("records must contain review_id, true_label, and text.")
    if records.empty or records.review_id.duplicated().any():
        raise ValueError("records must be nonempty and have unique review IDs.")
    if isinstance(sample_limit, bool) or not isinstance(sample_limit, int):
        raise ValueError("sample_limit must be an integer.")
    if sample_limit < 128 or sample_limit > len(records):
        raise ValueError(
            "sample_limit must be between 128 and the available row count."
        )
    if not records.true_label.isin((0, 1)).all():
        raise ValueError("true_label must contain only binary labels.")
    if not records.text.map(
        lambda value: isinstance(value, str) and bool(value.strip())
    ).all():
        raise ValueError("texts must be nonempty strings.")

    rng = np.random.default_rng(seed)
    per_class = sample_limit // 2
    selected_positions: list[int] = []
    for label in (0, 1):
        indexes = np.flatnonzero(records.true_label.to_numpy() == label)
        if len(indexes) < per_class:
            raise ValueError("The source does not contain enough rows in each class.")
        lengths = records.iloc[indexes].text.str.len().to_numpy()
        ordered = indexes[np.argsort(lengths, kind="stable")]
        positions = np.linspace(0, len(ordered) - 1, per_class).round().astype(int)
        selected_positions.extend(ordered[positions].tolist())
    selected_positions = np.asarray(selected_positions, dtype=int)
    selected_positions = selected_positions[rng.permutation(len(selected_positions))]
    sample = records.iloc[selected_positions].reset_index(drop=True).copy()
    if sample.review_id.duplicated().any():
        raise ValueError("Length-stratified selection produced duplicate review IDs.")
    return sample


def supported_device_plan(
    devices: tuple[str, ...], cuda_available: bool
) -> dict[str, list[str]]:
    """Return model/device pairs without silently moving work between devices."""
    if not devices or any(device not in {"cpu", "cuda"} for device in devices):
        raise ValueError("devices must contain cpu and/or cuda.")
    selected = {key: [] for key in MODEL_KEYS}
    for model_id in MODEL_KEYS:
        for device in devices:
            if device == "cpu":
                selected[model_id].append("cpu")
            elif (
                device == "cuda"
                and cuda_available
                and model_id != "logistic_regression"
            ):
                selected[model_id].append("cuda")
    return selected


def load_active_predictors(
    project_root: str | Path | None = None,
    device: str = "cpu",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load every active artifact through the project's existing inference loaders."""
    if device not in {"cpu", "cuda"}:
        raise ValueError("device must be cpu or cuda.")
    active = discover_active_generation(
        project_root or Path(__file__).resolve().parents[3]
    )
    selected_device = torch.device(device)
    predictors = {
        model_id: _load_model(
            model_id,
            active["model_directories"][model_id],
            selected_device,
        )[0]
        for model_id in MODEL_KEYS
    }
    return active, predictors


def _unavailable_device(reason: str) -> dict[str, Any]:
    return {"available": False, "unavailable_reason": reason}


def memory_measurement_record(
    model_id: str, device: str, result: dict[str, Any]
) -> dict[str, Any]:
    """Normalize process and GPU memory readings without inventing missing values."""
    process = result.get("process_memory", {})
    gpu = result.get("gpu_memory") or {}
    rss_before = process.get("rss_before_load_bytes")
    rss_peak = process.get("rss_peak_during_inference_bytes")
    return {
        "model_id": model_id,
        "model_name": MODEL_LABELS[model_id],
        "device": device,
        "available": bool(result.get("available")),
        "unavailable_reason": result.get("unavailable_reason"),
        "rss_before_load_bytes": rss_before,
        "rss_after_load_bytes": process.get("rss_after_load_bytes"),
        "rss_peak_during_inference_bytes": rss_peak,
        "observed_rss_increase_bytes": (
            max(0, rss_peak - rss_before)
            if rss_peak is not None and rss_before is not None
            else None
        ),
        "peak_gpu_allocated_bytes": gpu.get("peak_allocated_bytes"),
        "peak_gpu_reserved_bytes": gpu.get("peak_reserved_bytes"),
        "gpu_device_name": gpu.get("device_name"),
    }


def _diagnostic_rows(
    per_model_predictions: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    rows = []
    for model_id in MODEL_KEYS:
        predictions = per_model_predictions[model_id]
        if len(predictions) != len(DIAGNOSTIC_EXAMPLES):
            raise ValueError(f"{model_id} returned an incomplete diagnostic result.")
        for (text, expected, group), prediction in zip(
            DIAGNOSTIC_EXAMPLES, predictions, strict=True
        ):
            positive = float(prediction["positive_probability"])
            negative = float(prediction["negative_probability"])
            if not all(math.isfinite(value) for value in (positive, negative)):
                raise ValueError(f"{model_id} returned a non-finite diagnostic score.")
            rows.append(
                {
                    "model_id": model_id,
                    "model_name": MODEL_LABELS[model_id],
                    "text": text,
                    "example_group": group,
                    "expected_label": expected,
                    "predicted_label": int(prediction["predicted_class"]),
                    "predicted_sentiment": (
                        "positive" if int(prediction["predicted_class"]) else "negative"
                    ),
                    "negative_probability": negative,
                    "positive_probability": positive,
                }
            )
    return rows


def _quality_rows(
    quality: dict[str, Any],
    device_results: dict[str, dict[str, dict[str, Any]]],
    artifact_sizes: dict[str, int],
) -> list[dict[str, Any]]:
    rows = []
    for track_id, track in quality["tracks"].items():
        for model_id in MODEL_KEYS:
            metric = track["metrics_by_model"][model_id]
            for device in ("cpu", "cuda"):
                device_result = device_results[model_id].get(device, {})
                latency = device_result.get("single_review_latency", {})
                rows.append(
                    {
                        "model_id": model_id,
                        "model_name": MODEL_LABELS[model_id],
                        "quality_track_id": track_id,
                        "quality_dataset_id": track["dataset_id"],
                        "quality_split": track["split"],
                        "quality_source_generation": track.get(
                            "source_model_generation"
                        ),
                        "quality_source_run_id": track.get("source_evaluation_run_id"),
                        "quality_available": metric.get("available", False),
                        "evaluated_examples": metric.get("evaluated_examples"),
                        "accuracy": metric.get("accuracy"),
                        "precision": metric.get("precision"),
                        "recall": metric.get("recall"),
                        "f1": metric.get("f1"),
                        "confusion_matrix": json.dumps(metric.get("confusion_matrix")),
                        "latency_device": device,
                        "single_review_median_ms": latency.get("median_ms"),
                        "single_review_mean_ms": latency.get("mean_ms"),
                        "artifact_size_bytes": artifact_sizes[model_id],
                    }
                )
    return rows


def _frame_rows(rows: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def _save_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _track_score(
    quality: dict[str, Any], track_id: str, metric: str
) -> dict[str, float | None]:
    track = quality["tracks"][track_id]
    return {
        model_id: track["metrics_by_model"][model_id].get(metric)
        if track["metrics_by_model"][model_id].get("available")
        else None
        for model_id in MODEL_KEYS
    }


def _plot_quality_track(quality: dict[str, Any], metric: str, output: Path) -> None:
    track_ids = list(quality["tracks"])
    labels = [
        "IMDb val\nrecovery selection",
        "IMDb val\nPhase 12 historical",
        "IMDb test\nactive saved",
        "IMDb test\nPhase 13 historical",
        "IMDb test\nPhase 7 historical",
        "UCI val\nactive saved",
        "UCI test\nactive saved",
        "Phase 15\nsemantic",
        "Phase 15\nautomatic robustness",
        "Active short-text\ndiagnostics",
    ]
    x = np.arange(len(track_ids), dtype=float)
    width = 0.15
    fig, ax = plt.subplots(figsize=(18, 7), constrained_layout=True)
    for index, model_id in enumerate(MODEL_KEYS):
        values = [
            quality["tracks"][track_id]["metrics_by_model"][model_id].get(metric)
            for track_id in track_ids
        ]
        positions = x + (index - (len(MODEL_KEYS) - 1) / 2) * width
        ax.bar(
            positions,
            [np.nan if value is None else value for value in values],
            width,
            label=MODEL_LABELS[model_id],
            color=PLOT_COLORS[index],
        )
    ax.set_xticks(x, labels, rotation=15, ha="right")
    ax.set_ylim(0, 1.0)
    ax.set_ylabel(metric.replace("_", " ").title())
    ax.set_title(f"Saved model {metric.title()} by separate evaluation track")
    ax.grid(axis="y", alpha=0.22)
    ax.legend(ncols=2)
    fig.savefig(output, dpi=160)
    plt.close(fig)


def _save_plots(
    output: Path,
    quality: dict[str, Any],
    devices: dict[str, dict[str, dict[str, Any]]],
    artifact_sizes: dict[str, int],
) -> list[str]:
    plot_dir = output / "plots"
    plot_dir.mkdir(parents=True, exist_ok=False)
    files: list[str] = []

    _plot_quality_track(quality, "accuracy", plot_dir / "quality_accuracy.png")
    _plot_quality_track(quality, "f1", plot_dir / "quality_f1.png")
    files.extend(("quality_accuracy.png", "quality_f1.png"))

    fig, axes = plt.subplots(1, 2, figsize=(14, 5), constrained_layout=True)
    for axis, device in zip(axes, ("cpu", "cuda"), strict=True):
        values = []
        labels = []
        colors = []
        for index, model_id in enumerate(MODEL_KEYS):
            result = devices[model_id].get(device, {})
            latency = result.get("single_review_latency", {}).get("median_ms")
            if latency is not None:
                values.append(latency)
                labels.append(MODEL_LABELS[model_id])
                colors.append(PLOT_COLORS[index])
        axis.barh(labels, values, color=colors)
        axis.set_title(f"{device.upper()} end-to-end")
        axis.set_xlabel("Median single-review latency (ms)")
        axis.grid(axis="x", alpha=0.22)
    fig.suptitle("Single-review inference latency, including preprocessing")
    fig.savefig(plot_dir / "single_review_latency.png", dpi=160)
    plt.close(fig)
    files.append("single_review_latency.png")

    batch_size = 128
    fig, ax = plt.subplots(figsize=(11, 6), constrained_layout=True)
    x = np.arange(len(MODEL_KEYS), dtype=float)
    width = 0.36
    for offset, device, color in (
        (-width / 2, "cpu", "#159B62"),
        (width / 2, "cuda", "#F28E2B"),
    ):
        values = []
        for model_id in MODEL_KEYS:
            rows = devices[model_id].get(device, {}).get("batch_inference", [])
            row = next(
                (item for item in rows if item.get("batch_size") == batch_size), {}
            )
            values.append(row.get("reviews_per_second"))
        ax.bar(
            x + offset,
            [np.nan if value is None else value for value in values],
            width,
            label=device.upper(),
            color=color,
        )
    ax.set_xticks(x, [MODEL_LABELS[key] for key in MODEL_KEYS], rotation=15, ha="right")
    ax.set_ylabel("Reviews per second")
    ax.set_title("Batch throughput at size 128")
    ax.grid(axis="y", alpha=0.22)
    ax.legend()
    fig.savefig(plot_dir / "batch_throughput.png", dpi=160)
    plt.close(fig)
    files.append("batch_throughput.png")

    fig, axes = plt.subplots(1, 2, figsize=(14, 5), constrained_layout=True)
    for axis, device in zip(axes, ("cpu", "cuda"), strict=True):
        for index, model_id in enumerate(MODEL_KEYS):
            rows = devices[model_id].get(device, {}).get("batch_inference", [])
            supported = [row for row in rows if row.get("available")]
            if supported:
                axis.plot(
                    [row["batch_size"] for row in supported],
                    [row["reviews_per_second"] for row in supported],
                    marker="o",
                    color=PLOT_COLORS[index],
                    label=MODEL_LABELS[model_id],
                )
        axis.set_title(f"{device.upper()} throughput")
        axis.set_xlabel("Batch size")
        axis.set_ylabel("Reviews per second")
        axis.set_xticks(BATCH_SIZES)
        axis.grid(alpha=0.22)
    axes[1].legend(fontsize="small")
    fig.suptitle("Batch-size scaling")
    fig.savefig(plot_dir / "batch_size_scaling.png", dpi=160)
    plt.close(fig)
    files.append("batch_size_scaling.png")

    neural_models = MODEL_KEYS[1:]
    fig, ax = plt.subplots(figsize=(10, 5), constrained_layout=True)
    x = np.arange(len(neural_models), dtype=float)
    width = 0.36
    for offset, device, color in (
        (-width / 2, "cpu", "#159B62"),
        (width / 2, "cuda", "#F28E2B"),
    ):
        values = []
        for model_id in neural_models:
            rows = devices[model_id].get(device, {}).get("batch_inference", [])
            row = next((item for item in rows if item.get("batch_size") == 128), {})
            values.append(row.get("reviews_per_second"))
        ax.bar(
            x + offset,
            [np.nan if value is None else value for value in values],
            width,
            label=device.upper(),
            color=color,
        )
    ax.set_xticks(x, [MODEL_LABELS[key] for key in neural_models])
    ax.set_ylabel("Reviews per second at batch size 128")
    ax.set_title("CPU and CUDA throughput for neural models")
    ax.grid(axis="y", alpha=0.22)
    ax.legend()
    fig.savefig(plot_dir / "cpu_cuda_throughput.png", dpi=160)
    plt.close(fig)
    files.append("cpu_cuda_throughput.png")

    test_track = quality["tracks"]["imdb_official_test_active_generation_saved"][
        "metrics_by_model"
    ]
    fig, ax = plt.subplots(figsize=(8, 5), constrained_layout=True)
    for index, model_id in enumerate(MODEL_KEYS):
        metric = test_track[model_id]
        ax.scatter(
            artifact_sizes[model_id] / (1024 * 1024),
            metric.get("f1") if metric.get("available") else np.nan,
            s=85,
            color=PLOT_COLORS[index],
            label=MODEL_LABELS[model_id],
        )
        ax.annotate(
            MODEL_LABELS[model_id],
            (artifact_sizes[model_id] / (1024 * 1024), metric.get("f1", np.nan)),
            fontsize=8,
        )
    ax.set_xlabel("Active inference artifact size (MiB)")
    ax.set_ylabel("Saved IMDb official-test F1")
    ax.set_title("Artifact size versus previously saved test F1")
    ax.grid(alpha=0.22)
    fig.savefig(plot_dir / "artifact_size_vs_f1.png", dpi=160)
    plt.close(fig)
    files.append("artifact_size_vs_f1.png")

    validation_track = quality["tracks"]["imdb_validation_recovery_selection"][
        "metrics_by_model"
    ]
    fig, ax = plt.subplots(figsize=(8, 5), constrained_layout=True)
    for index, model_id in enumerate(MODEL_KEYS):
        metric = validation_track[model_id]
        latency = (
            devices[model_id]
            .get("cpu", {})
            .get("single_review_latency", {})
            .get("median_ms")
        )
        if latency is None or not metric.get("available"):
            continue
        ax.scatter(
            latency,
            metric["f1"],
            s=85,
            color=PLOT_COLORS[index],
            label=MODEL_LABELS[model_id],
        )
        ax.annotate(MODEL_LABELS[model_id], (latency, metric["f1"]), fontsize=8)
    ax.set_xlabel("Active-generation CPU median latency (ms, includes preprocessing)")
    ax.set_ylabel("Recovery candidate IMDb validation F1")
    ax.set_title("Validation F1 versus active-refit inference latency")
    ax.text(
        0.02,
        0.02,
        "Candidate validation weights differ from active full-data refits.",
        transform=ax.transAxes,
        fontsize=8,
    )
    ax.grid(alpha=0.22)
    fig.savefig(plot_dir / "latency_vs_f1.png", dpi=160)
    plt.close(fig)
    files.append("latency_vs_f1.png")

    fig, axes = plt.subplots(1, 2, figsize=(14, 5), constrained_layout=True)
    cpu_names = []
    cpu_values = []
    cpu_colors = []
    cuda_names = []
    allocated = []
    reserved = []
    for index, model_id in enumerate(MODEL_KEYS):
        cpu_result = devices[model_id].get("cpu", {})
        memory = cpu_result.get("process_memory", {})
        if memory:
            cpu_names.append(MODEL_LABELS[model_id])
            cpu_values.append(
                max(
                    0,
                    memory["rss_peak_during_inference_bytes"]
                    - memory["rss_before_load_bytes"],
                )
                / (1024 * 1024)
            )
            cpu_colors.append(PLOT_COLORS[index])
        gpu_result = devices[model_id].get("cuda", {}).get("gpu_memory")
        if gpu_result:
            cuda_names.append(MODEL_LABELS[model_id])
            allocated.append(gpu_result["peak_allocated_bytes"] / (1024 * 1024))
            reserved.append(gpu_result["peak_reserved_bytes"] / (1024 * 1024))
    axes[0].barh(cpu_names, cpu_values, color=cpu_colors)
    axes[0].set_xlabel("Observed process RSS increase (MiB)")
    axes[0].set_title("CPU process memory delta")
    axes[0].grid(axis="x", alpha=0.22)
    x = np.arange(len(cuda_names), dtype=float)
    axes[1].bar(x - 0.18, allocated, width=0.36, label="Allocated", color="#159B62")
    axes[1].bar(x + 0.18, reserved, width=0.36, label="Reserved", color="#F28E2B")
    axes[1].set_xticks(x, cuda_names, rotation=15, ha="right")
    axes[1].set_ylabel("Peak CUDA memory (MiB)")
    axes[1].set_title("CUDA peak memory")
    axes[1].grid(axis="y", alpha=0.22)
    axes[1].legend()
    fig.suptitle("Memory measures are shown separately by device")
    fig.savefig(plot_dir / "memory_comparison.png", dpi=160)
    plt.close(fig)
    files.append("memory_comparison.png")

    cpu_batch128 = {
        model_id: next(
            (
                row.get("reviews_per_second")
                for row in devices[model_id].get("cpu", {}).get("batch_inference", [])
                if row.get("batch_size") == 128
            ),
            None,
        )
        for model_id in MODEL_KEYS
    }
    fig, ax = plt.subplots(figsize=(8, 5), constrained_layout=True)
    for index, model_id in enumerate(MODEL_KEYS):
        memory = devices[model_id].get("cpu", {}).get("process_memory", {})
        if not memory or cpu_batch128[model_id] is None:
            continue
        rss_delta = max(
            0,
            memory["rss_peak_during_inference_bytes"] - memory["rss_before_load_bytes"],
        ) / (1024 * 1024)
        ax.scatter(rss_delta, cpu_batch128[model_id], color=PLOT_COLORS[index], s=85)
        ax.annotate(
            MODEL_LABELS[model_id], (rss_delta, cpu_batch128[model_id]), fontsize=8
        )
    ax.set_xlabel("Observed CPU process RSS increase (MiB)")
    ax.set_ylabel("CPU reviews per second, batch 128")
    ax.set_title("CPU throughput versus process memory increase")
    ax.grid(alpha=0.22)
    fig.savefig(plot_dir / "throughput_vs_memory.png", dpi=160)
    plt.close(fig)
    files.append("throughput_vs_memory.png")
    return files


def _flatten_device_results(
    results: dict[str, dict[str, dict[str, Any]]],
    model_profiles: dict[str, dict[str, Any]],
    artifact_sizes: dict[str, int],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    loading_rows = []
    latency_rows = []
    memory_rows = []
    batch_rows = []
    for model_id in MODEL_KEYS:
        for device in ("cpu", "cuda"):
            result = results[model_id].get(
                device, _unavailable_device("Not requested.")
            )
            available = bool(result.get("available"))
            loading_rows.append(
                {
                    "model_id": model_id,
                    "model_name": MODEL_LABELS[model_id],
                    "generation_id": model_profiles[model_id]["generation_id"],
                    "device": device,
                    "device_name": result.get("gpu_memory", {}).get("device_name")
                    if result.get("gpu_memory")
                    else (platform.processor() or "CPU" if device == "cpu" else None),
                    "available": available,
                    "unavailable_reason": result.get("unavailable_reason"),
                    "model_initialization_seconds": result.get(
                        "model_initialization_seconds"
                    ),
                    "model_initialization_unavailable_reason": result.get(
                        "model_initialization_unavailable_reason"
                    ),
                    "artifact_loading_seconds": result.get("artifact_loading_seconds"),
                    "preprocessing_artifact_loading_included": result.get(
                        "preprocessing_artifact_loading_included"
                    ),
                    "first_prediction_cold_start_latency_ms": result.get(
                        "first_prediction_cold_start_latency_ms"
                    ),
                    "artifact_size_bytes": artifact_sizes[model_id],
                }
            )
            single = result.get("single_review_latency", {})
            forward = result.get("forward_only_single_review_latency", {})
            latency_rows.append(
                {
                    "model_id": model_id,
                    "model_name": MODEL_LABELS[model_id],
                    "device": device,
                    "available": available,
                    "unavailable_reason": result.get("unavailable_reason"),
                    "single_review_observations": single.get("observations"),
                    "single_review_mean_ms": single.get("mean_ms"),
                    "single_review_median_ms": single.get("median_ms"),
                    "single_review_p95_ms": single.get("p95_ms"),
                    "single_review_minimum_ms": single.get("minimum_ms"),
                    "single_review_maximum_ms": single.get("maximum_ms"),
                    "forward_only_mean_ms": forward.get("mean_ms"),
                    "forward_only_median_ms": forward.get("median_ms"),
                    "forward_only_p95_ms": forward.get("p95_ms"),
                    "forward_only_observations": forward.get("observations"),
                }
            )
            memory_rows.append(memory_measurement_record(model_id, device, result))
            for row in result.get("batch_inference", []):
                batch_rows.append(
                    {
                        "model_id": model_id,
                        "model_name": MODEL_LABELS[model_id],
                        "device": device,
                        **row,
                    }
                )
            if not result.get("batch_inference"):
                for batch_size in BATCH_SIZES:
                    batch_rows.append(
                        {
                            "model_id": model_id,
                            "model_name": MODEL_LABELS[model_id],
                            "device": device,
                            "batch_size": batch_size,
                            "available": False,
                            "unavailable_reason": result.get(
                                "unavailable_reason", "Device measurement unavailable."
                            ),
                            "total_reviews": None,
                            "mean_batch_latency_ms": None,
                            "mean_review_latency_ms": None,
                            "reviews_per_second": None,
                            "final_partial_batch_size": None,
                        }
                    )
    return (
        _frame_rows(loading_rows),
        _frame_rows(latency_rows),
        _frame_rows(memory_rows),
        _frame_rows(batch_rows),
    )


def _efficiency_awards(
    results: dict[str, dict[str, dict[str, Any]]],
    artifact_sizes: dict[str, int],
    quality: dict[str, Any],
) -> dict[str, Any]:
    def fastest(device: str) -> dict[str, Any] | None:
        candidates = []
        for model_id in MODEL_KEYS:
            latency = (
                results[model_id]
                .get(device, {})
                .get("single_review_latency", {})
                .get("median_ms")
            )
            if latency is not None:
                candidates.append((float(latency), model_id))
        if not candidates:
            return None
        value, model_id = min(candidates)
        return {
            "model_id": model_id,
            "model_name": MODEL_LABELS[model_id],
            "median_ms": value,
            "device": device,
        }

    throughput_candidates = []
    for model_id in MODEL_KEYS:
        for device, result in results[model_id].items():
            for row in result.get("batch_inference", []):
                if row.get("available") and row.get("reviews_per_second") is not None:
                    throughput_candidates.append(
                        (
                            float(row["reviews_per_second"]),
                            model_id,
                            device,
                            row["batch_size"],
                        )
                    )
    highest_throughput = None
    if throughput_candidates:
        rate, model_id, device, batch_size = max(throughput_candidates)
        highest_throughput = {
            "model_id": model_id,
            "model_name": MODEL_LABELS[model_id],
            "device": device,
            "batch_size": batch_size,
            "reviews_per_second": rate,
        }

    memory_candidates = []
    for model_id in MODEL_KEYS:
        memory = results[model_id].get("cpu", {}).get("process_memory", {})
        if memory:
            delta = max(
                0,
                memory["rss_peak_during_inference_bytes"]
                - memory["rss_before_load_bytes"],
            )
            memory_candidates.append((delta, model_id))
    lowest_memory = None
    if memory_candidates:
        amount, model_id = min(memory_candidates)
        lowest_memory = {
            "model_id": model_id,
            "model_name": MODEL_LABELS[model_id],
            "device": "cpu",
            "observed_process_rss_increase_bytes": amount,
            "interpretation": (
                "Sampled process RSS delta; runtime and shared libraries contribute."
            ),
        }

    validation = quality["tracks"]["imdb_validation_recovery_selection"][
        "metrics_by_model"
    ]
    cpu_latency = {
        model_id: results[model_id]
        .get("cpu", {})
        .get("single_review_latency", {})
        .get("median_ms")
        for model_id in MODEL_KEYS
    }

    def pareto(metric: str) -> list[str]:
        candidates = {
            model_id: (
                validation[model_id].get(metric),
                cpu_latency[model_id],
            )
            for model_id in MODEL_KEYS
            if validation[model_id].get("available")
            and validation[model_id].get(metric) is not None
            and cpu_latency[model_id] is not None
        }
        frontier = []
        for model_id, (score, latency) in candidates.items():
            dominated = any(
                other_id != model_id
                and other_score >= score
                and other_latency <= latency
                and (other_score > score or other_latency < latency)
                for other_id, (other_score, other_latency) in candidates.items()
            )
            if not dominated:
                frontier.append(model_id)
        return frontier

    return {
        "fastest_cpu_single_review": fastest("cpu"),
        "fastest_supported_cuda_single_review": fastest("cuda"),
        "highest_batch_throughput": highest_throughput,
        "lowest_cpu_process_rss_increase": lowest_memory,
        "smallest_inference_artifact": {
            "model_id": min(artifact_sizes, key=artifact_sizes.get),
            "model_name": MODEL_LABELS[min(artifact_sizes, key=artifact_sizes.get)],
            "artifact_size_bytes": min(artifact_sizes.values()),
        },
        "quality_efficiency_pareto": {
            "source_track": "IMDb recovery candidate validation",
            "latency_metric": (
                "Active-refit CPU median end-to-end single-review latency"
            ),
            "accuracy_frontier_model_ids": pareto("accuracy"),
            "f1_frontier_model_ids": pareto("f1"),
            "caveat": (
                "Validation candidate weights differ from active full-data refits."
            ),
        },
        "no_composite_score_created": True,
        "official_test_used_for_model_selection": False,
    }


def validate_saved_output(output: str | Path) -> dict[str, Any]:
    """Check required JSON, CSV, image, model, device, and batch result structure."""
    directory = Path(output)
    for filename in OUTPUT_JSON_FILES:
        payload = json.loads((directory / filename).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"{filename} must contain a JSON object.")
    csv_counts = {}
    for filename, required_columns in OUTPUT_CSV_COLUMNS.items():
        frame = pd.read_csv(directory / filename)
        missing = required_columns.difference(frame.columns)
        if missing:
            raise ValueError(f"{filename} is missing columns: {sorted(missing)}.")
        csv_counts[filename] = len(frame)

    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    benchmark = json.loads((directory / "benchmark.json").read_text(encoding="utf-8"))
    quality = json.loads((directory / "metrics.json").read_text(encoding="utf-8"))
    if set(benchmark.get("models", {})) != set(MODEL_KEYS):
        raise ValueError("The benchmark output must contain all five models.")
    required_tracks = {
        "imdb_validation_recovery_selection",
        "imdb_official_test_active_generation_saved",
        "uci_short_text_test_active_generation_saved",
        "phase15_semantic_challenge_historical",
        "phase15_automatic_robustness_historical",
        "short_text_diagnostic_active_generation",
    }
    if not required_tracks.issubset(set(quality.get("tracks", {}))):
        raise ValueError("One or more required quality tracks are missing.")

    batch_frame = pd.read_csv(directory / "batch_benchmark.csv")
    for model_id in MODEL_KEYS:
        model_rows = batch_frame[batch_frame.model_id == model_id]
        for device in ("cpu", "cuda"):
            device_rows = model_rows[model_rows.device == device]
            if set(device_rows.batch_size.astype(int)) != set(BATCH_SIZES):
                raise ValueError(f"Missing batch sizes for {model_id} on {device}.")
            unavailable = device_rows[~device_rows.available.astype(bool)]
            if not unavailable.empty and unavailable.unavailable_reason.isna().any():
                raise ValueError("Unavailable batch measurements require a reason.")

    plot_files = metadata.get("plot_files", [])
    if not plot_files:
        raise ValueError("The output manifest does not list generated plots.")
    from PIL import Image

    for filename in plot_files:
        image_path = directory / "plots" / filename
        with Image.open(image_path) as image_file:
            image_file.verify()
    if metadata.get("source_artifact_hashes_before") != metadata.get(
        "source_artifact_hashes_after"
    ):
        raise ValueError(
            "Active model artifact hashes differ before and after the run."
        )
    if metadata.get("active_generation_pointer_unchanged") is not True:
        raise ValueError("The active generation pointer changed during the audit.")
    return {"csv_row_counts": csv_counts, "plot_count": len(plot_files)}


def _human_report(
    generation_id: str,
    output: Path,
    quality_summary: dict[str, Any],
    awards: dict[str, Any],
    artifacts: dict[str, int],
) -> str:
    def ranking_text(key: str) -> str:
        ranking = quality_summary[key]
        leaders = (
            ", ".join(MODEL_LABELS[item] for item in ranking.get("leaders", []))
            or "unavailable"
        )
        value = ranking.get("value")
        value_text = "unavailable" if value is None else f"{value:.4f}"
        return f"{leaders} — {value_text} {ranking.get('metric', '')}"

    fastest_cpu = awards["fastest_cpu_single_review"]
    fastest_cuda = awards["fastest_supported_cuda_single_review"]
    throughput = awards["highest_batch_throughput"]
    memory = awards["lowest_cpu_process_rss_increase"]
    smallest = awards["smallest_inference_artifact"]
    fastest_cpu_text = (
        f"{MODEL_LABELS[fastest_cpu['model_id']]} ({fastest_cpu['median_ms']:.3f} ms)"
        if fastest_cpu
        else "unavailable"
    )
    fastest_cuda_text = (
        f"{MODEL_LABELS[fastest_cuda['model_id']]} ({fastest_cuda['median_ms']:.3f} ms)"
        if fastest_cuda
        else "unavailable"
    )
    throughput_text = (
        f"{MODEL_LABELS[throughput['model_id']]} "
        f"({throughput['reviews_per_second']:.1f} "
        f"reviews/s, {throughput['device']} batch {throughput['batch_size']})"
        if throughput
        else "unavailable"
    )
    memory_text = (
        f"{MODEL_LABELS[memory['model_id']]} "
        f"({memory['observed_process_rss_increase_bytes'] / (1024 * 1024):.2f} MiB)"
        if memory
        else "unavailable"
    )
    validation_text = ranking_text("best_imdb_validation_accuracy")
    active_test_text = ranking_text("best_recorded_imdb_test_accuracy")
    uci_text = ranking_text("best_uci_short_text_test_f1")
    semantic_text = ranking_text("best_semantic_challenge_f1")
    diagnostic_text = ranking_text("fewest_short_text_diagnostic_errors")
    robustness_text = ranking_text("most_consistent_automatic_robustness")
    smallest_text = (
        f"{MODEL_LABELS[smallest['model_id']]} "
        f"({artifacts[smallest['model_id']] / (1024 * 1024):.2f} MiB)"
    )
    return f"""# Phase 16 final model quality audit and efficiency benchmark

- Active generation: `{generation_id}`
- Output: `{output}`
- Benchmark kind: inference-only measurements on the manifest-selected
  active artifacts.
- Historical metrics: read from saved Phase 7, 12, 13, 15 and recovery reports.
  No official test reviews were loaded in this audit.
- Model training, tuning, threshold changes, preprocessing refits, and
  checkpoint edits: none.

## Quality leaders by separate track

- Best recorded IMDb validation accuracy: {validation_text}
- Best saved active-generation IMDb test accuracy: {active_test_text}
- Best UCI Amazon/Yelp test F1: {uci_text}
- Best Phase 15 semantic challenge F1: {semantic_text}
- Fewest active-generation diagnostic errors: {diagnostic_text}
- Highest Phase 15 automatic perturbation consistency: {robustness_text}

Rankings stay within their named datasets and model generations. No
composite score is used. The IMDb official test has prior project use and
was not used for model selection. Phase 15 semantic labels are author-intended
and unadjudicated.

## Efficiency leaders

- Fastest CPU single-review median: {fastest_cpu_text}
- Fastest supported CUDA single-review median: {fastest_cuda_text}
- Highest measured batch throughput: {throughput_text}
- Lowest observed CPU process RSS increase: {memory_text}
- Smallest active inference artifact: {smallest_text}

## Interpretation and limits

TF-IDF + Logistic Regression has its own CPU-only path and artifact format.
Neural single-review calls include tokenization, encoding and host/device
movement; CUDA startup and synchronization affect latency. CPU process RSS
includes Python and shared libraries, so the per-model delta is not isolated
model memory. CUDA allocation and reservation are reported separately. Batch
throughput uses the same 130-review workload, includes final partial batches,
and is hardware-specific.

IMDb validation selection scores describe candidate checkpoints; active
artifacts are full-data refits. The latency-versus-validation plot pairs
candidate validation quality with the corresponding active architecture's
refit latency and is labelled accordingly. There is no statistical
significance claim or universal model winner.

## Files

- Machine-readable quality tracks: `metrics.json`
- Benchmark configuration and measurements: `benchmark_config.json`, `benchmark.json`
- Consolidated summary: `model_quality_summary.json`
- Human-readable findings: `report.md`
- Visualizations: `plots/`
"""


def run_phase16_final_audit(
    *,
    project_root: str | Path | None = None,
    output_root: str | Path = "outputs/efficiency",
    config: BenchmarkConfig | None = None,
) -> dict[str, Any]:
    """Benchmark the active generation and consolidate saved quality reports."""
    root = (
        Path(project_root).resolve()
        if project_root is not None
        else Path(__file__).resolve().parents[3]
    )
    benchmark_config = config or BenchmarkConfig(
        sample_limit=130,
        seed=42,
        batch_sizes=BATCH_SIZES,
        warmup_batches=5,
        timed_repetitions=25,
        single_review_repetitions=100,
        devices=("cpu", "cuda"),
    )
    if benchmark_config.sample_limit < 128:
        raise ValueError("Phase 16 requires at least 128 workload examples.")

    run_started_utc = datetime.now(UTC)
    active = discover_active_generation(root)
    source_quality = collect_quality_sources(active)
    phase15_run, workload_records, phase15_metadata = _load_phase15_workload(root)
    sample = select_representative_sample(
        workload_records,
        sample_limit=benchmark_config.sample_limit,
        seed=benchmark_config.seed,
    )
    texts = sample.text.tolist()
    diagnostic_texts = [row[0] for row in DIAGNOSTIC_EXAMPLES]
    sample_fingerprint = hashlib.sha256(
        "\n".join(sample.review_id.astype(str)).encode("utf-8")
    ).hexdigest()

    sources = active["model_directories"]
    hashes_before = _directory_hashes(sources)
    pointer_hash_before = _sha256_file(active["pointer_path"])
    process = psutil.Process()
    process_rss_before = int(process.memory_info().rss)
    active_metadata = {
        key: json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        for key, directory in sources.items()
    }
    model_profiles = {
        key: {
            "generation_id": active["generation_id"],
            "architecture": active_metadata[key].get("model_name", MODEL_LABELS[key]),
            "model_config": active_metadata[key].get("selected_model_config", {}),
            "preprocessing": active_metadata[key].get("preprocessing", {}),
        }
        for key in MODEL_KEYS
    }
    artifacts = {
        key: sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())
        for key, directory in sources.items()
    }

    device_plan = supported_device_plan(
        benchmark_config.devices, torch.cuda.is_available()
    )
    device_results: dict[str, dict[str, dict[str, Any]]] = {
        key: {} for key in MODEL_KEYS
    }
    original_torch_threads = torch.get_num_threads()
    try:
        torch.set_num_threads(1)
        with threadpool_limits(limits=1):
            for model_id in MODEL_KEYS:
                for device in ("cpu", "cuda"):
                    if device not in benchmark_config.devices:
                        device_results[model_id][device] = _unavailable_device(
                            "Device was not selected in the benchmark configuration."
                        )
                        continue
                    if device == "cuda" and not torch.cuda.is_available():
                        device_results[model_id][device] = _unavailable_device(
                            "CUDA is not available on this machine."
                        )
                        continue
                    if device not in device_plan[model_id]:
                        device_results[model_id][device] = _unavailable_device(
                            "The sklearn TF-IDF pipeline supports CPU inference only."
                        )
                        continue
                    profile = {
                        model_id: {
                            "model_config": model_profiles[model_id]["model_config"]
                        }
                    }
                    initialization = _initialization_seconds(model_id, profile)
                    try:
                        result = _measure_device_model(
                            model_id,
                            sources[model_id],
                            texts,
                            benchmark_config,
                            device,
                            initialization,
                            diagnostic_texts=diagnostic_texts,
                        )
                        result["available"] = True
                        result["device_name"] = (
                            torch.cuda.get_device_name(0)
                            if device == "cuda"
                            else platform.processor() or "CPU"
                        )
                        device_results[model_id][device] = result
                    except torch.cuda.OutOfMemoryError as error:
                        if device != "cuda":
                            raise
                        torch.cuda.empty_cache()
                        device_results[model_id][device] = _unavailable_device(
                            "CUDA ran out of memory while loading or warming the "
                            f"model ({type(error).__name__})."
                        )
    finally:
        torch.set_num_threads(original_torch_threads)

    hashes_after = _directory_hashes(sources)
    pointer_hash_after = _sha256_file(active["pointer_path"])
    pointer_unchanged = pointer_hash_before == pointer_hash_after and active[
        "pointer"
    ] == json.loads(active["pointer_path"].read_text(encoding="utf-8"))
    if hashes_before != hashes_after:
        raise RuntimeError("An active model artifact changed during benchmarking.")
    if not pointer_unchanged:
        raise RuntimeError("The active generation pointer changed during benchmarking.")

    cpu_diagnostics = {
        key: device_results[key]["cpu"].get("diagnostic_predictions", [])
        for key in MODEL_KEYS
    }
    diagnostic_rows = _diagnostic_rows(cpu_diagnostics)
    quality = add_diagnostic_track(source_quality, diagnostic_rows, active)
    quality_summary = summarize_quality(quality)
    quality_summary["generation_id"] = active["generation_id"]
    awards = _efficiency_awards(device_results, artifacts, quality)
    quality_summary["efficiency_awards"] = awards

    cuda_parity = {}
    for model_id in MODEL_KEYS:
        cpu = device_results[model_id].get("cpu", {}).get("diagnostic_predictions")
        cuda = device_results[model_id].get("cuda", {}).get("diagnostic_predictions")
        if not cpu or not cuda:
            cuda_parity[model_id] = {
                "available": False,
                "within_tolerance": None,
                "unavailable_reason": device_results[model_id]
                .get("cuda", {})
                .get("unavailable_reason", "CUDA prediction results are unavailable."),
            }
            continue
        max_probability_delta = max(
            abs(
                float(left["positive_probability"])
                - float(right["positive_probability"])
            )
            for left, right in zip(cpu, cuda, strict=True)
        )
        labels_match = all(
            int(left["predicted_class"]) == int(right["predicted_class"])
            for left, right in zip(cpu, cuda, strict=True)
        )
        cuda_parity[model_id] = {
            "available": True,
            "labels_match": labels_match,
            "maximum_positive_probability_difference": max_probability_delta,
            "tolerance": 1e-4,
            "within_tolerance": max_probability_delta <= 1e-4 and labels_match,
        }

    loading, latency, memory, batch = _flatten_device_results(
        device_results, model_profiles, artifacts
    )
    quality_rows = _quality_rows(quality, device_results, artifacts)
    quality_frame = _frame_rows(quality_rows)
    output_base = Path(output_root)
    if not output_base.is_absolute():
        output_base = root / output_base
    output_base = output_base.resolve()
    run_timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output = output_base / f"imdb-efficiency-{run_timestamp}"
    output.mkdir(parents=True, exist_ok=False)
    quality["tracks"]["short_text_diagnostic_active_generation"][
        "source_artifact_location"
    ] = str((output / "diagnostic_predictions.csv").resolve())

    loading.to_csv(output / "loading_times.csv", index=False)
    latency.to_csv(output / "latency_summary.csv", index=False)
    batch.to_csv(output / "batch_benchmark.csv", index=False)
    memory.to_csv(output / "memory_summary.csv", index=False)
    quality_frame.to_csv(output / "efficiency_quality_comparison.csv", index=False)
    pd.DataFrame(diagnostic_rows).to_csv(
        output / "diagnostic_predictions.csv", index=False
    )

    config_json = {
        "seed": benchmark_config.seed,
        "sample_size": len(sample),
        "sample_method": (
            "Class-balanced deterministic length-stratified sample spanning "
            "short and long reviews."
        ),
        "sample_source": str(
            (phase15_run / "automatic_perturbations/original_predictions.csv").resolve()
        ),
        "batch_sizes": list(benchmark_config.batch_sizes),
        "warmup_passes_per_batch_size": benchmark_config.warmup_batches,
        "timed_full_sample_repetitions_per_batch_size": (
            benchmark_config.timed_repetitions
        ),
        "single_review_observations": benchmark_config.single_review_repetitions,
        "requested_devices": list(benchmark_config.devices),
        "cpu_torch_threads": 1,
        "cpu_threadpool_limit": 1,
        "timing_units": {
            "latency": "milliseconds",
            "loading": "seconds",
            "memory": "bytes",
            "throughput": "reviews/second",
        },
        "input_measurement_scope": (
            "End-to-end raw text inference includes each saved model's preprocessing."
        ),
        "forward_only_scope": (
            "Saved TF-IDF transformation or tokenization/encoding is prepared "
            "before timing; only classifier/model forward is timed."
        ),
        "cuda_synchronization": (
            "torch.cuda.synchronize before and after each timed operation."
        ),
        "inference_mode": (
            "Neural prediction uses torch.inference_mode; sklearn prediction "
            "does not track gradients."
        ),
        "final_partial_batch_included": True,
        "out_of_memory_policy": (
            "Record the failed batch and mark larger CUDA batches unavailable; "
            "never switch devices during a timed run."
        ),
    }
    hardware = {
        "platform": platform.platform(),
        "processor": platform.processor() or None,
        "cpu_logical_count": psutil.cpu_count(logical=True),
        "cpu_physical_count": psutil.cpu_count(logical=False),
        "torch_cpu_thread_count_during_measurement": 1,
        "threadpool_limits_during_measurement": threadpool_info(),
        "cuda_available": bool(torch.cuda.is_available()),
        "cuda_device": torch.cuda.get_device_name(0)
        if torch.cuda.is_available()
        else None,
    }
    software = {
        "python": platform.python_version(),
        "torch": str(torch.__version__),
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "scikit_learn": sklearn.__version__,
        "psutil": psutil.__version__,
    }

    source_tracks = quality["tracks"]
    historical_phase12_path, historical_phase12 = _latest_verified_phase12(root)
    historical_phase13_path, historical_phase13, _ = _latest_verified_phase13(root)
    metrics_payload = {
        "tracks": source_tracks,
        "source_inventory": quality["source_inventory"],
        "dashboard_compatibility_summaries": {
            "validation": historical_phase12["metrics"],
            "official_test": historical_phase13["metrics"],
            "sources": {
                "validation": str(historical_phase12_path.resolve()),
                "official_test": str(historical_phase13_path.resolve()),
                "historical_model_generation": True,
            },
        },
        "official_test_reviews_loaded_during_phase16": False,
        "quality_tracks_are_not_combined": True,
    }
    benchmark = {
        "phase": 16,
        "active_generation_id": active["generation_id"],
        "configuration": config_json,
        "sample": {
            "count": len(sample),
            "label_counts": {
                str(key): int(value)
                for key, value in sample.true_label.value_counts().sort_index().items()
            },
            "character_length_minimum": int(sample.text.str.len().min()),
            "character_length_median": float(sample.text.str.len().median()),
            "character_length_maximum": int(sample.text.str.len().max()),
            "review_ids_sha256": sample_fingerprint,
            "source_generation": phase15_run.name,
            "purpose": (
                "Latency workload only; labels are not evaluated or used for "
                "model selection."
            ),
        },
        "hardware": hardware,
        "software_versions": software,
        "models": {
            model_id: {
                "name": MODEL_LABELS[model_id],
                "architecture": model_profiles[model_id]["architecture"],
                "generation_id": active["generation_id"],
                "artifact_directory": str(sources[model_id].resolve()),
                "artifact_size_bytes": artifacts[model_id],
                "artifact_sha256": hashes_before[model_id],
                "preprocessing_configuration": model_profiles[model_id][
                    "preprocessing"
                ],
                "parameter_count": next(
                    (
                        device_results[model_id][device].get("parameter_count")
                        for device in ("cpu", "cuda")
                        if device_results[model_id].get(device, {}).get("available")
                    ),
                    None,
                ),
                "trainable_parameter_count": next(
                    (
                        device_results[model_id][device].get(
                            "trainable_parameter_count"
                        )
                        for device in ("cpu", "cuda")
                        if device_results[model_id].get(device, {}).get("available")
                    ),
                    None,
                ),
                "parameter_count_unavailable_reason": (
                    "Fitted TF-IDF/Logistic Regression pipeline parameters are "
                    "not directly comparable."
                    if model_id == "logistic_regression"
                    else None
                ),
                "devices": device_results[model_id],
            }
            for model_id in MODEL_KEYS
        },
        "cpu_cuda_diagnostic_parity": cuda_parity,
    }
    comparison = {
        "active_generation_id": active["generation_id"],
        "model_ids": list(MODEL_KEYS),
        "quality_metrics_by_separate_track": source_tracks,
        "quality_efficiency_rows": _records_without_nan(quality_frame),
        "efficiency_awards": awards,
        "cpu_cuda_diagnostic_parity": cuda_parity,
        "no_cross_dataset_metric_aggregation": True,
        "official_test_used_for_model_selection": False,
    }
    metadata = {
        "phase": 16,
        "benchmark_type": "active-generation final audit; frozen-model inference only",
        "created_at_utc": run_started_utc.isoformat(),
        "output_directory": str(output.resolve()),
        "active_generation_id": active["generation_id"],
        "active_generation_manifest": str(active["manifest_path"].resolve()),
        "active_generation_pointer": str(active["pointer_path"].resolve()),
        "active_generation_pointer_sha256_before": pointer_hash_before,
        "active_generation_pointer_sha256_after": pointer_hash_after,
        "active_generation_pointer_unchanged": pointer_unchanged,
        "source_artifacts": {key: str(path.resolve()) for key, path in sources.items()},
        "source_artifact_hashes_before": hashes_before,
        "source_artifact_hashes_after": hashes_after,
        "source_artifacts_unchanged": hashes_before == hashes_after,
        "official_test_reviews_loaded": False,
        "official_test_results_reused_from_saved_artifacts": True,
        "model_training_or_tuning_performed": False,
        "thresholds_changed": False,
        "model_weights_or_checkpoints_modified": False,
        "preprocessing_refitted": False,
        "previous_generations_modified": False,
        "process_rss_before_benchmark_bytes": process_rss_before,
        "hardware": hardware,
        "software_versions": software,
        "source_phase15_workload_run": str(phase15_run.resolve()),
        "source_phase15_artifacts_unchanged": phase15_metadata.get(
            "source_artifacts_unchanged"
        ),
        "phase_metric_sources": quality["source_inventory"],
        "plot_files": [],
        "limitations": [
            (
                "Phase 7, 12, 13 and 15 quality reports are historical and may "
                "refer to older artifacts."
            ),
            (
                "Active-generation IMDb validation scores describe candidate "
                "checkpoints before final refit."
            ),
            (
                "Active-generation official-test metrics are saved historical "
                "results on a previously used set; they were not rerun or used "
                "for model selection."
            ),
            (
                "Phase 15 semantic challenge labels are author-intended and not "
                "independently adjudicated."
            ),
            (
                "Process RSS includes Python and shared-library memory and is "
                "sampled during inference."
            ),
            (
                "Latency and throughput depend on hardware, drivers, software "
                "and workload; no significance claim is made."
            ),
        ],
    }

    plot_files = _save_plots(output, quality, device_results, artifacts)
    metadata["plot_files"] = plot_files
    _save_json(output / "benchmark_config.json", config_json)
    _save_json(output / "benchmark.json", benchmark)
    _save_json(output / "metrics.json", metrics_payload)
    _save_json(output / "comparison.json", comparison)
    _save_json(output / "model_quality_summary.json", quality_summary)
    _save_json(output / "metadata.json", metadata)
    (output / "report.md").write_text(
        _human_report(
            active["generation_id"], output, quality_summary, awards, artifacts
        ),
        encoding="utf-8",
        newline="\n",
    )
    output_validation = validate_saved_output(output)
    return {
        "output_directory": str(output.resolve()),
        "active_generation_id": active["generation_id"],
        "metadata": metadata,
        "quality_summary": quality_summary,
        "efficiency_awards": awards,
        "output_validation": output_validation,
    }


def _load_phase15_workload(root: Path) -> tuple[Path, pd.DataFrame, dict[str, Any]]:
    from deeptext.experiments.efficiency import _latest_robustness_sample

    return _latest_robustness_sample(root)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", default="outputs/efficiency")
    parser.add_argument("--sample-limit", type=int, default=130)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--device", choices=("cpu", "both"), default="both")
    arguments = parser.parse_args()
    devices = ("cpu", "cuda") if arguments.device == "both" else ("cpu",)
    config = BenchmarkConfig(
        sample_limit=arguments.sample_limit,
        seed=arguments.seed,
        batch_sizes=BATCH_SIZES,
        warmup_batches=5,
        timed_repetitions=25,
        single_review_repetitions=100,
        devices=devices,
    )
    result = run_phase16_final_audit(
        output_root=arguments.output_root,
        config=config,
    )
    print(
        json.dumps(
            {
                "output_directory": result["output_directory"],
                "active_generation_id": result["active_generation_id"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
