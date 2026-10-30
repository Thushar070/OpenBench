"""Run and record a validation-only TextCNN experiment on IMDb."""

from __future__ import annotations

import argparse
import json
import platform
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import sklearn
import torch

from deeptext.data.dataset import load_imdb_split, split_training_data
from deeptext.data.torch_dataset import (
    DEFAULT_BATCH_SIZE,
    TextReviewDataset,
    create_data_loaders,
)
from deeptext.inference.predictor import load_textcnn_predictor
from deeptext.models.cnn import TextCNN, TextCNNConfig
from deeptext.preprocessing.encoding import DEFAULT_MAX_SEQUENCE_LENGTH
from deeptext.preprocessing.vocabulary import (
    DEFAULT_MAX_VOCAB_SIZE,
    DEFAULT_MIN_TOKEN_FREQUENCY,
    PREPROCESSING_VERSION,
    Vocabulary,
)
from deeptext.training.checkpoint import CheckpointManager
from deeptext.training.trainer import (
    TrainingConfig,
    resolve_device,
    seed_everything,
    train_textcnn,
)

SEED = 42
VALIDATION_SIZE = 0.2


def _class_counts(labels: pd.Series) -> dict[str, int]:
    counts = labels.value_counts().to_dict()
    return {"negative": int(counts.get(0, 0)), "positive": int(counts.get(1, 0))}


