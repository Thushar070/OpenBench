"""PyTorch dataset and reproducible DataLoader helpers for review text."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import torch
from torch import Tensor
from torch.utils.data import DataLoader, Dataset

from deeptext.evaluation.metrics import validate_binary_labels
from deeptext.preprocessing.encoding import (
    DEFAULT_MAX_SEQUENCE_LENGTH,
    encode_text,
)
from deeptext.preprocessing.vocabulary import Vocabulary

DEFAULT_BATCH_SIZE = 32
DEFAULT_NUM_WORKERS = 0
DEFAULT_RANDOM_SEED = 42


class TextReviewDataset(Dataset[dict[str, Tensor]]):
    """Pre-encode labeled reviews and return fixed-size tensors per sample."""

    def __init__(
        self,
        reviews: Sequence[str],
        labels: Sequence[int] | np.ndarray,
        vocabulary: Vocabulary,
        max_length: int = DEFAULT_MAX_SEQUENCE_LENGTH,
    ) -> None:
        if isinstance(reviews, str):
            raise TypeError("reviews must be a sequence of review strings.")
        if (
            isinstance(max_length, bool)
            or not isinstance(max_length, int)
            or max_length < 1
        ):
            raise ValueError("max_length must be a positive integer.")
        if not isinstance(vocabulary, Vocabulary):
            raise TypeError("vocabulary must be a built Vocabulary instance.")
        _ = vocabulary.size

        review_texts = list(reviews)
        label_values = np.asarray(labels)
        if label_values.ndim != 1:
            raise ValueError("labels must be a one-dimensional sequence.")
        if len(review_texts) != len(label_values):
            raise ValueError("reviews and labels must have equal lengths.")
        if not all(isinstance(review, str) for review in review_texts):
            raise TypeError("reviews must contain only strings.")

        if label_values.size:
            valid_labels = validate_binary_labels(label_values, "labels")
        else:
            valid_labels = np.asarray([], dtype=np.int64)

        encoded_rows: list[list[int]] = []
        sequence_lengths: list[int] = []
        for review in review_texts:
            input_ids, length = encode_text(review, vocabulary, max_length)
            encoded_rows.append(input_ids)
            sequence_lengths.append(length)

        if encoded_rows:
            self._input_ids = torch.tensor(encoded_rows, dtype=torch.long)
        else:
            self._input_ids = torch.empty((0, max_length), dtype=torch.long)
        self._labels = torch.tensor(valid_labels, dtype=torch.long)
        self._lengths = torch.tensor(sequence_lengths, dtype=torch.long)
        self.max_length = max_length
        self.vocabulary_size = vocabulary.size

    def __len__(self) -> int:
        return self._labels.size(0)

    def __getitem__(self, index: int) -> dict[str, Tensor]:
        return {
            "input_ids": self._input_ids[index],
            "label": self._labels[index],
            "length": self._lengths[index],
        }


def create_data_loaders(
    training_dataset: TextReviewDataset,
    validation_dataset: TextReviewDataset,
    batch_size: int = DEFAULT_BATCH_SIZE,
    num_workers: int = DEFAULT_NUM_WORKERS,
    seed: int = DEFAULT_RANDOM_SEED,
    device: str | torch.device = "cpu",
    pin_memory: bool | None = None,
) -> tuple[DataLoader, DataLoader]:
    """Create a shuffled training loader and ordered validation loader.

    A seeded generator makes training shuffle order reproducible. Workers
    default to zero to keep loading simple in Windows notebooks and scripts.
    Pinning defaults on only when the requested target device is CUDA.
    """
    if not isinstance(training_dataset, TextReviewDataset):
        raise TypeError("training_dataset must be a TextReviewDataset.")
    if not isinstance(validation_dataset, TextReviewDataset):
        raise TypeError("validation_dataset must be a TextReviewDataset.")
    if len(training_dataset) == 0 or len(validation_dataset) == 0:
        raise ValueError("Training and validation datasets must both be non-empty.")
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or batch_size < 1
    ):
        raise ValueError("batch_size must be a positive integer.")
    if (
        isinstance(num_workers, bool)
        or not isinstance(num_workers, int)
        or num_workers < 0
    ):
        raise ValueError("num_workers must be a non-negative integer.")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer.")
    if pin_memory is not None and not isinstance(pin_memory, bool):
        raise TypeError("pin_memory must be True, False, or None.")

    target_device = torch.device(device)
    use_pin_memory = (
        target_device.type == "cuda" and torch.cuda.is_available()
        if pin_memory is None
        else pin_memory
    )
    generator = torch.Generator()
    generator.manual_seed(seed)

    training_loader = DataLoader(
        training_dataset,
        batch_size=batch_size,
        shuffle=True,
        num_workers=num_workers,
        pin_memory=use_pin_memory,
        generator=generator,
    )
    validation_loader = DataLoader(
        validation_dataset,
        batch_size=batch_size,
        shuffle=False,
        num_workers=num_workers,
        pin_memory=use_pin_memory,
    )
    return training_loader, validation_loader
