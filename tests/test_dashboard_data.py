import json

from deeptext.dashboard.data import load_dashboard_data


def _write_json(path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _metrics(accuracy: float, matrix: list[list[int]]) -> dict[str, object]:
    return {
        "accuracy": accuracy,
        "precision": accuracy,
        "recall": accuracy,
        "f1": accuracy,
        "confusion_matrix": matrix,
    }


def _make_outputs(root, *, valid_test_metadata: bool = True) -> None:
    _write_json(
        root / "baseline" / "run-a" / "metadata.json",
        {
            "experiment_id": "run-a",
            "created_at_utc": "2026-10-30T11:00:00Z",
            "official_test_evaluated": False,
            "validation_metrics": _metrics(0.8, [[2, 0], [0, 2]]),
        },
    )
    _write_json(
        root / "textcnn" / "run-b" / "metadata.json",
        {
            "experiment_id": "run-b",
            "created_at_utc": "2026-10-30T12:00:00Z",
            "official_test_evaluated": False,
            "best_validation_metrics": _metrics(0.7, [[2, 0], [0, 2]]),
        },
    )
    _write_json(
        root / "comparison" / "run-c" / "comparison.json",
        {
            "comparison_id": "run-c",
            "created_at_utc": "2026-10-30T13:00:00Z",
            "validation_only": True,
            "official_test_evaluated": False,
            "sample_count": 4,
            "metrics": {
                "logistic_regression": _metrics(0.8, [[2, 0], [0, 2]]),
                "textcnn": _metrics(0.7, [[2, 0], [0, 2]]),
            },
        },
    )
    _write_json(
        root / "final_evaluation" / "run-d" / "final_results.json",
        {
            "evaluation_id": "run-d",
            "official_test_evaluated": True,
            "sample_count": 4,
            "class_distribution": {"negative": 2, "positive": 2},
            "prediction_alignment": {
                "same_test_records": True,
                "same_ground_truth_labels": True,
                "same_original_order": True,
                "baseline_prediction_count": 4,
                "textcnn_prediction_count": 4,
            },
            "metrics": {
                "logistic_regression": _metrics(0.75, [[1, 1], [0, 2]]),
                "textcnn": _metrics(0.5, [[1, 1], [1, 1]]),
            },
        },
    )
    _write_json(
        root / "final_evaluation" / "run-d" / "metadata.json",
        {
            "evaluation_id": "run-d",
            "official_test_evaluated": True,
            "dataset_split": "test",
            "dataset_validation": {
                "record_source_verified": True,
                "sample_count": 4,
            },
            "training_started": False,
            "preprocessing_refitted": False,
            "test_based_model_selection": False,
            "artifacts_unchanged": valid_test_metadata,
            "artifact_sha256_before": {"baseline": "abc"},
            "artifact_sha256_after": {"baseline": "abc"},
        },
    )


def test_dashboard_data_uses_verified_test_results_and_counts_saved_runs(
    tmp_path,
) -> None:
    outputs = tmp_path / "outputs"
    _make_outputs(outputs)

    data = load_dashboard_data(outputs)

    assert data.models == ("Logistic Regression", "TextCNN")
    assert data.experiment_count == 4
    assert data.test_results_verified is True
    assert data.test_sample_count == 4
    assert data.best_test_accuracy == 0.75
    assert data.test_metrics["logistic_regression"]["accuracy"] == 0.75
    assert data.confusion_matrices["textcnn"] == [[1, 1], [1, 1]]
    assert len(data.experiments) == 6


def test_dashboard_does_not_show_test_metrics_when_integrity_check_fails(
    tmp_path,
) -> None:
    outputs = tmp_path / "outputs"
    _make_outputs(outputs, valid_test_metadata=False)

    data = load_dashboard_data(outputs)

    assert data.test_results_verified is False
    assert data.test_sample_count is None
    assert data.best_test_accuracy is None
    assert data.test_metrics == {}
    assert data.validation_metrics["logistic_regression"]["accuracy"] == 0.8
    assert any("integrity metadata" in issue for issue in data.issues)


def test_dashboard_handles_missing_outputs_without_claiming_test_scores(
    tmp_path,
) -> None:
    data = load_dashboard_data(tmp_path / "missing")

    assert data.models == ()
    assert data.experiments == ()
    assert data.experiment_count == 0
    assert data.test_results_verified is False
    assert data.best_test_accuracy is None
    assert data.issues


def test_dashboard_rejects_test_confusion_matrix_with_wrong_sample_total(
    tmp_path,
) -> None:
    outputs = tmp_path / "outputs"
    _make_outputs(outputs)
    result_path = outputs / "final_evaluation" / "run-d" / "final_results.json"
    results = json.loads(result_path.read_text(encoding="utf-8"))
    results["metrics"]["logistic_regression"]["confusion_matrix"] = [
        [1, 0],
        [0, 1],
    ]
    _write_json(result_path, results)

    data = load_dashboard_data(outputs)

    assert data.test_results_verified is False
    assert data.test_metrics == {}
    assert any("does not match the test row count" in issue for issue in data.issues)
