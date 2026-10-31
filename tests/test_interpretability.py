"""Small integrity tests for validation-only explainability helpers."""

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import torch

from deeptext.experiments.interpretability import (
    _cnn_activations,
    _lr_explanations,
    _occlusion,
    _read_phase6_predictions,
    _recurrent_explanations,
    _recurrent_trace,
    _select_examples,
    _write_pairwise_explanations,
)
from deeptext.models.baseline import train_baseline
from deeptext.models.cnn import TextCNN, TextCNNConfig
from deeptext.models.recurrent import GRU, LSTM, RecurrentConfig, VanillaRNN
from deeptext.preprocessing.vocabulary import Vocabulary


@pytest.mark.parametrize(
    ("kind", "model_type"),
    [("rnn", VanillaRNN), ("lstm", LSTM), ("gru", GRU)],
)
def test_recurrent_analysis_equations_reproduce_native_model_logits(kind, model_type):
    torch.manual_seed(19)
    model = model_type(
        RecurrentConfig(
            vocab_size=12,
            embedding_dim=5,
            hidden_dim=4,
            num_layers=1,
            dropout=0.4,
            max_sequence_length=7,
        )
    ).eval()
    inputs = torch.tensor([[2, 3, 4, 0, 0, 0, 0]], dtype=torch.long)
    lengths = torch.tensor([3], dtype=torch.long)

    trace = _recurrent_trace(model, inputs, 3, kind)
    reproduced = model.classifier(trace["hidden"][-1])
    actual = model(inputs, lengths)[0]

    torch.testing.assert_close(reproduced, actual, atol=3e-5, rtol=2e-5)
    assert trace["hidden"].shape == (3, 4)
    if kind == "lstm":
        assert trace["gates"].shape == (3, 4)
    elif kind == "gru":
        assert trace["gates"].shape == (3, 3)


@pytest.mark.parametrize(
    ("kind", "model_type"),
    [("rnn", VanillaRNN), ("lstm", LSTM), ("gru", GRU)],
)
def test_recurrent_state_artifacts_and_weights_are_preserved(
    kind, model_type, tmp_path
):
    vocabulary = Vocabulary.build(["good movie story but boring"])
    model = model_type(
        RecurrentConfig(
            vocab_size=vocabulary.size,
            embedding_dim=5,
            hidden_dim=4,
            num_layers=1,
            dropout=0.2,
            max_sequence_length=8,
        )
    ).eval()
    predictor = SimpleNamespace(
        model=model, vocabulary=vocabulary, device=torch.device("cpu")
    )
    records = pd.DataFrame(
        [{"review_id": "train/pos/1.txt", "text": "good movie story but boring"}]
    )
    examples = [{"row_index": 0, "review_id": "train/pos/1.txt"}]
    before = {key: value.clone() for key, value in model.state_dict().items()}

    _recurrent_explanations(kind, predictor, examples, records, tmp_path)

    state = pd.read_csv(tmp_path / "hidden_state_evolution.csv")
    dimensions = pd.read_csv(tmp_path / "hidden_dimension_activations.csv")
    assert len(state) == 5
    assert dimensions.hidden_dimension.nunique() == 4
    if kind in {"lstm", "gru"}:
        gates = pd.read_csv(tmp_path / "gate_activations.csv")
        assert gates.validated_against_native_model.all()
    if kind == "lstm":
        assert "cell_state_l2_norm" in state
    for key, value in model.state_dict().items():
        torch.testing.assert_close(value, before[key])


def test_textcnn_activation_windows_and_occlusion_preserve_weights(tmp_path):
    vocabulary = Vocabulary.build(["good movie story but boring"])
    model = TextCNN(
        TextCNNConfig(
            vocab_size=vocabulary.size,
            embedding_dim=4,
            filter_sizes=(2, 3),
            filters_per_size=4,
            dropout=0.2,
            max_sequence_length=8,
        )
    ).eval()
    predictor = SimpleNamespace(
        model=model, vocabulary=vocabulary, device=torch.device("cpu")
    )
    records = pd.DataFrame(
        [{"review_id": "train/pos/1.txt", "label": 1, "text": "good movie story"}]
    )
    examples = [{"row_index": 0, "review_id": "train/pos/1.txt", "true_label": 1}]
    before = {key: value.clone() for key, value in model.state_dict().items()}

    _cnn_activations(predictor, examples, records, tmp_path)
    occlusion = _occlusion(
        predictor, examples, records, tmp_path, max_windows=10, batch_size=4
    )

    activations = pd.read_csv(tmp_path / "max_filter_activations.csv")
    assert len(activations) == 8
    assert activations.window_tokens.str.len().gt(0).all()
    assert len(occlusion) == 6
    assert all(row["sequence_length_preserved"] for row in occlusion)
    first = occlusion[0]
    assert first["positive_probability_drop"] == pytest.approx(
        first["original_positive_probability"] - first["occluded_positive_probability"]
    )
    for key, value in model.state_dict().items():
        torch.testing.assert_close(value, before[key])


