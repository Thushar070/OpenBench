import numpy as np
import pandas as pd
import pytest

from deeptext.experiments.final_evaluation import (
    _error_analysis,
    _validate_prediction_alignment,
    _validate_test_records,
    _validation_test_rows,
)


def _test_records(dataset_directory, labels=(0, 0, 1, 1)) -> pd.DataFrame:
    rows = []
    for index, label in enumerate(labels):
        sentiment = "neg" if label == 0 else "pos"
        review_id = f"test/{sentiment}/{index}.txt"
        review_directory = dataset_directory / "test" / sentiment
        review_directory.mkdir(parents=True, exist_ok=True)
        (review_directory / f"{index}.txt").write_text(
            f"review {index}", encoding="utf-8"
        )
        rows.append(
            {
                "text": f"review {index}",
                "label": label,
                "split": "test",
                "review_id": review_id,
            }
        )
    return pd.DataFrame(rows)


def test_test_record_validation_checks_source_labels_and_counts(tmp_path) -> None:
    records = _test_records(tmp_path)

    summary = _validate_test_records(
        records,
        tmp_path,
        expected_class_counts={0: 2, 1: 2},
    )

    assert summary["sample_count"] == 4
    assert summary["class_distribution"] == {"negative": 2, "positive": 2}
    assert summary["review_ids_unique"] is True
    assert len(summary["review_id_order_sha256"]) == 64


def test_test_record_validation_rejects_mismatched_labels_and_counts(
    tmp_path,
) -> None:
    records = _test_records(tmp_path)
    wrong_label = records.copy()
    wrong_label.loc[0, "label"] = 1
    with pytest.raises(ValueError, match="Label does not match"):
        _validate_test_records(
            wrong_label, tmp_path, expected_class_counts={0: 2, 1: 2}
        )

    with pytest.raises(ValueError, match="class counts"):
        _validate_test_records(records, tmp_path, expected_class_counts={0: 3, 1: 1})


def test_test_record_validation_rejects_non_test_ids_and_empty_text(tmp_path) -> None:
    records = _test_records(tmp_path)
    wrong_split = records.copy()
    wrong_split.loc[0, "split"] = "train"
    with pytest.raises(ValueError, match="official test split"):
        _validate_test_records(
            wrong_split, tmp_path, expected_class_counts={0: 2, 1: 2}
        )

    empty_text = records.copy()
    empty_text.loc[0, "text"] = "  "
    with pytest.raises(ValueError, match="empty text"):
        _validate_test_records(empty_text, tmp_path, expected_class_counts={0: 2, 1: 2})


def test_prediction_alignment_rejects_missing_or_invalid_predictions(tmp_path) -> None:
    records = _test_records(tmp_path)
    labels = records["label"].to_numpy()

    _validate_prediction_alignment(records, labels, labels)
    with pytest.raises(ValueError, match="exactly once"):
        _validate_prediction_alignment(records, labels[:-1], labels)
    with pytest.raises(ValueError, match="binary integers"):
        _validate_prediction_alignment(records, np.full(4, 2), labels)


def test_validation_test_comparison_reports_signed_percentage_point_gap() -> None:
    validation = {
        "logistic_regression": {
            "accuracy": 0.8,
            "precision": 0.7,
            "recall": 0.9,
            "f1": 0.8,
        },
        "textcnn": {"accuracy": 0.75, "precision": 0.65, "recall": 0.85, "f1": 0.74},
    }
    test = {
        "logistic_regression": {
            "accuracy": 0.78,
            "precision": 0.72,
            "recall": 0.86,
            "f1": 0.785,
        },
        "textcnn": {"accuracy": 0.77, "precision": 0.63, "recall": 0.87, "f1": 0.73},
    }

    rows = _validation_test_rows(validation, test)
    accuracy_rows = [row for row in rows if row["metric"] == "accuracy"]

    assert len(rows) == 8
    assert accuracy_rows[0]["difference_percentage_points"] == pytest.approx(2.0)
    assert accuracy_rows[1]["difference_percentage_points"] == pytest.approx(-2.0)


def test_error_analysis_counts_model_and_joint_errors_without_full_dataset_text() -> (
    None
):
    records = pd.DataFrame(
        {
            "review_id": [f"test/neg/{index}.txt" for index in range(8)],
            "text": [f"review text {index}" for index in range(8)],
        }
    )
    labels = np.array([0, 0, 1, 1, 0, 1, 0, 1])
    baseline = np.array([0, 1, 1, 0, 1, 0, 0, 1])
    textcnn = np.array([1, 0, 1, 1, 0, 0, 1, 1])
    baseline_probabilities = np.linspace(0.1, 0.8, 8)
    textcnn_probabilities = np.linspace(0.2, 0.9, 8)

    summary, examples, joint_categories = _error_analysis(
        records,
        labels,
        baseline,
        baseline_probabilities,
        textcnn,
        textcnn_probabilities,
    )

    counts = summary["counts"]
    assert counts["both_correct"] == 2
    assert counts["baseline_only_correct"] == 2
    assert counts["textcnn_only_correct"] == 3
    assert counts["both_wrong_same_prediction"] == 1
    assert counts["different_predictions"] == 5
    assert counts["logistic_regression_false_positives"] == 2
    assert counts["textcnn_false_negatives"] == 1
    assert len(examples) <= 6
    assert examples["review_text_snippet"].str.len().max() <= 400
    assert len(joint_categories) == len(labels)
    assert set(examples["review_id"]).issubset(set(records["review_id"]))
