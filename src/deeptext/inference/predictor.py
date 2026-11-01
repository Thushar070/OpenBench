"""Raw-review inference using a saved TextCNN and its training vocabulary."""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import torch
from torch import nn

from deeptext.models.cnn import TextCNN
from deeptext.models.recurrent import RECURRENT_MODELS
from deeptext.preprocessing.encoding import encode_text
from deeptext.preprocessing.vocabulary import Vocabulary
from deeptext.training.checkpoint import (
    load_recurrent_checkpoint,
    load_textcnn_checkpoint,
)

LABEL_NAMES = {0: "negative", 1: "positive"}


class NeuralSentimentPredictor:
    """Predict binary sentiment with a saved length-aware neural model."""

    def __init__(
        self,
        model: nn.Module,
        vocabulary: Vocabulary,
        device: str | torch.device = "cpu",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        supported_models = (TextCNN, *RECURRENT_MODELS.values())
        if not isinstance(model, supported_models):
            raise TypeError("model must be a supported text-classification model.")
        if not isinstance(vocabulary, Vocabulary):
            raise TypeError("vocabulary must be a built Vocabulary instance.")
        if model.config.vocab_size != vocabulary.size:
            raise ValueError(
                "Model vocabulary size does not match the supplied vocabulary."
            )
        if model.config.padding_idx != vocabulary.pad_id:
            raise ValueError("Model padding ID does not match the supplied vocabulary.")

        self.device = torch.device(device)
        if self.device.type == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDA was requested but is not available.")
        self.model = model.to(self.device)
        self.model.eval()
        self.vocabulary = vocabulary
        self.metadata = metadata or {}

    def predict(self, review: str) -> dict[str, int | str | float]:
        """Return the predicted label and finite negative/positive probabilities."""
        return self.predict_batch([review], batch_size=1)[0]

    def predict_batch(
        self,
        reviews: Sequence[str],
        batch_size: int = 128,
    ) -> list[dict[str, int | str | float]]:
        """Predict ordered raw reviews in batches without fitting preprocessing."""
        if isinstance(reviews, str):
            raise TypeError("reviews must be a sequence of review strings.")
        texts = list(reviews)
        if not all(isinstance(review, str) for review in texts):
            raise TypeError("reviews must contain only strings.")
        if (
            isinstance(batch_size, bool)
            or not isinstance(batch_size, int)
            or batch_size < 1
        ):
            raise ValueError("batch_size must be a positive integer.")

        predictions: list[dict[str, int | str | float]] = []
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            encoded = [
                encode_text(
                    review,
                    self.vocabulary,
                    max_length=self.model.config.max_sequence_length,
                )
                for review in batch
            ]
            input_ids = torch.tensor(
                [row[0] for row in encoded], dtype=torch.long, device=self.device
            )
            lengths = torch.tensor(
                [row[1] for row in encoded], dtype=torch.long, device=self.device
            )

            with torch.inference_mode():
                logits = self.model(input_ids, lengths)
                probabilities = torch.softmax(logits, dim=1).cpu().tolist()
                predicted_classes = logits.argmax(dim=1).cpu().tolist()

            for predicted_class, class_probabilities in zip(
                predicted_classes, probabilities, strict=True
            ):
                if not all(math.isfinite(value) for value in class_probabilities):
                    raise RuntimeError(
                        "Neural model produced a non-finite prediction probability."
                    )
                if not math.isclose(
                    sum(class_probabilities), 1.0, rel_tol=1e-5, abs_tol=1e-6
                ):
                    raise RuntimeError("TextCNN class probabilities do not sum to one.")
                predicted_label = int(predicted_class)
                predictions.append(
                    {
                        "predicted_class": predicted_label,
                        "sentiment": LABEL_NAMES[predicted_label],
                        "negative_probability": float(class_probabilities[0]),
                        "positive_probability": float(class_probabilities[1]),
                    }
                )
        return predictions


class TextCNNSentimentPredictor(NeuralSentimentPredictor):
    """Raw-review predictor restricted to a TextCNN model."""

    def __init__(
        self,
        model: TextCNN,
        vocabulary: Vocabulary,
        device: str | torch.device = "cpu",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(model, TextCNN):
            raise TypeError("model must be a TextCNN instance.")
        super().__init__(model, vocabulary, device, metadata)


class RecurrentSentimentPredictor(NeuralSentimentPredictor):
    """Raw-review predictor restricted to vanilla RNN, LSTM, or GRU models."""

    def __init__(
        self,
        model: nn.Module,
        vocabulary: Vocabulary,
        device: str | torch.device = "cpu",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if type(model).__name__ not in RECURRENT_MODELS:
            raise TypeError("model must be a vanilla RNN, LSTM, or GRU instance.")
        super().__init__(model, vocabulary, device, metadata)


def load_textcnn_predictor(
    run_directory: str | Path,
    device: str | torch.device = "cpu",
) -> TextCNNSentimentPredictor:
    """Load a trusted run directory and prepare its model for inference."""
    model, vocabulary, metadata = load_textcnn_checkpoint(run_directory, device)
    return TextCNNSentimentPredictor(model, vocabulary, device, metadata)


def load_recurrent_predictor(
    run_directory: str | Path,
    device: str | torch.device = "cpu",
) -> RecurrentSentimentPredictor:
    """Load a local recurrent-model run for raw-review inference."""
    model, vocabulary, metadata = load_recurrent_checkpoint(run_directory, device)
    return RecurrentSentimentPredictor(model, vocabulary, device, metadata)