def test_lr_feature_contributions_reconstruct_saved_pipeline_score(tmp_path):
    model = train_baseline(
        ["excellent film and great acting", "terrible film and bad acting"],
        np.array([1, 0]),
    )
    validation = pd.DataFrame(
        [{"review_id": "train/pos/1.txt", "label": 1, "text": "excellent film"}]
    )
    examples = [{"row_index": 0, "review_id": "train/pos/1.txt", "true_label": 1}]

    info = _lr_explanations(model, validation, examples, tmp_path)

    assert info["all_reconstructions_validated"] is True
    assert 0 < len(pd.read_csv(tmp_path / "global_top_positive.csv")) <= 20
    details = pd.read_csv(tmp_path / "review_feature_contributions.csv")
    assert len(details) > 0
    assert details["contribution"].notna().all()


def test_example_selection_is_deterministic_and_covers_cases():
    validation = pd.DataFrame(
        {
            "review_id": [f"train/review/{i}.txt" for i in range(6)],
            "label": [1, 0, 0, 1, 1, 0],
            "text": [
                "wonderful",
                "awful",
                "not good but watchable",
                "great",
                "mixed",
                "bad",
            ],
        }
    )
    baseline = (np.array([1, 0, 1, 0, 1, 0]), np.array([0.9, 0.1, 0.8, 0.4, 0.7, 0.2]))
    cnn = (np.array([1, 0, 0, 1, 1, 0]), np.array([0.8, 0.2, 0.4, 0.7, 0.6, 0.3]))
    predictions = {
        key: baseline for key in ("logistic_regression", "rnn", "lstm", "gru")
    }
    predictions["textcnn"] = cnn

    selected = _select_examples(validation, predictions)

    assert selected == _select_examples(validation, predictions)
    tags = {tag for example in selected for tag in example["selection_tags"]}
    assert {
        "correct_positive",
        "correct_negative",
        "false_positive",
        "false_negative",
    } <= tags
    assert "mixed_or_contrasting_phrase" in tags
    assert "model_disagreement" in tags


def test_phase6_prediction_alignment_rejects_changed_validation_ids(tmp_path):
    path = tmp_path / "validation_predictions.csv"
    pd.DataFrame(
        {
            "review_id": ["train/neg/1.txt", "train/pos/2.txt"],
            "true_label": [0, 1],
            "baseline_prediction": [0, 1],
            "baseline_positive_probability": [0.1, 0.9],
            "textcnn_prediction": [0, 1],
            "textcnn_positive_probability": [0.2, 0.8],
        }
    ).to_csv(path, index=False)
    validation = pd.DataFrame(
        {
            "review_id": ["train/neg/1.txt", "train/pos/3.txt"],
            "label": [0, 1],
        }
    )

    with pytest.raises(ValueError, match="do not match"):
        _read_phase6_predictions(path, validation)


def test_pairwise_explanations_include_all_model_pairs_and_local_evidence(tmp_path):
    for key in ("textcnn", "rnn", "lstm", "gru"):
        model_directory = tmp_path / key
        model_directory.mkdir()
        pd.DataFrame(
            [
                {
                    "review_id": "train/pos/1.txt",
                    "tokens": "excellent",
                    "positive_probability_drop": 0.2,
                },
                {
                    "review_id": "train/pos/1.txt",
                    "tokens": "boring",
                    "positive_probability_drop": -0.1,
                },
            ]
        ).to_csv(model_directory / "token_window_occlusion.csv", index=False)
    (tmp_path / "logistic_regression").mkdir()
    pd.DataFrame(
        [
            {"review_id": "train/pos/1.txt", "term": "excellent", "contribution": 0.4},
            {"review_id": "train/pos/1.txt", "term": "boring", "contribution": -0.3},
        ]
    ).to_csv(
        tmp_path / "logistic_regression" / "review_feature_contributions.csv",
        index=False,
    )
    roster = [
        {
            "review_id": "train/pos/1.txt",
            "true_label": 1,
            **{
                f"{key}_prediction": index % 2
                for index, key in enumerate(
                    ("logistic_regression", "textcnn", "rnn", "lstm", "gru")
                )
            },
            **{
                f"{key}_positive_probability": 0.4 + index / 10
                for index, key in enumerate(
                    ("logistic_regression", "textcnn", "rnn", "lstm", "gru")
                )
            },
        }
    ]
    (tmp_path / "cross_model").mkdir()

    _write_pairwise_explanations(tmp_path, roster)

    result = pd.read_csv(
        tmp_path / "cross_model" / "pairwise_explanation_comparison.csv"
    )
    assert len(result) == 10
    assert result["left_local_evidence"].str.len().gt(0).all()
    assert result["right_local_evidence"].str.len().gt(0).all()
