import pytest
import torch
from torch import nn

from deeptext.models.cnn import TextCNN, TextCNNConfig


def _small_model() -> TextCNN:
    return TextCNN(
        TextCNNConfig(
            vocab_size=20,
            embedding_dim=8,
            filter_sizes=(2, 3, 5),
            filters_per_size=4,
            dropout=0.0,
            max_sequence_length=6,
        )
    )


@pytest.mark.parametrize("batch_size", [1, 3])
def test_textcnn_returns_finite_two_class_logits(batch_size: int) -> None:
    model = _small_model()
    input_ids = torch.randint(1, 20, (batch_size, 6), dtype=torch.long)
    lengths = torch.full((batch_size,), 6, dtype=torch.long)

    logits = model(input_ids, lengths)

    assert logits.shape == (batch_size, 2)
    assert torch.isfinite(logits).all()


def test_textcnn_handles_padding_short_empty_and_unknown_token_sequences() -> None:
    model = _small_model()
    input_ids = torch.tensor(
        [
            [2, 3, 4, 0, 0, 0],
            [1, 2, 0, 0, 0, 0],
            [0, 0, 0, 0, 0, 0],
        ],
        dtype=torch.long,
    )

    logits = model(input_ids, torch.tensor([3, 2, 0], dtype=torch.long))

    assert logits.shape == (3, 2)
    assert torch.isfinite(logits).all()
    assert torch.count_nonzero(model.embedding.weight[0]) == 0


def test_padding_windows_cannot_win_temporal_max_pooling() -> None:
    model = TextCNN(
        TextCNNConfig(
            vocab_size=10,
            embedding_dim=2,
            filter_sizes=(2, 3, 5),
            filters_per_size=1,
            dropout=0.0,
            max_sequence_length=5,
        )
    )
    captured_features: list[torch.Tensor] = []

    def capture_classifier_input(
        _module: nn.Module, inputs: tuple[torch.Tensor, ...]
    ) -> None:
        captured_features.append(inputs[0].detach().clone())

    model.classifier.register_forward_pre_hook(capture_classifier_input)
    with torch.no_grad():
        for branch_index, convolution in enumerate(model.convolutions):
            convolution.weight.zero_()
            convolution.bias.fill_(4.0 + branch_index)

    inputs = torch.tensor(
        [[0, 0, 0, 0, 0], [2, 0, 0, 0, 0], [2, 3, 4, 0, 0]],
        dtype=torch.long,
    )
    model(inputs, torch.tensor([0, 1, 3], dtype=torch.long))
    pooled = captured_features[0]

    assert torch.equal(pooled[0], torch.zeros(3))
    assert torch.equal(pooled[1], torch.zeros(3))
    assert torch.equal(pooled[2], torch.tensor([4.0, 5.0, 0.0]))


def test_padding_embedding_receives_no_gradient_updates() -> None:
    model = _small_model()
    logits = model(
        torch.tensor([[2, 3, 0, 0, 0, 0]], dtype=torch.long),
        torch.tensor([2], dtype=torch.long),
    )
    nn.CrossEntropyLoss()(logits, torch.tensor([1], dtype=torch.long)).backward()

    assert torch.equal(model.embedding.weight.grad[0], torch.zeros(8))


@pytest.mark.skipif(not torch.cuda.is_available(), reason="CUDA is not available")
def test_textcnn_runs_on_cuda() -> None:
    model = _small_model().to("cuda")
    input_ids = torch.tensor([[2, 3, 0, 0, 0, 0]], device="cuda")
    lengths = torch.tensor([2], dtype=torch.long, device="cuda")

    logits = model(input_ids, lengths)

    assert logits.device.type == "cuda"
    assert logits.shape == (1, 2)
    assert torch.isfinite(logits).all()


@pytest.mark.parametrize(
    "config_values",
    [
        {"vocab_size": 1},
        {"vocab_size": 10, "padding_idx": 10},
        {"vocab_size": 10, "filter_sizes": ()},
        {"vocab_size": 10, "filter_sizes": (2, 2)},
        {"vocab_size": 10, "filter_sizes": (6,), "max_sequence_length": 5},
        {"vocab_size": 10, "dropout": 1.0},
    ],
)
def test_textcnn_config_rejects_invalid_dimensions(
    config_values: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        TextCNNConfig(**config_values)  # type: ignore[arg-type]


def test_textcnn_rejects_incompatible_sequence_and_length_shapes() -> None:
    model = _small_model()
    with pytest.raises(ValueError, match="sequence length must match"):
        model(torch.ones((2, 5), dtype=torch.long), torch.tensor([5, 5]))
    with pytest.raises(ValueError, match="lengths must have shape"):
        model(torch.ones((2, 6), dtype=torch.long), torch.tensor([5]))
    with pytest.raises(ValueError, match="between 0 and sequence_length"):
        model(torch.ones((1, 6), dtype=torch.long), torch.tensor([7]))
