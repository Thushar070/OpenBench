"""Kim-style TextCNN for binary sentiment classification."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import torch
from torch import Tensor, nn
from torch.nn import functional as F


@dataclass(frozen=True)
class TextCNNConfig:
    """Configuration needed to build a TextCNN with the Phase 4 encoding."""

    vocab_size: int
    embedding_dim: int = 100
    filter_sizes: tuple[int, ...] = (3, 4, 5)
    filters_per_size: int = 100
    dropout: float = 0.5
    padding_idx: int = 0
    max_sequence_length: int = 200

    def __post_init__(self) -> None:
        if (
            isinstance(self.vocab_size, bool)
            or not isinstance(self.vocab_size, int)
            or self.vocab_size < 2
        ):
            raise ValueError("vocab_size must be at least 2.")
        if (
            isinstance(self.embedding_dim, bool)
            or not isinstance(self.embedding_dim, int)
            or self.embedding_dim < 1
        ):
            raise ValueError("embedding_dim must be a positive integer.")
        if not isinstance(self.filter_sizes, tuple):
            raise ValueError("filter_sizes must be a tuple of positive integers.")
        if not self.filter_sizes or any(
            isinstance(size, bool) or not isinstance(size, int) or size < 1
            for size in self.filter_sizes
        ):
            raise ValueError("filter_sizes must contain positive integers.")
        if len(set(self.filter_sizes)) != len(self.filter_sizes):
            raise ValueError("filter_sizes must not contain duplicates.")
        if (
            isinstance(self.filters_per_size, bool)
            or not isinstance(self.filters_per_size, int)
            or self.filters_per_size < 1
        ):
            raise ValueError("filters_per_size must be a positive integer.")
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
        if (
            isinstance(self.max_sequence_length, bool)
            or not isinstance(self.max_sequence_length, int)
            or self.max_sequence_length < max(self.filter_sizes)
        ):
            raise ValueError(
                "max_sequence_length must be at least the largest filter size."
            )

    def to_dict(self) -> dict[str, int | float | list[int]]:
        """Return configuration values in a JSON-friendly form."""
        values = asdict(self)
        values["filter_sizes"] = list(self.filter_sizes)
        return values

    @classmethod
    def from_dict(cls, values: dict[str, object]) -> TextCNNConfig:
        """Reconstruct a model configuration from checkpoint metadata."""
        try:
            return cls(
                vocab_size=values["vocab_size"],  # type: ignore[arg-type]
                embedding_dim=values["embedding_dim"],  # type: ignore[arg-type]
                filter_sizes=tuple(values["filter_sizes"]),  # type: ignore[arg-type]
                filters_per_size=values["filters_per_size"],  # type: ignore[arg-type]
                dropout=values["dropout"],  # type: ignore[arg-type]
                padding_idx=values["padding_idx"],  # type: ignore[arg-type]
                max_sequence_length=values["max_sequence_length"],  # type: ignore[arg-type]
            )
        except (KeyError, TypeError) as error:
            raise ValueError(
                "Checkpoint contains an invalid TextCNN configuration."
            ) from error


class TextCNN(nn.Module):
    """Parallel convolution filters with length-aware temporal max pooling."""

    def __init__(self, config: TextCNNConfig) -> None:
        super().__init__()
        if not isinstance(config, TextCNNConfig):
            raise TypeError("config must be a TextCNNConfig.")
        self.config = config
        self.embedding = nn.Embedding(
            num_embeddings=config.vocab_size,
            embedding_dim=config.embedding_dim,
            padding_idx=config.padding_idx,
        )
        nn.init.xavier_uniform_(self.embedding.weight)
        with torch.no_grad():
            self.embedding.weight[config.padding_idx].zero_()

        self.convolutions = nn.ModuleList(
            nn.Conv1d(
                in_channels=config.embedding_dim,
                out_channels=config.filters_per_size,
                kernel_size=filter_size,
            )
            for filter_size in config.filter_sizes
        )
        self.dropout = nn.Dropout(config.dropout)
        self.classifier = nn.Linear(
            config.filters_per_size * len(config.filter_sizes), 2
        )

    def forward(self, input_ids: Tensor, lengths: Tensor) -> Tensor:
        """Return two logits per review, masking windows that include padding.

        Reviews shorter than a convolution filter receive a zero pooled vector
        for that branch. An empty review therefore produces finite logits from
        the classifier bias without treating padding as text.
        """
        if input_ids.ndim != 2:
            raise ValueError("input_ids must have shape (batch_size, sequence_length).")
        if input_ids.dtype != torch.long:
            raise TypeError("input_ids must use torch.long token IDs.")
        if input_ids.shape[0] == 0:
            raise ValueError("TextCNN does not support an empty batch.")
        if input_ids.shape[1] != self.config.max_sequence_length:
            raise ValueError(
                "input_ids sequence length must match max_sequence_length="
                f"{self.config.max_sequence_length}."
            )
        if lengths.ndim != 1 or lengths.shape[0] != input_ids.shape[0]:
            raise ValueError("lengths must have shape (batch_size,).")
        if lengths.dtype != torch.long:
            raise TypeError("lengths must use torch.long values.")
        if lengths.device != input_ids.device:
            raise ValueError("input_ids and lengths must be on the same device.")
        if (lengths < 0).any().item() or (lengths > input_ids.shape[1]).any().item():
            raise ValueError("lengths must be between 0 and sequence_length.")

        embedded = self.embedding(input_ids).transpose(1, 2)
        pooled_branches = []
        for filter_size, convolution in zip(
            self.config.filter_sizes, self.convolutions, strict=True
        ):
            activations = F.relu(convolution(embedded))
            window_count = activations.shape[2]
            window_starts = torch.arange(window_count, device=input_ids.device)
            valid_windows = window_starts.unsqueeze(
                0
            ) + filter_size <= lengths.unsqueeze(1)
            activations = activations.masked_fill(
                ~valid_windows.unsqueeze(1), torch.finfo(activations.dtype).min
            )
            pooled = activations.max(dim=2).values
            has_valid_window = (lengths >= filter_size).unsqueeze(1)
            pooled = torch.where(has_valid_window, pooled, torch.zeros_like(pooled))
            pooled_branches.append(pooled)

        features = torch.cat(pooled_branches, dim=1)
        return self.classifier(self.dropout(features))
