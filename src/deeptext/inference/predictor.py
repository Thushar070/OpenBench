"""Raw-review inference using a saved TextCNN and its training vocabulary."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import torch

from deeptext.models.cnn import TextCNN
from deeptext.preprocessing.encoding import encode_text
from deeptext.preprocessing.vocabulary import Vocabulary
from deeptext.training.checkpoint import load_textcnn_checkpoint

LABEL_NAMES = {0: "negative", 1: "positive"}


class TextCNNSentimentPredictor:
    """Predict sentiment and class probabilities for one raw review at a time."""

    def __init__(
        self,
        model: TextCNN,
        vocabulary: Vocabulary,
        device: str | torch.device = "cpu",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(model, TextCNN):
            raise TypeError("model must be a TextCNN instance.")
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
        input_ids, length = encode_text(
            review,
            self.vocabulary,
            max_length=self.model.config.max_sequence_length,
        )
        input_tensor = torch.tensor([input_ids], dtype=torch.long, device=self.device)
        length_tensor = torch.tensor([length], dtype=torch.long, device=self.device)

        with torch.inference_mode():
            logits = self.model(input_tensor, length_tensor)
            probabilities = torch.softmax(logits, dim=1)[0].cpu().tolist()
        if not all(math.isfinite(probability) for probability in probabilities):
            raise RuntimeError("TextCNN produced a non-finite prediction probability.")
        if not math.isclose(sum(probabilities), 1.0, rel_tol=1e-5, abs_tol=1e-6):
            raise RuntimeError("TextCNN class probabilities do not sum to one.")

        predicted_class = int(torch.tensor(probabilities).argmax().item())
        return {
            "predicted_class": predicted_class,
            "sentiment": LABEL_NAMES[predicted_class],
            "negative_probability": float(probabilities[0]),
            "positive_probability": float(probabilities[1]),
        }


def load_textcnn_predictor(
    run_directory: str | Path,
    device: str | torch.device = "cpu",
) -> TextCNNSentimentPredictor:
    """Load a trusted run directory and prepare its model for inference."""
    model, vocabulary, metadata = load_textcnn_checkpoint(run_directory, device)
    return TextCNNSentimentPredictor(model, vocabulary, device, metadata)
