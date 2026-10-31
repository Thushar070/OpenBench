import numpy as np
import pytest
import torch
from torch import nn

from deeptext.data.torch_dataset import TextReviewDataset, create_data_loaders
from deeptext.experiments.recurrent import run_recurrent_experiment
from deeptext.inference.predictor import load_recurrent_predictor
from deeptext.models.recurrent import (
    GRU,
    LSTM,
    RECURRENT_MODELS,
    RecurrentConfig,
    VanillaRNN,
)
from deeptext.preprocessing.vocabulary import Vocabulary
from deeptext.training.checkpoint import CheckpointManager, load_recurrent_checkpoint
from deeptext.training.trainer import TrainingConfig, train_neural_model


def _config() -> RecurrentConfig:
    return RecurrentConfig(
        vocab_size=16,
        embedding_dim=8,
        hidden_dim=6,
        num_layers=1,
        dropout=0.0,
        max_sequence_length=8,
    )


@pytest.mark.parametrize("model_type", [VanillaRNN, LSTM, GRU])
@pytest.mark.parametrize("batch_size", [1, 3])
def test_recurrent_models_return_finite_two_class_logits(
    model_type, batch_size
) -> None:
    model = model_type(_config())
    input_ids = torch.randint(1, 16, (batch_size, 8), dtype=torch.long)
    lengths = torch.full((batch_size,), 8, dtype=torch.long)

    logits = model(input_ids, lengths)

    assert logits.shape == (batch_size, 2)
    assert torch.isfinite(logits).all()


@pytest.mark.parametrize("model_type", [VanillaRNN, LSTM, GRU])
def test_packed_sequences_ignore_padding_and_support_empty_reviews(model_type) -> None:
    model = model_type(_config()).eval()
    prefix = torch.tensor([[2, 3, 4]], dtype=torch.long)
    first = torch.cat([prefix, torch.zeros((1, 5), dtype=torch.long)], dim=1)
    second = torch.cat([prefix, torch.full((1, 5), 5, dtype=torch.long)], dim=1)
    lengths = torch.tensor([3], dtype=torch.long)

    with torch.inference_mode():
        first_logits = model(first, lengths)
        second_logits = model(second, lengths)
        empty_logits = model(
            torch.zeros((1, 8), dtype=torch.long), torch.tensor([0], dtype=torch.long)
        )
        mixed_logits = model(
            torch.cat([first, torch.zeros((1, 8), dtype=torch.long)]),
            torch.tensor([3, 0], dtype=torch.long),
        )

    assert torch.allclose(first_logits, second_logits, atol=1e-6)
    assert torch.isfinite(empty_logits).all()
    assert mixed_logits.shape == (2, 2)
    assert torch.isfinite(mixed_logits).all()
    assert torch.count_nonzero(model.embedding.weight[0]) == 0


@pytest.mark.parametrize("model_type", [VanillaRNN, LSTM, GRU])
def test_recurrent_model_backward_checkpoint_and_raw_inference(
    model_type, tmp_path
) -> None:
    vocabulary = Vocabulary.build(
        ["excellent kind story", "awful dull movie", "warm excellent film"]
    )
    reviews = ["excellent kind", "awful dull", "warm film", ""]
    labels = [1, 0, 1, 0]
    training_dataset = TextReviewDataset(
        reviews, labels, vocabulary, max_length=_config().max_sequence_length
    )
    validation_dataset = TextReviewDataset(
        reviews, labels, vocabulary, max_length=_config().max_sequence_length
    )
    training_loader, validation_loader = create_data_loaders(
        training_dataset, validation_dataset, batch_size=2, seed=42
    )
    model = model_type(
        RecurrentConfig(
            vocab_size=vocabulary.size,
            embedding_dim=8,
            hidden_dim=6,
            dropout=0.0,
            padding_idx=vocabulary.pad_id,
            max_sequence_length=8,
        )
    )
    initial = [parameter.detach().clone() for parameter in model.parameters()]
    run_directory = tmp_path / model_type.__name__
    checkpoint_manager = CheckpointManager(run_directory, vocabulary)

    result = train_neural_model(
        model,
        training_loader,
        validation_loader,
        TrainingConfig(max_epochs=2, patience=2, device="cpu"),
        checkpoint_manager=checkpoint_manager,
    )
    checkpoint_manager.finalize(result.history, {"model_name": model_type.__name__})
    loaded, loaded_vocabulary, metadata = load_recurrent_checkpoint(run_directory)
    predictor = load_recurrent_predictor(run_directory)
    predictions = predictor.predict_batch([*reviews, "unseen tokens"], batch_size=2)

    assert result.device == "cpu"
    assert len(result.history) == 2
    assert all(
        not torch.equal(before, after)
        for before, after in zip(initial, model.parameters(), strict=True)
    )
    assert type(loaded) is model_type
    assert loaded_vocabulary.tokens == vocabulary.tokens
    assert metadata["best_epoch"] == result.best_epoch
    assert (run_directory / "best_model.pt").is_file()
    assert (run_directory / "history.json").is_file()
    assert len(predictions) == 5
    assert all(item["predicted_class"] in (0, 1) for item in predictions)
    assert all(
        np.isclose(item["negative_probability"] + item["positive_probability"], 1)
        for item in predictions
    )


