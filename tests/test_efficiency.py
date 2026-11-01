"""Small, deterministic tests for Phase 16 benchmark helpers."""

import json
from pathlib import Path

import pandas as pd
import pytest
from PIL import Image

from deeptext.experiments.efficiency import (
    BATCH_SIZES,
    MODEL_KEYS,
    BenchmarkConfig,
    _directory_hashes,
    _predict_batch,
    _summary_rows,
    select_benchmark_sample,
    summarize_batch_measurements,
    summarize_latencies,
)
from deeptext.experiments.phase_16_final_audit import (
    OUTPUT_CSV_COLUMNS,
    OUTPUT_JSON_FILES,
    load_active_predictors,
    memory_measurement_record,
    select_representative_sample,
    supported_device_plan,
    validate_saved_output,
)
from deeptext.experiments.phase_16_quality import (
    DIAGNOSTIC_EXAMPLES,
    _normalize_metrics,
    add_diagnostic_track,
    collect_quality_sources,
    discover_active_generation,
    summarize_quality,
)


def test_benchmark_configuration_rejects_invalid_batch_and_timing_settings():
    with pytest.raises(ValueError, match="batch_sizes"):
        BenchmarkConfig(batch_sizes=(1, 16))
    with pytest.raises(ValueError, match="timed repetitions"):
        BenchmarkConfig(timed_repetitions=4)
    with pytest.raises(ValueError, match="sample_limit"):
        BenchmarkConfig(sample_limit=True)
    assert BenchmarkConfig().batch_sizes == BATCH_SIZES


def test_validation_sample_is_repeatable_balanced_and_without_replacement():
    records = pd.DataFrame(
        [
            {
                "review_id": f"review-{label}-{index}",
                "true_label": label,
                "text": f"text {index}",
            }
            for label in (0, 1)
            for index in range(40)
        ]
    )

    first = select_benchmark_sample(records, sample_limit=32, seed=12)
    second = select_benchmark_sample(records, sample_limit=32, seed=12)

    pd.testing.assert_frame_equal(first, second)
    assert first.review_id.is_unique
    assert first.true_label.value_counts().to_dict() == {0: 16, 1: 16}
    with pytest.raises(ValueError, match="available row count"):
        select_benchmark_sample(records, sample_limit=81)


def test_latency_summary_uses_individual_calls_and_p95_threshold():
    values = [float(index) for index in range(1, 101)]
    summary = summarize_latencies(values)

    assert summary["observations"] == 100
    assert summary["mean_ms"] == pytest.approx(50.5)
    assert summary["median_ms"] == pytest.approx(50.5)
    assert summary["p95_ms"] == 96.0
    assert summarize_latencies([1.0, 2.0, 3.0])["p95_ms"] is None
    with pytest.raises(ValueError, match="finite observation"):
        summarize_latencies([1.0, float("nan")])


def test_summary_keeps_validation_and_test_metrics_separate_and_marks_unavailable():
    results = {
        key: {
            "cpu": {
                "available": True,
                "single_review_latency": {
                    "mean_ms": 2.0,
                    "median_ms": 1.5,
                    "p95_ms": 3.0,
                },
                "batch_inference": [{"batch_size": 128, "reviews_per_second": 40.0}],
                "artifact_loading_seconds": 1.0,
                "model_initialization_seconds": 0.1,
                "first_prediction_cold_start_latency_ms": 8.0,
                "process_memory": {
                    "rss_before_load_bytes": 100,
                    "rss_after_load_bytes": 200,
                    "rss_peak_during_inference_bytes": 250,
                },
                "gpu_memory": None,
                "parameter_count": 10,
            },
            "cuda": {"available": False, "unavailable_reason": "not available"},
        }
        for key in MODEL_KEYS
    }
    phase12 = {
        "metrics": {key: {"accuracy": 0.7, "f1": 0.6} for key in MODEL_KEYS},
        "model_profiles": {
            key: {"parameter_count": 10, "training_duration_seconds": None}
            for key in MODEL_KEYS
        },
    }
    phase13 = {"metrics": {key: {"accuracy": 0.8, "f1": 0.75} for key in MODEL_KEYS}}

    summary, batches, scores = _summary_rows(
        results, phase12, phase13, {key: 1_000 for key in MODEL_KEYS}
    )

    assert len(summary) == 2 * len(MODEL_KEYS)
    assert len(batches) == len(MODEL_KEYS)
    assert scores.validation_accuracy.eq(0.7).all()
    assert scores.test_accuracy.eq(0.8).all()
    assert summary[summary.device == "cuda"].available.eq(False).all()
    assert summary[summary.device == "cuda"].unavailable_reason.notna().all()
    assert (
        summary[summary.device == "cpu"]
        .throughput_batch_128_reviews_per_second.eq(40)
        .all()
    )


