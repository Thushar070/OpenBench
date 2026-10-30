"""Safe best-checkpoint writing and local TextCNN checkpoint loading."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

import torch

from deeptext.models.cnn import TextCNN, TextCNNConfig
from deeptext.preprocessing.vocabulary import Vocabulary


class CheckpointManager:
    """Write best weights into a new run directory and finalize its metadata."""

    def __init__(self, run_directory: str | Path, vocabulary: Vocabulary) -> None:
        self.run_directory = Path(run_directory).expanduser()
        self.run_directory.mkdir(parents=True, exist_ok=False)
        self.vocabulary_path = vocabulary.save(self.run_directory / "vocabulary.json")
        self.checkpoint_path = self.run_directory / "best_model.pt"
        self.best_validation_f1: float | None = None
        self.best_epoch: int | None = None

    def save_best(
        self,
        model: TextCNN,
        epoch: int,
        validation_metrics: dict[str, float],
    ) -> bool:
        """Atomically replace this run's weights when validation F1 improves."""
        score = validation_metrics.get("f1")
        if score is None or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError(
                "validation_metrics must contain a finite F1 value in [0, 1]."
            )
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            raise ValueError("epoch must be a positive integer.")
        if self.best_validation_f1 is not None and score <= self.best_validation_f1:
            return False

        checkpoint = {
            "format_version": 1,
            "model_config": model.config.to_dict(),
            "state_dict": {
                name: tensor.detach().cpu().clone()
                for name, tensor in model.state_dict().items()
            },
        }
        temporary_path = self.run_directory / "best_model.tmp"
        torch.save(checkpoint, temporary_path)
        os.replace(temporary_path, self.checkpoint_path)
        self.best_validation_f1 = float(score)
        self.best_epoch = epoch
        return True

    def finalize(
        self,
        history: list[dict[str, int | float]],
        metadata: dict[str, Any],
    ) -> tuple[Path, Path]:
        """Write epoch history and readable experiment metadata as JSON."""
        if self.best_epoch is None or not self.checkpoint_path.is_file():
            raise RuntimeError(
                "A best checkpoint must be saved before finalizing the run."
            )

        saved_metadata = dict(metadata)
        saved_metadata.update(
            {
                "checkpoint": self.checkpoint_path.name,
                "vocabulary": self.vocabulary_path.name,
                "best_epoch": self.best_epoch,
                "best_validation_f1": self.best_validation_f1,
            }
        )
        history_path = self.run_directory / "history.json"
        metadata_path = self.run_directory / "metadata.json"
        _write_json_atomically(history_path, {"epochs": history})
        _write_json_atomically(metadata_path, saved_metadata)
        return history_path, metadata_path


def _write_json_atomically(path: Path, value: dict[str, Any]) -> None:
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_path, path)


def load_textcnn_checkpoint(
    run_directory: str | Path,
    device: str | torch.device = "cpu",
) -> tuple[TextCNN, Vocabulary, dict[str, Any]]:
    """Load a trusted local checkpoint, vocabulary, and run metadata.

    The weights file contains only tensors and primitive configuration values
    and is loaded with ``weights_only=True``. Do not load untrusted artifacts.
    """
    source = Path(run_directory).expanduser()
    checkpoint_path = source / "best_model.pt"
    vocabulary_path = source / "vocabulary.json"
    metadata_path = source / "metadata.json"
    for required_path in (checkpoint_path, vocabulary_path, metadata_path):
        if not required_path.is_file():
            raise FileNotFoundError(f"TextCNN artifact file not found: {required_path}")

    target_device = torch.device(device)
    if target_device.type == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested but is not available.")

    checkpoint = torch.load(
        checkpoint_path,
        map_location=target_device,
        weights_only=True,
    )
    if not isinstance(checkpoint, dict) or checkpoint.get("format_version") != 1:
        raise ValueError("TextCNN checkpoint has an unsupported or invalid format.")
    config_data = checkpoint.get("model_config")
    state_dict = checkpoint.get("state_dict")
    if not isinstance(config_data, dict) or not isinstance(state_dict, dict):
        raise ValueError("TextCNN checkpoint is missing its configuration or weights.")

    config = TextCNNConfig.from_dict(config_data)
    vocabulary = Vocabulary.load(vocabulary_path)
    if config.vocab_size != vocabulary.size:
        raise ValueError("Checkpoint vocabulary size does not match vocabulary.json.")
    if config.padding_idx != vocabulary.pad_id:
        raise ValueError("Checkpoint padding ID does not match vocabulary.json.")

    model = TextCNN(config).to(target_device)
    model.load_state_dict(state_dict, strict=True)
    model.eval()
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError("TextCNN metadata must contain a JSON object.")
    return model, vocabulary, metadata
