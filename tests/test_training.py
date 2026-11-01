import json
from pathlib import Path

import numpy as np
import pytest
import torch
from torch import nn
from torch.utils.data import DataLoader

from deeptext.data.torch_dataset import TextReviewDataset, create_data_loaders
from deeptext.experiments import recovery
from deeptext.inference.predictor import (
    TextCNNSentimentPredictor,
    load_textcnn_predictor,
)
from deeptext.models.cnn import TextCNN, TextCNNConfig
from deeptext.preprocessing.vocabulary import Vocabulary
from deeptext.training.checkpoint import (
    CheckpointManager,
    load_textcnn_checkpoint,
)
from deeptext.training.early_stopping import EarlyStopping
from deeptext.training.trainer import (
    TrainingConfig,
    run_epoch,
    train_textcnn,
)

TRAINING_TEXTS = [
    "excellent warm story",
    "wonderful acting and plot",
    "awful dull story",
    "terrible acting and plot",
    "a charming funny film",
    "a boring confusing film",
    "I loved this movie",
    "I hated this movie",
]
TRAINING_LABELS = [1, 1, 0, 0, 1, 0, 1, 0]


def _training_fixture(
    max_length: int = 8,
) -> tuple[Vocabulary, TextReviewDataset, TextReviewDataset]:
    vocabulary = Vocabulary.build(TRAINING_TEXTS)
    training_dataset = TextReviewDataset(
        TRAINING_TEXTS[:6], TRAINING_LABELS[:6], vocabulary, max_length
    )
    validation_dataset = TextReviewDataset(
        TRAINING_TEXTS[6:], TRAINING_LABELS[6:], vocabulary, max_length
    )
    return vocabulary, training_dataset, validation_dataset


def _training_model(vocabulary: Vocabulary, max_length: int = 8) -> TextCNN:
    return TextCNN(
        TextCNNConfig(
            vocab_size=vocabulary.size,
            embedding_dim=8,
            filter_sizes=(2, 3),
            filters_per_size=4,
            dropout=0.1,
            padding_idx=vocabulary.pad_id,
            max_sequence_length=max_length,
        )
    )


def test_early_stopping_tracks_best_score_and_minimum_improvement() -> None:
    stopping = EarlyStopping(patience=2, min_delta=0.05)

    first = stopping.update(0.5, 1)
    small_gain = stopping.update(0.51, 2)
    stop = stopping.update(0.52, 3)

    assert first.is_best and first.improved_enough
    assert small_gain.is_best and not small_gain.improved_enough
    assert not stop.improved_enough and stop.should_stop
    assert stopping.best_epoch == 3
    assert stopping.best_score == pytest.approx(0.52)
    assert stopping.bad_epochs == 2
    assert stopping.stopping_reason is not None


def test_early_stopping_resets_patience_after_significant_gain() -> None:
    stopping = EarlyStopping(patience=2, min_delta=0.01)

    stopping.update(0.5, 1)
    stopping.update(0.49, 2)
    gain = stopping.update(0.52, 3)
    first_plateau_epoch = stopping.update(0.52, 4)
    second_plateau_epoch = stopping.update(0.52, 5)

    assert gain.improved_enough
    assert first_plateau_epoch.bad_epochs == 1
    assert second_plateau_epoch.should_stop
    assert stopping.best_epoch == 3


def test_early_stopping_rejects_invalid_scores() -> None:
    stopping = EarlyStopping()
    with pytest.raises(ValueError, match="between 0 and 1"):
        stopping.update(float("nan"), 1)


def test_run_epoch_validation_does_not_change_model_parameters() -> None:
    vocabulary, training_dataset, _ = _training_fixture()
    model = _training_model(vocabulary)
    _, validation_loader = create_data_loaders(
        training_dataset, training_dataset, batch_size=2
    )
    parameters_before = [parameter.detach().clone() for parameter in model.parameters()]

    metrics = run_epoch(
        model,
        validation_loader,
        nn.CrossEntropyLoss(),
        torch.device("cpu"),
    )

    assert all(
        torch.equal(before, after)
        for before, after in zip(parameters_before, model.parameters(), strict=True)
    )
    assert all(np.isfinite(value) for value in metrics.values())
    assert not model.training