def test_directory_hashes_detect_artifact_changes(tmp_path: Path):
    artifact = tmp_path / "model.bin"
    artifact.write_bytes(b"first")
    before = _directory_hashes({"model": tmp_path})
    artifact.write_bytes(b"second")
    after = _directory_hashes({"model": tmp_path})

    assert before != after
    assert set(before["model"]) == {"model.bin"}


def test_batch_summary_includes_final_partial_batch_and_throughput():
    summary = summarize_batch_measurements(
        batch_size=8,
        sample_count=10,
        timed_repetitions=2,
        batch_latencies_ms=[10.0, 20.0, 30.0, 40.0],
    )

    assert summary["timed_batches"] == 4
    assert summary["total_reviews"] == 20
    assert summary["mean_batch_latency_ms"] == pytest.approx(25.0)
    assert summary["mean_review_latency_ms"] == pytest.approx(5.0)
    assert summary["reviews_per_second"] == pytest.approx(200.0)
    assert summary["final_partial_batch_size"] == 2
    with pytest.raises(ValueError, match="observations"):
        summarize_batch_measurements(
            batch_size=8,
            sample_count=10,
            timed_repetitions=2,
            batch_latencies_ms=[10.0],
        )


def test_device_plan_keeps_logistic_cpu_only_and_cuda_explicit():
    plan = supported_device_plan(("cpu", "cuda"), cuda_available=True)

    assert plan["logistic_regression"] == ["cpu"]
    assert plan["textcnn"] == ["cpu", "cuda"]
    assert plan["rnn"] == ["cpu", "cuda"]
    assert supported_device_plan(("cpu", "cuda"), False)["gru"] == ["cpu"]
    with pytest.raises(ValueError, match="devices"):
        supported_device_plan(("gpu",), cuda_available=True)


def test_active_generation_discovery_checks_manifest_artifacts(tmp_path: Path):
    outputs = tmp_path / "outputs" / "models"
    generation = outputs / "generation-a"
    models = {}
    for model_id in MODEL_KEYS:
        model_dir = generation / "models" / model_id
        model_dir.mkdir(parents=True)
        required = (
            ("baseline.joblib", "metadata.json")
            if model_id == "logistic_regression"
            else ("best_model.pt", "vocabulary.json", "metadata.json")
        )
        for filename in required:
            file_path = model_dir / filename
            if filename == "metadata.json":
                file_path.write_text(
                    '{"generation_id":"generation-a"}', encoding="utf-8"
                )
            else:
                file_path.write_bytes(b"artifact")
        models[model_id] = f"models/{model_id}"
    (generation / "generation_manifest.json").write_text(
        json.dumps({"generation_id": "generation-a", "models": models}),
        encoding="utf-8",
    )
    (outputs / "active_generation.json").write_text(
        json.dumps(
            {
                "generation_id": "generation-a",
                "generation_manifest": "generation-a/generation_manifest.json",
            }
        ),
        encoding="utf-8",
    )

    active = discover_active_generation(tmp_path)

    assert active["generation_id"] == "generation-a"
    assert set(active["model_directories"]) == set(MODEL_KEYS)
    (generation / "models/gru/vocabulary.json").unlink()
    with pytest.raises(FileNotFoundError, match="gru artifacts"):
        discover_active_generation(tmp_path)