def run_textcnn_experiment(
    dataset_dir: str | Path = Path("datasets/aclImdb"),
    output_root: str | Path = Path("outputs/textcnn"),
    device: str = "auto",
) -> dict[str, Any]:
    """Train on the labeled IMDb train split and select with its validation set."""
    seed_everything(SEED)
    original_training = load_imdb_split(dataset_dir, "train")
    training, validation = split_training_data(
        original_training, validation_size=VALIDATION_SIZE, seed=SEED
    )
    training_texts = training["text"].tolist()
    validation_texts = validation["text"].tolist()
    vocabulary = Vocabulary.build(
        training_texts,
        max_size=DEFAULT_MAX_VOCAB_SIZE,
        min_frequency=DEFAULT_MIN_TOKEN_FREQUENCY,
    )
    training_dataset = TextReviewDataset(
        training_texts,
        training["label"].tolist(),
        vocabulary,
        max_length=DEFAULT_MAX_SEQUENCE_LENGTH,
    )
    validation_dataset = TextReviewDataset(
        validation_texts,
        validation["label"].tolist(),
        vocabulary,
        max_length=DEFAULT_MAX_SEQUENCE_LENGTH,
    )
    requested_device = resolve_device(device)
    training_loader, validation_loader = create_data_loaders(
        training_dataset,
        validation_dataset,
        batch_size=DEFAULT_BATCH_SIZE,
        seed=SEED,
        device=requested_device,
    )

    model_config = TextCNNConfig(
        vocab_size=vocabulary.size,
        embedding_dim=100,
        filter_sizes=(3, 4, 5),
        filters_per_size=100,
        dropout=0.5,
        padding_idx=vocabulary.pad_id,
        max_sequence_length=DEFAULT_MAX_SEQUENCE_LENGTH,
    )
    seed_everything(SEED)
    model = TextCNN(model_config)
    training_config = TrainingConfig(
        max_epochs=10,
        learning_rate=0.001,
        patience=3,
        min_delta=0.001,
        seed=SEED,
        device=str(requested_device),
    )

    created_at = datetime.now(UTC)
    experiment_id = f"imdb-textcnn-{created_at:%Y%m%dT%H%M%S.%fZ}"
    run_directory = Path(output_root).expanduser() / experiment_id
    checkpoint_manager = CheckpointManager(run_directory, vocabulary)
    result = train_textcnn(
        model,
        training_loader,
        validation_loader,
        config=training_config,
        checkpoint_manager=checkpoint_manager,
    )

    metadata: dict[str, Any] = {
        "experiment_id": experiment_id,
        "created_at_utc": created_at.isoformat(),
        "dataset": "aclImdb v1.0",
        "model_name": "TextCNN",
        "model_config": model_config.to_dict(),
        "training_config": training_config.to_dict(),
        "preprocessing": {
            "version": PREPROCESSING_VERSION,
            "max_vocabulary_size": vocabulary.max_size,
            "minimum_token_frequency": vocabulary.min_frequency,
            "actual_vocabulary_size": vocabulary.size,
            "maximum_sequence_length": DEFAULT_MAX_SEQUENCE_LENGTH,
            "padding_id": vocabulary.pad_id,
            "unknown_id": vocabulary.unk_id,
            "label_mapping": {"negative": 0, "positive": 1},
        },
        "split": {
            "source": "official labeled training split only",
            "validation_fraction": VALIDATION_SIZE,
            "random_state": SEED,
            "training_rows": len(training),
            "validation_rows": len(validation),
            "training_class_counts": _class_counts(training["label"]),
            "validation_class_counts": _class_counts(validation["label"]),
        },
        "best_validation_metrics": result.best_validation_metrics,
        "best_epoch": result.best_epoch,
        "stopped_early": result.stopped_early,
        "stopping_reason": result.stopping_reason,
        "training_duration_seconds": result.training_duration_seconds,
        "selected_device": result.device,
        "device_name": (
            torch.cuda.get_device_name(requested_device)
            if requested_device.type == "cuda"
            else platform.processor() or "CPU"
        ),
        "software_versions": {
            "python": platform.python_version(),
            "torch": str(torch.__version__),
            "numpy": np.__version__,
            "pandas": pd.__version__,
            "scikit_learn": sklearn.__version__,
        },
        "official_test_evaluated": False,
        "unlabeled_reviews_used": False,
        "vocabulary_fit_partition": "training",
        "checkpoint_reload_verified": False,
        "reload_verification_samples": 0,
        "reload_probabilities_match": False,
        "reload_predictions_match": False,
        "inference_vocabulary_matches": False,
    }
    checkpoint_manager.finalize(result.history, metadata)
    metadata["artifact_directory"] = str(run_directory.resolve())
    metadata["history_path"] = str((run_directory / "history.json").resolve())
    metadata["checkpoint_path"] = str(checkpoint_manager.checkpoint_path.resolve())

    predictor = load_textcnn_predictor(run_directory, device="cpu")
    sample = validation_dataset[: min(16, len(validation_dataset))]
    with torch.inference_mode():
        original_logits = model(
            sample["input_ids"].to(result.device), sample["length"].to(result.device)
        )
        expected_probabilities = torch.softmax(original_logits, dim=1).cpu()
        reloaded_probabilities = torch.softmax(
            predictor.model(sample["input_ids"], sample["length"]), dim=1
        )
    probabilities_match = torch.allclose(
        expected_probabilities, reloaded_probabilities, rtol=1e-5, atol=1e-6
    )
    predictions_match = torch.equal(
        expected_probabilities.argmax(dim=1),
        reloaded_probabilities.argmax(dim=1),
    )
    if not probabilities_match or not predictions_match:
        raise RuntimeError(
            "CPU checkpoint reload predictions did not match training model."
        )

    sample_predictions = [predictor.predict(text) for text in validation_texts[:16]]
    if any(
        not np.isclose(
            prediction["negative_probability"] + prediction["positive_probability"],
            1.0,
            atol=1e-6,
        )
        for prediction in sample_predictions
    ):
        raise RuntimeError("Reloaded inference returned invalid class probabilities.")
    metadata.update(
        {
            "checkpoint_reload_verified": True,
            "reload_verification_samples": len(sample_predictions),
            "reload_probabilities_match": probabilities_match,
            "reload_predictions_match": predictions_match,
            "inference_vocabulary_matches": (
                predictor.vocabulary.to_dict() == vocabulary.to_dict()
            ),
        }
    )
    checkpoint_manager.finalize(result.history, metadata)
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Train a TextCNN using the labeled IMDb train split only."
    )
    parser.add_argument("--dataset-dir", type=Path, default=Path("datasets/aclImdb"))
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/textcnn"))
    parser.add_argument("--device", default="auto", help="auto, cpu, or cuda")
    arguments = parser.parse_args()
    result = run_textcnn_experiment(
        dataset_dir=arguments.dataset_dir,
        output_root=arguments.output_dir,
        device=arguments.device,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
