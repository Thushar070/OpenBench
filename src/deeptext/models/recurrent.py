"""Length-aware vanilla RNN, LSTM, and GRU text classifiers."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Literal

import torch
from torch import Tensor, nn
from torch.nn.utils.rnn import pack_padded_sequence

RecurrentKind = Literal["rnn", "lstm", "gru"]


@dataclass(frozen=True)
class RecurrentConfig:
    """Shared, JSON-friendly configuration for the recurrent classifiers."""

    vocab_size: int
    embedding_dim: int = 100
    hidden_dim: int = 100
    num_layers: int = 1
    dropout: float = 0.5
    padding_idx: int = 0
    max_sequence_length: int = 200

    def __post_init__(self) -> None:
        for name in (
            "vocab_size",
            "embedding_dim",
            "hidden_dim",
            "num_layers",
            "max_sequence_length",
        ):
            value = getattr(self, name)
            minimum = 2 if name == "vocab_size" else 1
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an integer of at least {minimum}.")
        if (
            isinstance(self.dropout, bool)
            or not isinstance(self.dropout, (int, float))
            or not math.isfinite(self.dropout)
            or not 0 <= self.dropout < 1
        ):
            raise ValueError("dropout must be finite and in the range [0, 1).")
        if (
            isinstance(self.padding_idx, bool)
            or not isinstance(self.padding_idx, int)
            or not 0 <= self.padding_idx < self.vocab_size
        ):
            raise ValueError("padding_idx must be a valid vocabulary ID.")

    def to_dict(self) -> dict[str, int | float]:
        """Return configuration values that can be saved in JSON metadata."""
        return asdict(self)

    @classmethod
    def from_dict(cls, values: dict[str, object]) -> RecurrentConfig:
        """Reconstruct a validated configuration from checkpoint metadata."""
        try:
            return cls(
                vocab_size=values["vocab_size"],  # type: ignore[arg-type]
                embedding_dim=values["embedding_dim"],  # type: ignore[arg-type]
                hidden_dim=values["hidden_dim"],  # type: ignore[arg-type]
                num_layers=values["num_layers"],  # type: ignore[arg-type]
                dropout=values["dropout"],  # type: ignore[arg-type]
                padding_idx=values["padding_idx"],  # type: ignore[arg-type]
                max_sequence_length=values["max_sequence_length"],  # type: ignore[arg-type]
            )
        except (KeyError, TypeError) as error:
            raise ValueError(
                "Checkpoint contains an invalid recurrent configuration."
            ) from error


class _RecurrentTextClassifier(nn.Module):
    """Shared packed-sequence handling for the three unidirectional cells."""

    recurrent_kind: RecurrentKind

    def __init__(self, config: RecurrentConfig) -> None:
        super().__init__()
        if not isinstance(config, RecurrentConfig):
            raise TypeError("config must be a RecurrentConfig.")
        self.config = config
        self.embedding = nn.Embedding(
            config.vocab_size, config.embedding_dim, padding_idx=config.padding_idx
        )
        nn.init.xavier_uniform_(self.embedding.weight)
        with torch.no_grad():
            self.embedding.weight[config.padding_idx].zero_()

        cell_types: dict[RecurrentKind, type[nn.RNNBase]] = {
            "rnn": nn.RNN,
            "lstm": nn.LSTM,
            "gru": nn.GRU,
        }
        cell_type = cell_types[self.recurrent_kind]
        kwargs: dict[str, object] = {
            "input_size": config.embedding_dim,
            "hidden_size": config.hidden_dim,
            "num_layers": config.num_layers,
            "batch_first": True,
            "dropout": config.dropout if config.num_layers > 1 else 0.0,
            "bidirectional": False,
        }
        if self.recurrent_kind == "rnn":
            kwargs["nonlinearity"] = "tanh"
        self.recurrent = cell_type(**kwargs)
        self.dropout = nn.Dropout(config.dropout)
        self.classifier = nn.Linear(config.hidden_dim, 2)

    def forward(self, input_ids: Tensor, lengths: Tensor) -> Tensor:
        """Return two logits per review, excluding all padded positions.

        Empty reviews receive a zero sequence representation and are classified
        by the same head. Non-empty reviews use packed sequences, so recurrent
        state never advances through the padded tail.
        """
        if input_ids.ndim != 2:
            raise ValueError("input_ids must have shape (batch_size, sequence_length).")
        if input_ids.dtype != torch.long:
            raise TypeError("input_ids must use torch.long token IDs.")
        batch_size, sequence_length = input_ids.shape
        if batch_size == 0:
            raise ValueError("Recurrent models do not support an empty batch.")
        if sequence_length != self.config.max_sequence_length:
            raise ValueError(
                "input_ids sequence length must match max_sequence_length="
                f"{self.config.max_sequence_length}."
            )
        if lengths.ndim != 1 or lengths.shape[0] != batch_size:
            raise ValueError("lengths must have shape (batch_size,).")
        if lengths.dtype != torch.long:
            raise TypeError("lengths must use torch.long values.")
        if lengths.device != input_ids.device:
            raise ValueError("input_ids and lengths must be on the same device.")
        if (lengths < 0).any().item() or (lengths > sequence_length).any().item():
            raise ValueError("lengths must be between 0 and sequence_length.")

        non_empty = torch.nonzero(lengths > 0, as_tuple=False).flatten()
        representations = self.embedding.weight.new_zeros(
            (batch_size, self.config.hidden_dim)
        )
        if non_empty.numel() > 0:
            selected_ids = input_ids.index_select(0, non_empty)
            selected_lengths = lengths.index_select(0, non_empty)
            embedded = self.embedding(selected_ids)
            packed = pack_padded_sequence(
                embedded,
                selected_lengths.detach().cpu(),
                batch_first=True,
                enforce_sorted=False,
            )
            _, state = self.recurrent(packed)
            hidden_state = state[0] if isinstance(state, tuple) else state
            final_hidden = hidden_state[-1]
            representations = representations.index_copy(0, non_empty, final_hidden)

        return self.classifier(self.dropout(representations))


class VanillaRNN(_RecurrentTextClassifier):
    """Unidirectional tanh RNN sentiment classifier."""

    recurrent_kind: RecurrentKind = "rnn"


class LSTM(_RecurrentTextClassifier):
    """Unidirectional LSTM sentiment classifier using the final hidden state."""

    recurrent_kind: RecurrentKind = "lstm"


class GRU(_RecurrentTextClassifier):
    """Unidirectional GRU sentiment classifier."""

    recurrent_kind: RecurrentKind = "gru"


RECURRENT_MODELS: dict[str, type[_RecurrentTextClassifier]] = {
    "VanillaRNN": VanillaRNN,
    "LSTM": LSTM,
    "GRU": GRU,
}