def test_length_stratified_sample_is_balanced_repeatable_and_spans_lengths():
    rows = [
        {
            "review_id": f"{label}-{index}",
            "true_label": label,
            "text": "x" * (index + 1),
        }
        for label in (0, 1)
        for index in range(100)
    ]
    records = pd.DataFrame(rows)

    first = select_representative_sample(records, 130, seed=42)
    second = select_representative_sample(records, 130, seed=42)

    pd.testing.assert_frame_equal(first, second)
    assert len(first) == 130
    assert first.review_id.is_unique
    assert first.true_label.value_counts().to_dict() == {0: 65, 1: 65}
    assert first.text.str.len().min() <= 2
    assert first.text.str.len().max() >= 98


def test_quality_tracks_keep_sources_and_missing_metrics_explicit():
    missing = _normalize_metrics(None)
    assert missing["available"] is False
    assert missing["accuracy"] is None
    assert missing["unavailable_reason"]

    root = Path(__file__).resolve().parents[1]
    active = discover_active_generation(root)
    quality = collect_quality_sources(active)

    validation = quality["tracks"]["imdb_validation_recovery_selection"]
    historical = quality["tracks"]["imdb_validation_phase12_historical"]
    phase7 = quality["tracks"]["imdb_official_test_phase7_historical"]
    assert validation["source_model_generation"] == active["generation_id"]
    assert historical["source_model_generation"] != active["generation_id"]
    assert validation["dataset_id"] == historical["dataset_id"]
    assert validation["split"] != historical["split"]
    assert phase7["metrics_by_model"]["rnn"]["available"] is False
    assert phase7["metrics_by_model"]["rnn"]["accuracy"] is None


def test_diagnostic_quality_summary_does_not_score_semantic_examples(tmp_path: Path):
    source_tracks = {
        "generation_id": "generation-a",
        "tracks": {},
        "source_inventory": {},
    }
    for track_id, metric_name in (
        ("imdb_validation_recovery_selection", "accuracy"),
        ("imdb_validation_phase12_historical", "accuracy"),
        ("imdb_official_test_active_generation_saved", "accuracy"),
        ("uci_short_text_test_active_generation_saved", "f1"),
        ("phase15_semantic_challenge_historical", "f1"),
        ("phase15_automatic_robustness_historical", "prediction_consistency"),
    ):
        source_tracks["tracks"][track_id] = {
            "dataset_id": track_id,
            "split": track_id,
            "source_model_generation": "generation-a",
            "metrics_by_model": {
                key: {
                    "available": True,
                    metric_name: 0.8,
                    "accuracy": 0.8,
                    "f1": 0.8,
                    "prediction_consistency": 0.99,
                }
                for key in MODEL_KEYS
            },
        }
    prediction_rows = []
    for model_id in MODEL_KEYS:
        for index, (text, expected, group) in enumerate(DIAGNOSTIC_EXAMPLES):
            predicted = expected if expected is not None else index % 2
            if model_id == "rnn" and text == "this movie is excellent":
                predicted = 0
            prediction_rows.append(
                {
                    "model_id": model_id,
                    "text": text,
                    "example_group": group,
                    "expected_label": expected,
                    "predicted_label": predicted,
                }
            )
    active = {
        "generation_id": "generation-a",
        "generation_root": tmp_path,
        "model_directories": {key: tmp_path / key for key in MODEL_KEYS},
    }

    add_diagnostic_track(source_tracks, prediction_rows, active)
    summary = summarize_quality(source_tracks)
    metrics = source_tracks["tracks"]["short_text_diagnostic_active_generation"][
        "metrics_by_model"
    ]

    assert metrics["rnn"]["evaluated_examples"] == 12
    assert metrics["rnn"]["error_count"] == 1
    assert metrics["textcnn"]["error_count"] == 0
    assert summary["fewest_short_text_diagnostic_errors"]["leaders"] == [
        "logistic_regression",
        "textcnn",
        "lstm",
        "gru",
    ]