def test_training_updates_parameters_and_records_expected_history() -> None:
    vocabulary, training_dataset, validation_dataset = _training_fixture()
    training_loader, validation_loader = create_data_loaders(
        training_dataset, validation_dataset, batch_size=2, seed=42
    )
    model = _training_model(vocabulary)
    initial_parameters = [
        parameter.detach().clone() for parameter in model.parameters()
    ]

    result = train_textcnn(
        model,
        training_loader,
        validation_loader,
        TrainingConfig(max_epochs=2, patience=2, device="cpu"),
    )

    assert len(result.history) == 2
    assert result.best_epoch in (1, 2)
    assert result.device == "cpu"
    assert all(
        np.isfinite(value)
        for row in result.history
        for key, value in row.items()
        if key != "epoch"
    )
    assert all(
        not torch.equal(before, after)
        for before, after in zip(initial_parameters, model.parameters(), strict=True)
    )
    assert not model.training


@pytest.mark.skipif(not torch.cuda.is_available(), reason="CUDA is not available")
def test_training_completes_one_epoch_on_cuda() -> None:
    vocabulary, training_dataset, validation_dataset = _training_fixture()
    training_loader, validation_loader = create_data_loaders(
        training_dataset, validation_dataset, batch_size=2, seed=42, device="cuda"
    )
    model = _training_model(vocabulary)

    result = train_textcnn(
        model,
        training_loader,
        validation_loader,
        TrainingConfig(max_epochs=1, device="cuda"),
    )

    assert result.device == "cuda"
    assert len(result.history) == 1
    assert all(np.isfinite(value) for value in result.history[0].values())


def test_training_rejects_empty_loaders() -> None:
    vocabulary, _, _ = _training_fixture()
    model = _training_model(vocabulary)
    empty_loader = DataLoader([], batch_size=2)
    with pytest.raises(ValueError, match="training_loader"):
        train_textcnn(model, empty_loader, empty_loader)


def test_checkpoint_save_load_and_cpu_inference_round_trip(tmp_path: Path) -> None:
    vocabulary, training_dataset, validation_dataset = _training_fixture()
    training_loader, validation_loader = create_data_loaders(
        training_dataset, validation_dataset, batch_size=2, seed=42
    )
    model = _training_model(vocabulary)
    run_directory = tmp_path / "textcnn-run"
    checkpoint_manager = CheckpointManager(run_directory, vocabulary)

    result = train_textcnn(
        model,
        training_loader,
        validation_loader,
        TrainingConfig(max_epochs=3, patience=2, device="cpu"),
        checkpoint_manager=checkpoint_manager,
    )
    checkpoint_manager.finalize(result.history, {"experiment_id": "unit-test"})

    reloaded_model, reloaded_vocabulary, metadata = load_textcnn_checkpoint(
        run_directory, device="cpu"
    )
    predictor = load_textcnn_predictor(run_directory, device="cpu")
    sample_reviews = ["excellent wonderful film", "awful boring film", ""]
    original_predictor = load_textcnn_predictor(run_directory, device="cpu")
    original_predictions = [original_predictor.predict(text) for text in sample_reviews]
    reloaded_predictions = [predictor.predict(text) for text in sample_reviews]

    assert reloaded_model.config == model.config
    assert reloaded_vocabulary.tokens == vocabulary.tokens
    assert metadata["experiment_id"] == "unit-test"
    assert metadata["best_epoch"] == result.best_epoch
    assert (run_directory / "history.json").is_file()
    assert (run_directory / "best_model.pt").is_file()
    for original, reloaded in zip(
        original_predictions, reloaded_predictions, strict=True
    ):
        assert original["predicted_class"] == reloaded["predicted_class"]
        assert np.allclose(
            [original["negative_probability"], original["positive_probability"]],
            [reloaded["negative_probability"], reloaded["positive_probability"]],
            atol=1e-6,
        )
        assert np.isclose(
            reloaded["negative_probability"] + reloaded["positive_probability"], 1.0
        )


def test_checkpoint_manager_saves_only_strict_best_and_refuses_run_overwrite(
    tmp_path: Path,
) -> None:
    vocabulary, _, _ = _training_fixture()
    model = _training_model(vocabulary)
    run_directory = tmp_path / "existing-run"
    manager = CheckpointManager(run_directory, vocabulary)

    assert manager.save_best(model, 1, {"f1": 0.5}) is True
    assert manager.save_best(model, 2, {"f1": 0.49}) is False
    assert manager.best_epoch == 1
    with pytest.raises(FileExistsError):
        CheckpointManager(run_directory, vocabulary)


