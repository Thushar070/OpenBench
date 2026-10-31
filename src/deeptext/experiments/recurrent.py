"""Train one recurrent architecture on the shared IMDb train/validation split."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
from collections.abc import Callable
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
from deeptext.evaluation.comparison import evaluate_comparison_model
from deeptext.inference.predictor import load_recurrent_predictor
from deeptext.models.recurrent import (
    GRU,
    LSTM,
    RecurrentConfig,
    VanillaRNN,
)
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
    train_neural_model,
)

SEED = 42
VALIDATION_SIZE = 0.2
MODEL_TYPES = {"rnn": VanillaRNN, "lstm": LSTM, "gru": GRU}
MODEL_NAMES = {"rnn": "VanillaRNN", "lstm": "LSTM", "gru": "GRU"}


def _class_counts(labels: pd.Series) -> dict[str, int]:
    counts = labels.value_counts().to_dict()
    return {"negative": int(counts.get(0, 0)), "positive": int(counts.get(1, 0))}


def _partition_fingerprint(records: pd.DataFrame) -> str:
    digest = hashlib.sha256()
    for review_id, label, text in records[["review_id", "label", "text"]].itertuples(
        index=False, name=None
    ):
        text_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        digest.update(f"{review_id}\0{int(label)}\0{text_digest}\n".encode("utf-8"))
    return digest.hexdigest()


def _save_validation_predictions(
    predictor,
    validation: pd.DataFrame,
    run_directory: Path,
) -> tuple[dict[str, Any], Path]:
    predictions = predictor.predict_batch(
        validation["text"].tolist(), batch_size=DEFAULT_BATCH_SIZE
    )
    predicted_labels = [int(item["predicted_class"]) for item in predictions]
    positive_probabilities = [
        float(item["positive_probability"]) for item in predictions
    ]
    metrics = evaluate_comparison_model(
        validation["label"].tolist(), predicted_labels, positive_probabilities
    )
    path = run_directory / "validation_predictions.csv"
    pd.DataFrame(
        {
            "review_id": validation["review_id"].tolist(),
            "true_label": validation["label"].tolist(),
            "predicted_label": predicted_labels,
            "negative_probability": [
                float(item["negative_probability"]) for item in predictions
            ],
            "positive_probability": positive_probabilities,
        }
    ).to_csv(path, index=False)
    return metrics, path


def backfill_validation_predictions(
    run_directory: str | Path,
    dataset_dir: str | Path = Path("datasets/aclImdb"),
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Save ordered validation predictions for a previously completed run.

    This helper reuses only the official training partition and refuses to
    write unless its deterministic partition fingerprints match the run.
    """
    run_path = Path(run_directory).expanduser()
    report = progress or (lambda _message: None)
    metadata_path = run_path / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    report("Reconstructing the labeled training/validation split...")
    training_records = load_imdb_split(dataset_dir, "train")
    training, validation = split_training_data(
        training_records, validation_size=VALIDATION_SIZE, seed=SEED
    )
    split = metadata.get("split", {})
    if split.get("training_partition_sha256") != _partition_fingerprint(
        training
    ) or split.get("validation_partition_sha256") != _partition_fingerprint(validation):
        raise ValueError(
            "Run metadata does not match the reconstructed training split."
        )
    predictor = load_recurrent_predictor(
        run_path, device=metadata.get("selected_device", "cpu")
    )
    report(f"Reproducing validation metrics on {predictor.device}...")
    metrics, prediction_path = _save_validation_predictions(
        predictor, validation, run_path
    )
    for name in ("accuracy", "precision", "recall", "f1"):
        if not np.isclose(
            metrics[name], metadata["best_validation_metrics"][name], rtol=0, atol=1e-12
        ):
            raise RuntimeError(
                f"Reloaded validation {name} differs from saved metrics."
            )
    metadata["validation_metrics"] = metrics
    metadata["validation_predictions"] = prediction_path.name
    metadata["validation_inference_device"] = str(predictor.device)
    metadata["checkpoint_size_bytes"] = (
        (run_path / metadata["checkpoint"]).stat().st_size
    )
    metadata.update(
        {
            "checkpoint_reload_verified": True,
            "reload_verification_samples": len(validation),
            "reload_validation_metrics_match": True,
            "reload_probabilities_match": None,
            "reload_predictions_match": None,
            "inference_vocabulary_matches": (
                predictor.vocabulary.to_dict()
                == Vocabulary.load(run_path / metadata["vocabulary"]).to_dict()
            ),
        }
    )
    _write_json(metadata_path, metadata)
    report(f"Validation metrics and predictions saved to {run_path}.")
    return metadata


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def run_recurrent_experiment(
    architecture: str,
    dataset_dir: str | Path = Path("datasets/aclImdb"),
    output_root: str | Path = Path("outputs/recurrent"),
    device: str = "auto",
    training_config: TrainingConfig | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Train one RNN-family model and save a new validation-only run.

    Only the labeled official training split is read. The official test split
    is neither loaded nor used for selection, evaluation, or metadata checks.
    """
    if architecture not in MODEL_TYPES:
        raise ValueError("architecture must be one of: rnn, lstm, gru.")
    model_name = MODEL_NAMES[architecture]
    report = progress or (lambda _message: None)
    settings = training_config or TrainingConfig(
        max_epochs=10,
        learning_rate=0.001,
        patience=3,
        min_delta=0.001,
        seed=SEED,
        device=device,
    )
    requested_device = resolve_device(settings.device)
    seed_everything(settings.seed, settings.deterministic)

    report(f"Loading labeled IMDb training reviews for {model_name}...")
    official_training = load_imdb_split(dataset_dir, "train")
    training, validation = split_training_data(
        official_training, validation_size=VALIDATION_SIZE, seed=SEED
    )
    report(
        f"Prepared {len(training):,} training and {len(validation):,} validation rows."
    )
    training_texts = training["text"].tolist()
    validation_texts = validation["text"].tolist()
    vocabulary = Vocabulary.build(
        training_texts,
        max_size=DEFAULT_MAX_VOCAB_SIZE,
        min_frequency=DEFAULT_MIN_TOKEN_FREQUENCY,
    )
    report(f"Built training-only vocabulary with {vocabulary.size:,} tokens.")
    maximum_length = DEFAULT_MAX_SEQUENCE_LENGTH
    training_dataset = TextReviewDataset(
        training_texts,
        training["label"].tolist(),
        vocabulary,
        max_length=maximum_length,
    )
    validation_dataset = TextReviewDataset(
        validation_texts,
        validation["label"].tolist(),
        vocabulary,
        max_length=maximum_length,
    )
    training_loader, validation_loader = create_data_loaders(
        training_dataset,
        validation_dataset,
        batch_size=DEFAULT_BATCH_SIZE,
        seed=settings.seed,
        device=requested_device,
    )
    report("Encoded data and created seeded DataLoaders.")

    model_config = RecurrentConfig(
        vocab_size=vocabulary.size,
        embedding_dim=100,
        hidden_dim=100,
        num_layers=1,
        dropout=0.5,
        padding_idx=vocabulary.pad_id,
        max_sequence_length=maximum_length,
    )
    seed_everything(settings.seed, settings.deterministic)
    model = MODEL_TYPES[architecture](model_config)
    experiment_time = datetime.now(UTC)
    experiment_id = f"imdb-{architecture}-{experiment_time:%Y%m%dT%H%M%S.%fZ}"
    run_directory = Path(output_root).expanduser() / experiment_id
    checkpoint_manager = CheckpointManager(run_directory, vocabulary)

    report(f"Training on {requested_device} with validation-F1 early stopping...")
    if requested_device.type == "cuda":
        torch.cuda.reset_peak_memory_stats(requested_device)
    result = train_neural_model(
        model,
        training_loader,
        validation_loader,
        config=settings,
        checkpoint_manager=checkpoint_manager,
    )
    peak_memory = (
        {
            "allocated_bytes": torch.cuda.max_memory_allocated(requested_device),
            "reserved_bytes": torch.cuda.max_memory_reserved(requested_device),
        }
        if requested_device.type == "cuda"
        else None
    )

    metadata: dict[str, Any] = {
        "experiment_id": experiment_id,
        "created_at_utc": experiment_time.isoformat(),
        "dataset": "aclImdb v1.0",
        "model_name": model_name,
        "model_config": model_config.to_dict(),
        "training_config": settings.to_dict(),
        "parameter_count": sum(parameter.numel() for parameter in model.parameters()),
        "preprocessing": {
            "version": PREPROCESSING_VERSION,
            "max_vocabulary_size": vocabulary.max_size,
            "minimum_token_frequency": vocabulary.min_frequency,
            "actual_vocabulary_size": vocabulary.size,
            "maximum_sequence_length": maximum_length,
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
            "training_partition_sha256": _partition_fingerprint(training),
            "validation_partition_sha256": _partition_fingerprint(validation),
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
        "peak_cuda_memory": peak_memory,
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
    report("Training finished; verifying checkpoint reload and raw-text inference...")
    predictor = load_recurrent_predictor(run_directory, device=result.device)
    sample = validation_dataset[: min(16, len(validation_dataset))]
    with torch.inference_mode():
        expected = torch.softmax(
            model(
                sample["input_ids"].to(result.device),
                sample["length"].to(result.device),
            ),
            dim=1,
        ).cpu()
        actual = torch.softmax(
            predictor.model(
                sample["input_ids"].to(result.device),
                sample["length"].to(result.device),
            ),
            dim=1,
        ).cpu()
    probabilities_match = torch.allclose(expected, actual, rtol=1e-4, atol=1e-5)
    predictions_match = torch.equal(expected.argmax(1), actual.argmax(1))
    if not probabilities_match or not predictions_match:
        raise RuntimeError(
            "Reloaded predictions did not match the training model on its device."
        )
    sample_predictions = predictor.predict_batch(validation_texts[:16], batch_size=16)
    validation_metrics, prediction_path = _save_validation_predictions(
        predictor, validation, run_directory
    )
    for metric_name in ("accuracy", "precision", "recall", "f1"):
        if not np.isclose(
            validation_metrics[metric_name],
            result.best_validation_metrics[metric_name],
            rtol=0,
            atol=1e-12,
        ):
            raise RuntimeError(
                f"Reloaded validation {metric_name} does not match the training run."
            )
    metadata.update(
        {
            "checkpoint_reload_verified": True,
            "reload_verification_samples": len(sample_predictions),
            "reload_probabilities_match": probabilities_match,
            "reload_predictions_match": predictions_match,
            "reload_validation_metrics_match": True,
            "inference_vocabulary_matches": (
                predictor.vocabulary.to_dict() == vocabulary.to_dict()
            ),
            "checkpoint_size_bytes": checkpoint_manager.checkpoint_path.stat().st_size,
            "validation_metrics": validation_metrics,
            "validation_predictions": prediction_path.name,
            "validation_inference_device": str(predictor.device),
        }
    )
    checkpoint_manager.finalize(result.history, metadata)
    report(f"Verified run saved to {run_directory}.")
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Train one recurrent model using the labeled IMDb train split."
    )
    parser.add_argument("--architecture", choices=tuple(MODEL_TYPES), required=True)
    parser.add_argument("--dataset-dir", type=Path, default=Path("datasets/aclImdb"))
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/recurrent"))
    parser.add_argument("--device", default="auto", help="auto, cpu, or cuda")
    arguments = parser.parse_args()
    result = run_recurrent_experiment(
        architecture=arguments.architecture,
        dataset_dir=arguments.dataset_dir,
        output_root=arguments.output_dir,
        device=arguments.device,
        progress=print,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