def test_memory_result_preserves_unavailable_gpu_values():
    available = memory_measurement_record(
        "textcnn",
        "cuda",
        {
            "available": True,
            "process_memory": {
                "rss_before_load_bytes": 100,
                "rss_after_load_bytes": 150,
                "rss_peak_during_inference_bytes": 200,
            },
            "gpu_memory": {
                "peak_allocated_bytes": 300,
                "peak_reserved_bytes": 400,
                "device_name": "test-gpu",
            },
        },
    )
    unavailable = memory_measurement_record(
        "gru", "cuda", {"available": False, "unavailable_reason": "CUDA OOM"}
    )

    assert available["observed_rss_increase_bytes"] == 100
    assert available["peak_gpu_allocated_bytes"] == 300
    assert unavailable["peak_gpu_allocated_bytes"] is None
    assert unavailable["unavailable_reason"] == "CUDA OOM"


def test_saved_output_json_csv_and_plot_structure(tmp_path: Path):
    (tmp_path / "plots").mkdir()
    for filename in OUTPUT_JSON_FILES:
        if filename == "metadata.json":
            payload = {
                "plot_files": ["fixture.png"],
                "source_artifact_hashes_before": {"a": "same"},
                "source_artifact_hashes_after": {"a": "same"},
                "active_generation_pointer_unchanged": True,
            }
        elif filename == "benchmark.json":
            payload = {"models": {key: {} for key in MODEL_KEYS}}
        elif filename == "metrics.json":
            payload = {
                "tracks": {
                    key: {}
                    for key in (
                        "imdb_validation_recovery_selection",
                        "imdb_official_test_active_generation_saved",
                        "uci_short_text_test_active_generation_saved",
                        "phase15_semantic_challenge_historical",
                        "phase15_automatic_robustness_historical",
                        "short_text_diagnostic_active_generation",
                    )
                }
            }
        else:
            payload = {}
        (tmp_path / filename).write_text(json.dumps(payload), encoding="utf-8")

    for filename, columns in OUTPUT_CSV_COLUMNS.items():
        rows = []
        if filename == "batch_benchmark.csv":
            for model_id in MODEL_KEYS:
                for device in ("cpu", "cuda"):
                    for batch_size in BATCH_SIZES:
                        rows.append(
                            {
                                "model_id": model_id,
                                "device": device,
                                "batch_size": batch_size,
                                "available": False,
                                "unavailable_reason": "fixture unavailable",
                                "total_reviews": None,
                                "mean_batch_latency_ms": None,
                                "mean_review_latency_ms": None,
                                "reviews_per_second": None,
                                "final_partial_batch_size": None,
                            }
                        )
        else:
            rows.append({column: None for column in columns})
        pd.DataFrame(rows, columns=sorted(columns) if not rows else None).to_csv(
            tmp_path / filename, index=False
        )
    Image.new("RGB", (8, 8), color=(20, 150, 90)).save(tmp_path / "plots/fixture.png")

    result = validate_saved_output(tmp_path)

    assert result["plot_count"] == 1
    assert result["csv_row_counts"]["batch_benchmark.csv"] == 5 * 2 * len(BATCH_SIZES)


def test_active_generation_artifact_integration_loads_all_models_and_preserves_hashes():
    root = Path(__file__).resolve().parents[1]
    active, predictors = load_active_predictors(root, device="cpu")
    before = _directory_hashes(active["model_directories"])
    for model_id in MODEL_KEYS:
        prediction = _predict_batch(
            model_id, predictors[model_id], ["A good film."], 1
        )[0]
        assert prediction["predicted_class"] in (0, 1)
        assert 0 <= prediction["negative_probability"] <= 1
        assert 0 <= prediction["positive_probability"] <= 1
        assert sum(
            prediction[key] for key in ("negative_probability", "positive_probability")
        ) == pytest.approx(1.0, abs=1e-5)
    after = _directory_hashes(active["model_directories"])
    assert before == after