def test_checkpoint_manager_supports_an_explicit_selection_metric(
    tmp_path: Path,
) -> None:
    vocabulary, _, _ = _training_fixture()
    model = _training_model(vocabulary)
    run_directory = tmp_path / "combined-selection"
    manager = CheckpointManager(
        run_directory, vocabulary, selection_metric="selection_score"
    )

    assert manager.save_best(model, 1, {"selection_score": 0.6}) is True
    assert manager.save_best(model, 2, {"selection_score": 0.7}) is True
    manager.finalize([], {"experiment_id": "combined-selection-test"})
    metadata = json.loads((run_directory / "metadata.json").read_text(encoding="utf-8"))

    assert manager.best_epoch == 2
    assert metadata["selection_metric"] == "selection_score"
    assert metadata["best_validation_score"] == pytest.approx(0.7)
    assert "best_validation_f1" not in metadata


def test_official_test_gate_requires_every_pretest_check() -> None:
    assert recovery._pre_test_acceptance_passes({"api": True, "parity": True})
    assert not recovery._pre_test_acceptance_passes({"api": True, "polarity": False})
    assert not recovery._pre_test_acceptance_passes({})


def test_recovery_candidate_selects_checkpoint_on_full_and_short_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vocabulary, training_dataset, validation_dataset = _training_fixture()
    training_loader, validation_loader = create_data_loaders(
        training_dataset, validation_dataset, batch_size=2, seed=7
    )
    model = _training_model(vocabulary)
    manager = CheckpointManager(
        tmp_path / "recovery-candidate",
        vocabulary,
        selection_metric="selection_score",
    )
    epoch_metrics = iter(
        [
            {"loss": 0.5, "accuracy": 0.5, "precision": 0.5, "recall": 0.5, "f1": 0.5},
            {"loss": 0.4, "accuracy": 0.7, "precision": 0.7, "recall": 0.7, "f1": 0.7},
            {"loss": 0.8, "accuracy": 0.4, "precision": 0.4, "recall": 0.4, "f1": 0.4},
            {"loss": 0.7, "accuracy": 0.5, "precision": 0.5, "recall": 0.5, "f1": 0.5},
            {"loss": 0.5, "accuracy": 0.5, "precision": 0.5, "recall": 0.5, "f1": 0.5},
            {"loss": 0.4, "accuracy": 0.6, "precision": 0.6, "recall": 0.6, "f1": 0.6},
            {"loss": 0.3, "accuracy": 0.9, "precision": 0.9, "recall": 0.9, "f1": 0.9},
            {"loss": 0.2, "accuracy": 0.9, "precision": 0.9, "recall": 0.9, "f1": 0.9},
        ]
    )
    monkeypatch.setattr(
        recovery, "run_epoch", lambda *args, **kwargs: next(epoch_metrics)
    )

    best_epoch, _, history = recovery._train_candidate_with_short_validation(
        model,
        training_loader,
        validation_loader,
        validation_loader,
        validation_loader,
        TrainingConfig(max_epochs=2, patience=3, min_delta=0, device="cpu"),
        manager,
    )
    manager.finalize(history, {"experiment_id": "recovery-candidate-test"})

    assert best_epoch == 2
    assert manager.best_epoch == 2
    assert history[0]["selection_score"] == pytest.approx(0.525)
    assert history[1]["selection_score"] == pytest.approx(0.81)


def test_recovery_candidate_training_smoke(tmp_path: Path) -> None:
    vocabulary, training_dataset, validation_dataset = _training_fixture()
    training_loader, validation_loader = create_data_loaders(
        training_dataset, validation_dataset, batch_size=2, seed=7
    )
    model = _training_model(vocabulary)
    manager = CheckpointManager(
        tmp_path / "recovery-smoke",
        vocabulary,
        selection_metric="selection_score",
    )

    best_epoch, _, history = recovery._train_candidate_with_short_validation(
        model,
        training_loader,
        validation_loader,
        validation_loader,
        validation_loader,
        TrainingConfig(max_epochs=1, patience=1, device="cpu"),
        manager,
    )
    manager.finalize(history, {"experiment_id": "recovery-smoke-test"})

    assert best_epoch == 1
    assert manager.checkpoint_path.is_file()
    assert 0 <= history[0]["selection_score"] <= 1
    assert "sentence_validation_f1" in history[0]


def test_checkpoint_loader_rejects_missing_and_incompatible_files(
    tmp_path: Path,
) -> None:
    with pytest.raises(FileNotFoundError, match="not found"):
        load_textcnn_checkpoint(tmp_path / "missing")

    vocabulary, _, _ = _training_fixture()
    run_directory = tmp_path / "broken-run"
    manager = CheckpointManager(run_directory, vocabulary)
    model = _training_model(vocabulary)
    manager.save_best(model, 1, {"f1": 0.5})
    manager.finalize([], {"experiment_id": "broken-test"})
    torch.save(
        {
            "format_version": 1,
            "model_config": model.config.to_dict(),
            "state_dict": {"wrong.weight": torch.ones(1)},
        },
        run_directory / "best_model.pt",
    )

    with pytest.raises(RuntimeError, match="(Missing key|Unexpected key)"):
        load_textcnn_checkpoint(run_directory)