@pytest.mark.skipif(not torch.cuda.is_available(), reason="CUDA is not available")
@pytest.mark.parametrize("model_type", [VanillaRNN, LSTM, GRU])
def test_recurrent_models_run_forward_backward_on_cuda(model_type) -> None:
    model = model_type(_config()).to("cuda")
    inputs = torch.tensor([[2, 3, 0, 0, 0, 0, 0, 0]], device="cuda")
    lengths = torch.tensor([2], dtype=torch.long, device="cuda")
    labels = torch.tensor([1], dtype=torch.long, device="cuda")
    optimizer = torch.optim.Adam(model.parameters(), lr=0.001)
    before = model.classifier.weight.detach().clone()

    logits = model(inputs, lengths)
    loss = nn.CrossEntropyLoss()(logits, labels)
    loss.backward()
    optimizer.step()

    assert logits.device.type == "cuda"
    assert torch.isfinite(loss)
    assert not torch.equal(before, model.classifier.weight)


@pytest.mark.parametrize(
    "model_type",
    [VanillaRNN, LSTM, GRU],
)
def test_supported_model_registry_matches_cell_types(model_type) -> None:
    assert RECURRENT_MODELS[model_type.__name__] is model_type


@pytest.mark.parametrize(
    "values",
    [
        {"vocab_size": 1},
        {"vocab_size": 10, "hidden_dim": 0},
        {"vocab_size": 10, "num_layers": 0},
        {"vocab_size": 10, "padding_idx": 10},
        {"vocab_size": 10, "dropout": 1.0},
    ],
)
def test_recurrent_configuration_rejects_invalid_values(values) -> None:
    with pytest.raises(ValueError):
        RecurrentConfig(**values)


def test_recurrent_model_rejects_invalid_batch_dimensions() -> None:
    model = VanillaRNN(_config())
    with pytest.raises(ValueError, match="sequence length must match"):
        model(torch.ones((2, 7), dtype=torch.long), torch.tensor([7, 7]))
    with pytest.raises(ValueError, match="lengths must have shape"):
        model(torch.ones((2, 8), dtype=torch.long), torch.tensor([8]))
    with pytest.raises(ValueError, match="between 0 and sequence_length"):
        model(torch.ones((1, 8), dtype=torch.long), torch.tensor([9]))


def test_recurrent_experiment_saves_training_only_validation_artifacts(
    tmp_path,
) -> None:
    dataset = tmp_path / "aclImdb"
    for sentiment, review in (
        ("neg", "awful dull boring film"),
        ("pos", "excellent warm exciting film"),
    ):
        class_directory = dataset / "train" / sentiment
        class_directory.mkdir(parents=True)
        for index in range(12):
            (class_directory / f"{index}.txt").write_text(
                f"{review} number {index}", encoding="utf-8"
            )

    metadata = run_recurrent_experiment(
        "gru",
        dataset_dir=dataset,
        output_root=tmp_path / "runs",
        training_config=TrainingConfig(max_epochs=1, patience=1, device="cpu"),
    )
    run_directory = tmp_path / "runs" / metadata["experiment_id"]
    saved_predictions = torch.tensor(
        np.loadtxt(
            run_directory / "validation_predictions.csv",
            delimiter=",",
            skiprows=1,
            usecols=(1, 2),
            dtype=np.int64,
        )
    )

    assert metadata["model_name"] == "GRU"
    assert metadata["official_test_evaluated"] is False
    assert metadata["split"]["training_rows"] == 19
    assert metadata["split"]["validation_rows"] == 5
    assert metadata["checkpoint_reload_verified"] is True
    assert metadata["validation_metrics"]["confusion_matrix"]
    assert saved_predictions.shape == (5, 2)
    assert not any(path.name == "test" for path in dataset.iterdir())
