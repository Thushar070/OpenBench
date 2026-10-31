from pathlib import Path

import numpy as np
import pytest

from deeptext.evaluation.comparison import (
    available_history_metrics,
    benchmark_batch_throughput,
    benchmark_single_review_latency,
    compare_error_categories,
    evaluate_comparison_model,
    required_artifact_sizes,
)


def test_comparison_metrics_use_positive_class_and_expected_confusion_order() -> None:
    result = evaluate_comparison_model(
        [0, 0, 1, 1],
        [0, 1, 0, 1],
        positive_probabilities=[0.1, 0.7, 0.4, 0.8],
    )

    assert result["confusion_matrix"] == [[1, 1], [1, 1]]
    assert result["confusion_matrix_labels"] == [0, 1]
    assert result["true_negative"] == 1
    assert result["false_positive"] == 1
    assert result["false_negative"] == 1
    assert result["true_positive"] == 1
    assert result["precision"] == pytest.approx(0.5)
    assert result["recall"] == pytest.approx(0.5)
    assert result["f1"] == pytest.approx(0.5)
    assert result["positive_class"] == 1
    assert result["zero_division"] == 0


def test_comparison_metrics_reject_prediction_length_mismatch() -> None:
    with pytest.raises(ValueError, match="equal lengths"):
        evaluate_comparison_model([0, 1], [1])


def test_error_categories_count_joint_and_disagreeing_cases() -> None:
    result = compare_error_categories(
        [0, 1, 1, 0, 1, 0],
        [0, 0, 1, 1, 0, 1],
        [0, 1, 0, 0, 0, 1],
    )

    assert result == {
        "both_correct": 1,
        "baseline_only_correct": 1,
        "textcnn_only_correct": 2,
        "both_wrong_same_prediction": 2,
        "different_predictions": 3,
    }
    with pytest.raises(ValueError, match="equal lengths"):
        compare_error_categories([0], [0], [0, 1])


def test_single_review_benchmark_reports_repetitions_and_units() -> None:
    seen: list[str] = []

    result = benchmark_single_review_latency(
        seen.append,
        ["first", "second"],
        warmup_count=1,
        repetitions=2,
    )

    assert seen.count("first") == 3
    assert seen.count("second") == 2
    assert result["timed_observations"] == 4
    assert result["batch_size"] == 1
    assert result["device"] == "cpu"
    assert result["end_to_end_includes_preprocessing"] is True
    assert result["mean_latency_ms"] >= 0
    assert result["median_latency_ms"] >= 0


def test_batch_benchmark_uses_batches_and_reports_throughput() -> None:
    seen_batches: list[list[str]] = []

    def predict_batch(reviews: list[str]) -> list[str]:
        seen_batches.append(reviews)
        return reviews

    result = benchmark_batch_throughput(
        predict_batch,
        ["a", "b", "c"],
        batch_size=2,
        warmup_count=1,
        repetitions=2,
    )

    assert seen_batches[0] == ["a", "b"]
    assert result["sample_count_per_repetition"] == 3
    assert result["batch_size"] == 2
    assert result["batch_count_per_repetition"] == 2
    assert result["samples_per_second"] > 0
    assert result["end_to_end_includes_preprocessing"] is True


def test_benchmarks_reject_invalid_arguments() -> None:
    with pytest.raises(ValueError, match="warmup_count"):
        benchmark_single_review_latency(lambda _text: None, ["review"], warmup_count=-1)
    with pytest.raises(ValueError, match="batch_size"):
        benchmark_batch_throughput(lambda _batch: None, ["review"], batch_size=0)
    with pytest.raises(ValueError, match="at least one"):
        benchmark_batch_throughput(lambda _batch: None, [])


def test_artifact_sizes_include_only_named_required_files(tmp_path: Path) -> None:
    weights = tmp_path / "model.bin"
    vocabulary = tmp_path / "vocabulary.json"
    weights.write_bytes(b"weights")
    vocabulary.write_text("{}", encoding="utf-8")

    result = required_artifact_sizes({"weights": weights, "vocabulary": vocabulary})

    assert result == {
        "files": {"weights": 7, "vocabulary": 2},
        "total_bytes": 9,
    }
    with pytest.raises(FileNotFoundError, match="not found"):
        required_artifact_sizes({"missing": tmp_path / "missing.bin"})


def test_learning_curve_metrics_omit_unrecorded_fields() -> None:
    fields = available_history_metrics(
        [
            {"epoch": 1, "training_loss": 0.8, "validation_loss": 0.9},
            {"epoch": 2, "training_loss": 0.5, "validation_loss": 0.7},
        ]
    )

    assert fields == ("training_loss", "validation_loss")
    with pytest.raises(ValueError, match="at least one epoch"):
        available_history_metrics([])


def test_positive_probabilities_are_validated() -> None:
    with pytest.raises(ValueError, match="finite values"):
        evaluate_comparison_model([0, 1], [0, 1], [0.2, np.nan])