def test_predictor_handles_unknown_empty_text_and_preserves_weights(
    tmp_path: Path,
) -> None:
    vocabulary, _, _ = _training_fixture()
    model = _training_model(vocabulary)
    manager = CheckpointManager(tmp_path / "predictor-run", vocabulary)
    manager.save_best(model, 1, {"f1": 0.0})
    manager.finalize([], {"experiment_id": "inference-test"})
    predictor = load_textcnn_predictor(tmp_path / "predictor-run")
    original_parameters = [
        parameter.detach().clone() for parameter in predictor.model.parameters()
    ]

    prediction = predictor.predict("unseenwordxyz")
    empty_prediction = predictor.predict("")

    assert prediction["predicted_class"] in (0, 1)
    assert prediction["sentiment"] in ("negative", "positive")
    assert np.isfinite(prediction["negative_probability"])
    assert np.isfinite(prediction["positive_probability"])
    assert np.isclose(
        prediction["negative_probability"] + prediction["positive_probability"], 1.0
    )
    assert empty_prediction["predicted_class"] in (0, 1)
    assert all(
        torch.equal(before, after)
        for before, after in zip(
            original_parameters, predictor.model.parameters(), strict=True
        )
    )


def test_predictor_batch_preserves_order_and_matches_single_predictions() -> None:
    vocabulary, _, _ = _training_fixture()
    predictor = TextCNNSentimentPredictor(
        _training_model(vocabulary), vocabulary, device="cpu"
    )
    reviews = ["excellent warm story", "", "unseen token", "awful dull story"]

    batched = predictor.predict_batch(reviews, batch_size=2)
    individual = [predictor.predict(review) for review in reviews]

    assert [result["predicted_class"] for result in batched] == [
        result["predicted_class"] for result in individual
    ]
    assert np.allclose(
        [result["positive_probability"] for result in batched],
        [result["positive_probability"] for result in individual],
        atol=1e-6,
    )
    assert predictor.predict_batch([], batch_size=2) == []


def test_training_checkpoint_is_best_validation_f1_and_loads_on_cpu(
    tmp_path: Path,
) -> None:
    vocabulary, training_dataset, validation_dataset = _training_fixture()
    training_loader, validation_loader = create_data_loaders(
        training_dataset, validation_dataset, batch_size=2, seed=7
    )
    model = _training_model(vocabulary)
    checkpoint_manager = CheckpointManager(tmp_path / "best-run", vocabulary)
    result = train_textcnn(
        model,
        training_loader,
        validation_loader,
        TrainingConfig(max_epochs=4, patience=2, min_delta=0.0, device="cpu"),
        checkpoint_manager=checkpoint_manager,
    )
    checkpoint_manager.finalize(result.history, {"experiment_id": "best-test"})
    loaded_model, _, metadata = load_textcnn_checkpoint(
        tmp_path / "best-run", device="cpu"
    )

    recorded_best = max(result.history, key=lambda row: row["validation_f1"])
    assert metadata["best_epoch"] == recorded_best["epoch"]
    assert metadata["best_validation_f1"] == pytest.approx(
        recorded_best["validation_f1"]
    )
    assert next(loaded_model.parameters()).device.type == "cpu"


def test_run_epoch_applies_configured_gradient_clipping(monkeypatch) -> None:
    vocabulary, training_dataset, _ = _training_fixture()
    loader, _ = create_data_loaders(
        training_dataset,
        training_dataset,
        batch_size=2,
        seed=7,
    )
    model = _training_model(vocabulary)
    optimizer = torch.optim.SGD(model.parameters(), lr=0.01)
    calls: list[float] = []

    def record_clip(parameters, max_norm):
        calls.append(max_norm)
        return torch.tensor(0.0)

    monkeypatch.setattr(torch.nn.utils, "clip_grad_norm_", record_clip)
    metrics = run_epoch(
        model,
        loader,
        nn.CrossEntropyLoss(),
        "cpu",
        optimizer,
        max_gradient_norm=0.75,
    )

    assert calls == [0.75] * len(loader)
    assert np.isfinite(metrics["loss"])


def test_training_config_validates_gradient_clip() -> None:
    assert TrainingConfig(max_gradient_norm=1.0).to_dict()["max_gradient_norm"] == 1.0
    with pytest.raises(ValueError, match="max_gradient_norm"):
        TrainingConfig(max_gradient_norm=0)
