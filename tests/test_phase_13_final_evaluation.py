"""Tests for the frozen five-model official test evaluation."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import torch

from deeptext.evaluation.comparison import evaluate_comparison_model
from deeptext.experiments import phase_13_final_evaluation as phase13

LABELS = np.array([0, 0, 1, 1], dtype=np.int64)
IDS = [
    "test/neg/0.txt",
    "test/neg/1.txt",
    "test/pos/0.txt",
    "test/pos/1.txt",
]
TEXTS = [
    "negative review zero",
    "negative review one",
    "positive review zero",
    "positive review one",
]
MODEL_OUTPUTS = {
    "logistic_regression": ([0, 1, 1, 0], [0.1, 0.7, 0.8, 0.4]),
    "textcnn": ([0, 0, 1, 0], [0.1, 0.2, 0.8, 0.6]),
    "rnn": ([1, 1, 0, 0], [0.8, 0.7, 0.3, 0.2]),
    "lstm": ([0, 1, 1, 1], [0.2, 0.6, 0.8, 0.9]),
    "gru": ([0, 0, 0, 1], [0.3, 0.1, 0.4, 0.9]),
}


def _records() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "review_id": IDS,
            "text": TEXTS,
            "label": LABELS,
            "split": ["test"] * 4,
        }
    )


def _prediction_frames() -> dict[str, pd.DataFrame]:
    records = _records()
    return {
        key: phase13._prediction_frame(
            records,
            np.asarray(predicted, dtype=np.int64),
            np.asarray(probability, dtype=np.float64),
        )
        for key, (predicted, probability) in MODEL_OUTPUTS.items()
    }


def test_prediction_rows_require_same_ids_order_labels_and_binary_outputs():
    records = _records()
    frames = _prediction_frames()
    phase13._validate_prediction_rows(records, frames)

    reordered = {**frames, "gru": frames["gru"].iloc[::-1].reset_index(drop=True)}
    with pytest.raises(ValueError, match="row order"):
        phase13._validate_prediction_rows(records, reordered)

    wrong_labels = {**frames, "lstm": frames["lstm"].copy()}
    wrong_labels["lstm"].loc[0, "true_label"] = 1
    with pytest.raises(ValueError, match="not aligned"):
        phase13._validate_prediction_rows(records, wrong_labels)


def test_metrics_include_confusion_counts_class_scores_and_totals():
    metrics, reports = phase13._evaluate_metrics(
        LABELS, {"logistic_regression": _prediction_frames()["logistic_regression"]}
    )
    result = metrics["logistic_regression"]
    assert result["accuracy"] == 0.5
    assert result["precision"] == 0.5
    assert result["recall"] == 0.5
    assert result["f1"] == 0.5
    assert result["confusion_matrix"] == [[1, 1], [1, 1]]
    assert result["true_positive"] == result["true_negative"] == 1
    assert result["false_positive"] == result["false_negative"] == 1
    assert result["total_correct"] == result["total_incorrect"] == 2
    assert result["per_class"]["negative"]["support"] == 2
    assert reports["logistic_regression"]["positive"]["support"] == 2


def test_pairwise_agreement_and_error_examples_cover_all_categories():
    frames = _prediction_frames()
    agreement = phase13._pairwise_agreement(frames)
    assert (
        agreement["logistic_regression_vs_logistic_regression"]["agreement_rate"] == 1
    )
    assert agreement["logistic_regression_vs_textcnn"]["agreement_count"] == 3

    examples, summary = phase13._error_analysis(_records(), frames)
    assert summary["counts"]["all_five_correct"] == 0
    assert summary["counts"]["all_five_misclassified"] == 0
    assert summary["counts"]["models_disagree"] == 4
    assert "logistic_regression_false_positive" in set(
        examples["analysis_tags"].str.cat(sep=";").split(";")
    )
    assert "review_text" in examples
    assert all(f"{key}_confidence" in examples for key in phase13.MODEL_KEYS)


def test_latency_sample_is_class_balanced_and_ordered():
    labels = np.array([0] * 8 + [1] * 8)
    indices = phase13._latency_sample_indices(labels, 6)
    assert len(indices) == 6
    assert np.count_nonzero(labels[indices] == 0) == 3
    assert np.count_nonzero(labels[indices] == 1) == 3
    assert indices.tolist() == sorted(indices.tolist())


def test_baseline_batch_inference_returns_labels_and_probabilities_once_per_batch(
    monkeypatch,
):
    calls = []
    scores = dict(zip(TEXTS, MODEL_OUTPUTS["logistic_regression"][1], strict=True))

    def probabilities(_model, texts):
        calls.append(list(texts))
        positive = np.asarray([scores[text] for text in texts])
        return np.column_stack((1 - positive, positive))

    monkeypatch.setattr(phase13, "predict_baseline_probabilities", probabilities)
    predicted, positive, elapsed = phase13._infer_baseline(
        object(), TEXTS, batch_size=2
    )

    np.testing.assert_array_equal(predicted, MODEL_OUTPUTS["logistic_regression"][0])
    np.testing.assert_allclose(positive, MODEL_OUTPUTS["logistic_regression"][1])
    assert len(calls) == 3  # one warmup batch and two timed batches
    assert elapsed >= 0


def test_frozen_artifact_hashes_detect_changes(tmp_path):
    run = tmp_path / "rnn" / "run"
    run.mkdir(parents=True)
    checkpoint = run / "best_model.pt"
    checkpoint.write_bytes(b"frozen checkpoint")
    paths = {"rnn": run}
    before = phase13._artifact_hashes(paths)
    assert phase13._artifact_hashes(paths) == before
    checkpoint.write_bytes(b"changed checkpoint")
    assert phase13._artifact_hashes(paths) != before


def test_phase12_provenance_requires_validation_only_matching_model_runs(
    tmp_path, monkeypatch
):
    phase12_directory = tmp_path / "phase12"
    phase12_directory.mkdir()
    source_paths = {key: tmp_path / key for key in phase13.MODEL_KEYS}
    comparison = {
        "official_test_evaluated": False,
        "validation_only": True,
        "models": list(phase13.MODEL_KEYS),
        "source_artifacts": {
            "baseline": str(source_paths["logistic_regression"]),
            **{
                key: str(value)
                for key, value in source_paths.items()
                if key != "logistic_regression"
            },
        },
    }
    comparison_path = phase12_directory / "comparison.json"
    comparison_path.write_text(json.dumps(comparison), encoding="utf-8")
    monkeypatch.setattr(phase13, "PHASE12_RUN", Path("phase12"))

    assert phase13._phase12_validation(tmp_path, source_paths) == comparison_path

    comparison["official_test_evaluated"] = True
    comparison_path.write_text(json.dumps(comparison), encoding="utf-8")
    with pytest.raises(ValueError, match="validation-only"):
        phase13._phase12_validation(tmp_path, source_paths)


class _FakeBaseline:
    def fit(self, *args, **kwargs):
        raise AssertionError("The evaluation must not fit a model.")


class _FakeNeural:
    def __init__(self, key):
        self.key = key
        model_name = (
            "TextCNN" if key == "textcnn" else phase13.RECURRENT_MODEL_NAMES[key]
        )
        self.metadata = {
            "model_name": model_name,
            "official_test_evaluated": False,
            "unlabeled_reviews_used": False,
            "vocabulary_fit_partition": "training",
            "checkpoint_reload_verified": True,
            "best_epoch": 2,
            "model_config": {},
        }
        self.model = torch.nn.Linear(1, 2)

    def predict_batch(self, reviews, batch_size=128):
        predicted, probability = MODEL_OUTPUTS[self.key]
        index_by_text = {text: index for index, text in enumerate(TEXTS)}
        rows = []
        for text in reviews:
            index = index_by_text[text]
            rows.append(
                {
                    "predicted_class": predicted[index],
                    "positive_probability": probability[index],
                }
            )
        return rows

    def predict(self, review):
        return self.predict_batch([review])[0]


def test_runner_preserves_artifacts_and_does_not_train_or_overwrite_phase7(
    tmp_path, monkeypatch
):
    run_paths = {}
    for key in phase13.MODEL_KEYS:
        run = tmp_path / "source" / key / f"{key}-run"
        run.mkdir(parents=True)
        filenames = (
            ("baseline.joblib", "metadata.json")
            if key == "logistic_regression"
            else ("best_model.pt", "metadata.json", "vocabulary.json")
        )
        for filename in filenames:
            (run / filename).write_text(f"{key}:{filename}", encoding="utf-8")
        run_paths[key] = run

    records = _records()
    phase7_directory = tmp_path / "phase7"
    phase7_directory.mkdir()
    phase7_predictions = pd.DataFrame(
        {
            "review_id": IDS,
            "true_label": LABELS,
            "logistic_regression_prediction": MODEL_OUTPUTS["logistic_regression"][0],
            "textcnn_prediction": MODEL_OUTPUTS["textcnn"][0],
        }
    )
    phase7_predictions.to_csv(phase7_directory / "test_predictions.csv", index=False)
    phase7_metrics = {
        key: evaluate_comparison_model(
            LABELS,
            np.asarray(MODEL_OUTPUTS[key][0]),
            np.asarray(MODEL_OUTPUTS[key][1]),
        )
        for key in ("logistic_regression", "textcnn")
    }
    (phase7_directory / "final_results.json").write_text(
        json.dumps(
            {
                "evaluation_id": "phase7-fixture",
                "official_test_evaluated": True,
                "sample_count": len(records),
                "metrics": phase7_metrics,
                "artifacts": {"test_predictions": "test_predictions.csv"},
            }
        ),
        encoding="utf-8",
    )
    source_hashes = phase13._artifact_hashes(run_paths)
    phase7_hashes = {}
    for key, digest in source_hashes.items():
        model_key, remainder = key.split("/", 1)
        phase7_model_key = (
            "baseline" if model_key == "logistic_regression" else model_key
        )
        if model_key in {"logistic_regression", "textcnn"}:
            phase7_hashes[f"{phase7_model_key}/{remainder}"] = digest
    (phase7_directory / "metadata.json").write_text(
        json.dumps(
            {"artifacts_unchanged": True, "artifact_sha256_after": phase7_hashes}
        ),
        encoding="utf-8",
    )

    baseline_metadata = {
        "model_name": "TF-IDF + Logistic Regression",
        "official_test_evaluated": False,
        "unlabeled_reviews_used": False,
        "selected_settings": {
            "c": 1.0,
            "max_iter": 1000,
            "ngram_range": [1, 2],
            "seed": 42,
        },
    }
    predictors = {
        key: _FakeNeural(key) for key in MODEL_OUTPUTS if key != "logistic_regression"
    }
    monkeypatch.setattr(phase13, "_run_paths", lambda project_root: run_paths)
    monkeypatch.setattr(
        phase13,
        "_phase12_validation",
        lambda project_root, runs: tmp_path / "phase12.json",
    )
    monkeypatch.setattr(phase13, "PHASE7_RUN", phase7_directory)
    monkeypatch.setattr(
        phase13, "load_imdb_split", lambda *_args, **_kwargs: records.copy()
    )
    monkeypatch.setattr(
        phase13,
        "_validate_test_records",
        lambda *_args, **_kwargs: {
            "sample_count": 4,
            "class_distribution": {"negative": 2, "positive": 2},
            "review_id_order_sha256": "fixture",
        },
    )
    monkeypatch.setattr(
        phase13,
        "load_baseline_artifacts",
        lambda _path: (_FakeBaseline(), baseline_metadata),
    )
    monkeypatch.setattr(
        phase13,
        "predict_baseline_probabilities",
        lambda _model, texts: np.column_stack(
            (
                1 - np.asarray(MODEL_OUTPUTS["logistic_regression"][1][: len(texts)]),
                MODEL_OUTPUTS["logistic_regression"][1][: len(texts)],
            )
        ),
    )
    monkeypatch.setattr(
        phase13,
        "load_textcnn_predictor",
        lambda *_args, **_kwargs: predictors["textcnn"],
    )
    monkeypatch.setattr(
        phase13,
        "load_recurrent_predictor",
        lambda path, *_args, **_kwargs: predictors[Path(path).parent.name],
    )
    monkeypatch.setattr(phase13.torch.cuda, "is_available", lambda: False)

    result = phase13.run_phase13_evaluation(
        tmp_path / "dataset", tmp_path / "new_output"
    )

    output = Path(result["output_directory"])
    assert (output / "metrics.json").is_file()
    assert (output / "comparison.json").is_file()
    assert (output / "benchmark.json").is_file()
    assert (output / "metadata.json").is_file()
    assert len(list((output / "predictions").glob("*.csv"))) == 5
    assert result["metadata"]["artifacts_unchanged"] is True
    assert all(result["metadata"]["phase7_artifact_hashes_match"].values())
    assert result["comparison"]["phase7_consistency"]["textcnn"][
        "metrics_match_within_1e-12"
    ]
    assert result["metadata"]["model_training_started"] is False
    assert result["metadata"]["preprocessing_refitted"] is False
    assert result["metadata"]["official_test_evaluated"] is True
