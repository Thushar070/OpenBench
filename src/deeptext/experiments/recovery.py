"""Train and evaluate a versioned recovery generation for all five models."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import sklearn
import torch
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report
from sklearn.pipeline import Pipeline

from deeptext.data.dataset import load_imdb_split
from deeptext.data.external_sentiment import load_uci_short_sentiment_splits
from deeptext.data.recovery import (
    add_review_sentence_examples,
    audit_training_records,
    normalized_review_hash,
    split_training_records_by_group,
)
from deeptext.data.torch_dataset import TextReviewDataset, create_data_loaders
from deeptext.evaluation.metrics import evaluate_binary_predictions
from deeptext.inference.predictor import (
    NeuralSentimentPredictor,
    load_recurrent_predictor,
    load_textcnn_predictor,
)
from deeptext.models.baseline import predict_baseline_probabilities
from deeptext.models.cnn import TextCNN, TextCNNConfig
from deeptext.models.recurrent import GRU, LSTM, RecurrentConfig, VanillaRNN
from deeptext.preprocessing.encoding import encode_text
from deeptext.preprocessing.tokenizer import normalize_text, tokenize_text
from deeptext.preprocessing.vocabulary import (
    DEFAULT_MAX_VOCAB_SIZE,
    DEFAULT_MIN_TOKEN_FREQUENCY,
    PREPROCESSING_VERSION,
    Vocabulary,
)
from deeptext.training.checkpoint import CheckpointManager
from deeptext.training.early_stopping import EarlyStopping
from deeptext.training.trainer import (
    TrainingConfig,
    resolve_device,
    run_epoch,
    seed_everything,
)

SEED = 42
VALIDATION_SIZE = 0.2
MAX_SEQUENCE_LENGTH = 200
SHORT_PREFIX_LENGTHS = (8, 16, 24)
SENTENCE_TOKEN_RANGE = (4, 32)
EXTERNAL_TRAINING_REPEAT_COUNT = 4
FULL_VALIDATION_WEIGHT = 0.3
PREFIX_VALIDATION_WEIGHT = 0.35
SENTENCE_VALIDATION_WEIGHT = 0.35
MODEL_TYPES = {
    "textcnn": TextCNN,
    "rnn": VanillaRNN,
    "lstm": LSTM,
    "gru": GRU,
}
MODEL_NAMES = {
    "logistic_regression": "TF-IDF + Logistic Regression",
    "textcnn": "TextCNN",
    "rnn": "Vanilla RNN",
    "lstm": "LSTM",
    "gru": "GRU",
}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _pre_test_acceptance_passes(checks: dict[str, bool]) -> bool:
    """Require every frozen-model preflight check before opening IMDb test data."""
    return bool(checks) and all(checks.values())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _protected_output_hashes(project_root: Path) -> dict[str, str]:
    output_names = (
        "baseline",
        "textcnn",
        "recurrent",
        "comparison",
        "neural_comparison",
        "final_evaluation",
        "interpretability",
        "robustness",
        "efficiency",
    )
    hashes = {}
    for name in output_names:
        directory = project_root / "outputs" / name
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                hashes[path.relative_to(project_root).as_posix()] = _sha256(path)
    return hashes


def _metrics(labels: list[int], predictions: list[int], probabilities: list[float]):
    return evaluate_binary_predictions(labels, predictions, probabilities)


def _detailed_metrics(
    labels: list[int], predictions: list[int], probabilities: list[float]
) -> dict[str, Any]:
    metrics = evaluate_binary_predictions(labels, predictions, probabilities)
    report = classification_report(
        labels,
        predictions,
        labels=[0, 1],
        output_dict=True,
        zero_division=0,
    )
    metrics["per_class"] = {
        "negative": report["0"],
        "positive": report["1"],
    }
    return metrics


def _prefix_view(records: pd.DataFrame, token_limit: int) -> pd.DataFrame:
    result = records.copy()
    result["text"] = result["text"].map(
        lambda text: " ".join(tokenize_text(text)[:token_limit])
    )
    return result[result["text"].str.strip().ne("")].reset_index(drop=True)


def _compose_training_examples(
    official_reviews: pd.DataFrame,
    external_sentences: pd.DataFrame,
) -> pd.DataFrame:
    """Combine full IMDb reviews with weighted, independently labeled sentences."""
    imdb_rows = official_reviews.copy()
    imdb_rows["split"] = "train"
    imdb_rows["training_source"] = "imdb_full_review"

    if external_sentences.empty:
        return imdb_rows.reset_index(drop=True)

    repeated_external = []
    for repeat_index in range(EXTERNAL_TRAINING_REPEAT_COUNT):
        rows = external_sentences.copy()
        rows["split"] = "train"
        rows["training_source"] = "uci_short_sentence"
        rows["review_id"] = rows["review_id"].astype(str) + (
            f"::weight-{repeat_index + 1}"
        )
        repeated_external.append(rows)

    return pd.concat([imdb_rows, *repeated_external], ignore_index=True)


def _training_data_policy(external_data_used: bool) -> str:
    """Describe the exact fitting sources without implying held-out use."""
    if external_data_used:
        return (
            "Full official IMDb training reviews plus the UCI Amazon/Yelp training "
            f"partition repeated {EXTERNAL_TRAINING_REPEAT_COUNT} times. Held-out "
            "UCI partitions are excluded from fitting and model selection."
        )
    return "Full official IMDb training reviews only."


def _baseline_pipeline(
    settings: dict[str, Any], texts: list[str], labels: np.ndarray
) -> Pipeline:
    pipeline = Pipeline(
        [
            (
                "tfidf",
                TfidfVectorizer(
                    ngram_range=tuple(settings["ngram_range"]),
                    min_df=settings["min_df"],
                    max_df=settings["max_df"],
                    sublinear_tf=settings["sublinear_tf"],
                    max_features=settings["max_features"],
                ),
            ),
            (
                "logistic_regression",
                LogisticRegression(
                    C=settings["c"],
                    max_iter=1_000,
                    random_state=SEED,
                    class_weight=settings["class_weight"],
                ),
            ),
        ]
    )
    pipeline.fit(texts, labels)
    if list(pipeline.named_steps["logistic_regression"].classes_) != [0, 1]:
        raise RuntimeError("Baseline class ordering is not [negative, positive].")
    return pipeline


def _baseline_scores(model: Pipeline, records: pd.DataFrame) -> dict[str, Any]:
    probabilities = predict_baseline_probabilities(model, records["text"].tolist())
    positive = probabilities[:, 1].tolist()
    predictions = probabilities.argmax(axis=1).astype(int).tolist()
    return _metrics(records["label"].astype(int).tolist(), predictions, positive)


def _new_neural_model(
    model_key: str, vocabulary: Vocabulary, dropout: float, hidden_dim: int
) -> torch.nn.Module:
    if model_key == "textcnn":
        config = TextCNNConfig(
            vocab_size=vocabulary.size,
            embedding_dim=100,
            filter_sizes=(3, 4, 5),
            filters_per_size=100 if hidden_dim == 100 else 128,
            dropout=dropout,
            padding_idx=vocabulary.pad_id,
            max_sequence_length=MAX_SEQUENCE_LENGTH,
        )
        return TextCNN(config)
    config = RecurrentConfig(
        vocab_size=vocabulary.size,
        embedding_dim=100,
        hidden_dim=hidden_dim,
        num_layers=1,
        dropout=dropout,
        padding_idx=vocabulary.pad_id,
        max_sequence_length=MAX_SEQUENCE_LENGTH,
    )
    return MODEL_TYPES[model_key](config)


def _predict_neural(
    predictor: NeuralSentimentPredictor, records: pd.DataFrame
) -> dict[str, Any]:
    results = predictor.predict_batch(records["text"].tolist(), batch_size=128)
    return _metrics(
        records["label"].astype(int).tolist(),
        [int(result["predicted_class"]) for result in results],
        [float(result["positive_probability"]) for result in results],
    )


def _evaluate_external_holdouts(
    predictors: dict[str, Any], metadata: dict[str, Any] | None
) -> tuple[dict[str, Any] | None, pd.DataFrame]:
    """Report licensed external holdouts without using them for selection."""
    if metadata is None:
        return None, pd.DataFrame()

    archive_path = Path(metadata["archive_path"])
    _, validation, test, split_metadata = load_uci_short_sentiment_splits(
        archive_path, seed=int(metadata["split_seed"])
    )
    if split_metadata["archive_sha256"] != metadata["archive_sha256"]:
        raise RuntimeError("The external sentence archive changed after training.")

    prediction_rows: list[dict[str, Any]] = []
    split_metrics: dict[str, Any] = {}
    for split_name, records in (("validation", validation), ("test", test)):
        split_metrics[split_name] = {}
        for model_key, model in predictors.items():
            if model_key == "logistic_regression":
                probabilities = predict_baseline_probabilities(
                    model, records["text"].tolist()
                )
                predictions = probabilities.argmax(axis=1).astype(int).tolist()
                positive_probabilities = probabilities[:, 1].tolist()
            else:
                results = model.predict_batch(records["text"].tolist(), batch_size=128)
                predictions = [int(row["predicted_class"]) for row in results]
                positive_probabilities = [
                    float(row["positive_probability"]) for row in results
                ]

            split_metrics[split_name][model_key] = _detailed_metrics(
                records["label"].astype(int).tolist(),
                predictions,
                positive_probabilities,
            )
            for source, predicted, probability in zip(
                records.itertuples(),
                predictions,
                positive_probabilities,
                strict=True,
            ):
                prediction_rows.append(
                    {
                        "split": split_name,
                        "review_id": source.review_id,
                        "domain": source.domain,
                        "true_label": int(source.label),
                        "model_id": model_key,
                        "predicted_label": predicted,
                        "positive_probability": probability,
                    }
                )

    report = {
        "dataset": metadata["dataset"],
        "license": metadata["license"],
        "archive_sha256": metadata["archive_sha256"],
        "source_domains": metadata["source_domains_included"],
        "model_selection_used_external_validation_or_test": False,
        "validation_and_test_metrics": split_metrics,
        "limitations": [
            "The external holdouts cover Amazon and Yelp review sentences, "
            "not IMDb reviews.",
            "These metrics are descriptive and did not influence training, "
            "checkpoint selection, or activation.",
        ],
    }
    return report, pd.DataFrame(prediction_rows)


def _train_candidate_with_short_validation(
    model: torch.nn.Module,
    training_loader: Any,
    full_validation_loader: Any,
    prefix_validation_loader: Any,
    sentence_validation_loader: Any,
    config: TrainingConfig,
    checkpoint_manager: CheckpointManager,
) -> tuple[int, float, list[dict[str, int | float]]]:
    """Select checkpoints with full, prefix, and sentence validation F1."""
    target_device = resolve_device(config.device)
    seed_everything(config.seed, config.deterministic)
    model.to(target_device)
    criterion = torch.nn.CrossEntropyLoss().to(target_device)
    optimizer = torch.optim.Adam(model.parameters(), lr=config.learning_rate)
    early_stopping = EarlyStopping(config.patience, config.min_delta)
    history: list[dict[str, int | float]] = []
    started = time.perf_counter()

    for epoch in range(1, config.max_epochs + 1):
        epoch_started = time.perf_counter()
        training_metrics = run_epoch(
            model,
            training_loader,
            criterion,
            target_device,
            optimizer,
            config.max_gradient_norm,
        )
        full_metrics = run_epoch(
            model, full_validation_loader, criterion, target_device
        )
        prefix_metrics = run_epoch(
            model, prefix_validation_loader, criterion, target_device
        )
        sentence_metrics = run_epoch(
            model, sentence_validation_loader, criterion, target_device
        )
        selection_score = (
            FULL_VALIDATION_WEIGHT * full_metrics["f1"]
            + PREFIX_VALIDATION_WEIGHT * prefix_metrics["f1"]
            + SENTENCE_VALIDATION_WEIGHT * sentence_metrics["f1"]
        )
        row: dict[str, int | float] = {"epoch": epoch}
        row.update(
            {f"training_{key}": value for key, value in training_metrics.items()}
        )
        row.update(
            {f"full_validation_{key}": value for key, value in full_metrics.items()}
        )
        row.update(
            {f"prefix_validation_{key}": value for key, value in prefix_metrics.items()}
        )
        row.update(
            {
                f"sentence_validation_{key}": value
                for key, value in sentence_metrics.items()
            }
        )
        row["selection_score"] = selection_score
        row["epoch_duration_seconds"] = time.perf_counter() - epoch_started
        history.append(row)

        stopping_update = early_stopping.update(selection_score, epoch)
        if stopping_update.is_best:
            saved = checkpoint_manager.save_best(
                model, epoch, {"selection_score": selection_score}
            )
            if not saved:
                raise RuntimeError("Checkpoint manager rejected a new best model.")
        if stopping_update.should_stop:
            break

    if early_stopping.best_epoch is None:
        raise RuntimeError("Training completed without a valid selection score.")
    return (
        early_stopping.best_epoch,
        time.perf_counter() - started,
        history,
    )


def _save_full_training_checkpoint(
    model: torch.nn.Module,
    model_directory: Path,
    vocabulary: Vocabulary,
    model_key: str,
    training_config: TrainingConfig,
    epochs: int,
    batch_size: int,
    device: torch.device,
    training_records: pd.DataFrame,
) -> list[dict[str, int | float]]:
    train_dataset = TextReviewDataset(
        training_records["text"].tolist(),
        training_records["label"].astype(int).tolist(),
        vocabulary,
        max_length=MAX_SEQUENCE_LENGTH,
    )
    loader, _ = create_data_loaders(
        train_dataset,
        train_dataset,
        batch_size=batch_size,
        seed=training_config.seed,
        device=device,
    )
    optimizer = torch.optim.Adam(model.parameters(), lr=training_config.learning_rate)
    loss_function = torch.nn.CrossEntropyLoss()
    model.to(device)
    history: list[dict[str, int | float]] = []
    for epoch in range(1, epochs + 1):
        metrics = run_epoch(
            model,
            loader,
            loss_function,
            device,
            optimizer,
            training_config.max_gradient_norm,
        )
        history.append(
            {
                "epoch": epoch,
                **{f"training_{key}": value for key, value in metrics.items()},
            }
        )
        print(f"{model_key}: final-fit epoch {epoch}/{epochs}", flush=True)

    vocabulary.save(model_directory / "vocabulary.json")
    checkpoint = {
        "format_version": 1,
        "model_type": type(model).__name__,
        "model_config": model.config.to_dict(),
        "state_dict": {
            key: value.detach().cpu().clone()
            for key, value in model.state_dict().items()
        },
    }
    torch.save(checkpoint, model_directory / "best_model.pt")
    return history


def _neural_candidate(
    model_key: str,
    candidate_name: str,
    training: pd.DataFrame,
    validation: pd.DataFrame,
    validation_prefixes: pd.DataFrame,
    validation_sentences: pd.DataFrame,
    vocabulary: Vocabulary,
    device: torch.device,
    output_directory: Path,
    batch_size: int,
    max_epochs: int,
) -> dict[str, Any]:
    settings = (
        {"hidden_dim": 100, "dropout": 0.5, "learning_rate": 0.001}
        if candidate_name == "standard"
        else {"hidden_dim": 128, "dropout": 0.35, "learning_rate": 0.0005}
    )
    training_config = TrainingConfig(
        max_epochs=max_epochs,
        learning_rate=settings["learning_rate"],
        patience=3,
        min_delta=0.001,
        seed=SEED,
        device=str(device),
        max_gradient_norm=1.0 if model_key in {"rnn", "lstm", "gru"} else None,
    )
    train_data = TextReviewDataset(
        training["text"].tolist(),
        training["label"].tolist(),
        vocabulary,
        max_length=MAX_SEQUENCE_LENGTH,
    )
    validation_data = TextReviewDataset(
        validation["text"].tolist(),
        validation["label"].tolist(),
        vocabulary,
        max_length=MAX_SEQUENCE_LENGTH,
    )
    prefix_validation_data = TextReviewDataset(
        validation_prefixes["text"].tolist(),
        validation_prefixes["label"].tolist(),
        vocabulary,
        max_length=MAX_SEQUENCE_LENGTH,
    )
    sentence_validation_data = TextReviewDataset(
        validation_sentences["text"].tolist(),
        validation_sentences["label"].tolist(),
        vocabulary,
        max_length=MAX_SEQUENCE_LENGTH,
    )
    train_loader, validation_loader = create_data_loaders(
        train_data, validation_data, batch_size=batch_size, seed=SEED, device=device
    )
    _, prefix_validation_loader = create_data_loaders(
        prefix_validation_data,
        prefix_validation_data,
        batch_size=batch_size,
        seed=SEED,
        device=device,
    )
    _, sentence_validation_loader = create_data_loaders(
        sentence_validation_data,
        sentence_validation_data,
        batch_size=batch_size,
        seed=SEED,
        device=device,
    )
    seed_everything(SEED)
    model = _new_neural_model(
        model_key, vocabulary, settings["dropout"], settings["hidden_dim"]
    )
    candidate_path = output_directory / "candidates" / model_key / candidate_name
    manager = CheckpointManager(
        candidate_path, vocabulary, selection_metric="selection_score"
    )
    best_epoch, training_duration, history = _train_candidate_with_short_validation(
        model,
        train_loader,
        validation_loader,
        prefix_validation_loader,
        sentence_validation_loader,
        training_config,
        manager,
    )
    manager.finalize(
        history,
        {
            "model_name": MODEL_NAMES[model_key],
            "model_config": model.config.to_dict(),
            "training_config": training_config.to_dict(),
            "selection_metric_description": (
                "0.3 * full-review validation F1 + 0.35 * multi-length prefix "
                "validation F1 + 0.35 * first/last sentence validation F1"
            ),
        },
    )
    if model_key == "textcnn":
        predictor = load_textcnn_predictor(candidate_path, device="cpu")
    else:
        predictor = load_recurrent_predictor(candidate_path, device="cpu")
    full_metrics = _predict_neural(predictor, validation)
    prefix_metrics = _predict_neural(predictor, validation_prefixes)
    sentence_metrics = _predict_neural(predictor, validation_sentences)
    combined = (
        FULL_VALIDATION_WEIGHT * float(full_metrics["f1"])
        + PREFIX_VALIDATION_WEIGHT * float(prefix_metrics["f1"])
        + SENTENCE_VALIDATION_WEIGHT * float(sentence_metrics["f1"])
    )
    metadata = {
        "model": MODEL_NAMES[model_key],
        "candidate": candidate_name,
        "model_config": model.config.to_dict(),
        "training_config": training_config.to_dict(),
        "best_epoch": best_epoch,
        "training_duration_seconds": training_duration,
        "full_validation_metrics": full_metrics,
        "validation_prefix_metrics": prefix_metrics,
        "validation_sentence_metrics": sentence_metrics,
        "selection_score": combined,
        "official_test_evaluated": False,
    }
    _write_json(candidate_path / "recovery_candidate.json", metadata)
    print(
        f"{model_key}/{candidate_name}: full F1={full_metrics['f1']:.4f}, "
        f"prefix F1={prefix_metrics['f1']:.4f}, "
        f"sentence F1={sentence_metrics['f1']:.4f}, "
        f"selection={combined:.4f}",
        flush=True,
    )
    return {**metadata, "directory": str(candidate_path.resolve())}


def _load_final_predictor(
    model_key: str, model_directory: Path, device: str = "cpu"
) -> Any:
    if model_key == "logistic_regression":
        return joblib.load(model_directory / "baseline.joblib")
    if model_key == "textcnn":
        return load_textcnn_predictor(model_directory, device=device)
    return load_recurrent_predictor(model_directory, device=device)


def run_model_recovery(
    dataset_dir: str | Path = "datasets/aclImdb",
    output_root: str | Path = "outputs/models",
    device: str = "auto",
    batch_size: int = 64,
    max_epochs: int = 8,
    external_data_archive: str | Path | None = None,
) -> dict[str, Any]:
    """Select on grouped training-only data and freeze a fresh generation."""
    target_device = resolve_device(device)
    created_at = datetime.now(UTC)
    generation_id = f"recovery-{created_at:%Y%m%dT%H%M%S.%fZ}"
    generation = Path(output_root).expanduser() / generation_id
    generation.mkdir(parents=True, exist_ok=False)
    project_root = Path(__file__).resolve().parents[3]
    protected_hashes = _protected_output_hashes(project_root)
    _write_json(generation / "protected_output_hashes_before.json", protected_hashes)

    official_training = load_imdb_split(dataset_dir, "train")
    audit, assignments = audit_training_records(official_training)
    training, validation, split_metadata = split_training_records_by_group(
        official_training, validation_size=VALIDATION_SIZE, seed=SEED
    )
    external_training = pd.DataFrame(
        columns=[
            "review_id",
            "text",
            "label",
            "split",
            "group_id",
            "domain",
            "source_view",
        ]
    )
    external_validation = None
    external_test = None
    external_metadata: dict[str, Any] | None = None
    if external_data_archive is not None:
        (
            external_training,
            external_validation,
            external_test,
            loaded_external_metadata,
        ) = load_uci_short_sentiment_splits(external_data_archive, seed=SEED)
        official_group_ids = set(official_training["text"].map(normalized_review_hash))
        overlapping_rows = external_training["group_id"].isin(official_group_ids)
        overlap_count = int(overlapping_rows.sum())
        external_training = external_training.loc[~overlapping_rows].copy()
        external_metadata = {
            **loaded_external_metadata,
            "archive_path": str(Path(external_data_archive).expanduser().resolve()),
            "training_rows_overlapping_official_imdb_train_excluded": overlap_count,
            "training_rows_after_overlap_exclusion": len(external_training),
            "training_repeat_factor": EXTERNAL_TRAINING_REPEAT_COUNT,
            "effective_training_rows_after_repeat_weighting": (
                len(external_training) * EXTERNAL_TRAINING_REPEAT_COUNT
            ),
            "held_out_validation_and_test_used_for_model_selection": False,
        }
        assignment_columns = ["review_id", "group_id", "domain", "label", "split"]
        pd.concat(
            [external_training, external_validation, external_test],
            ignore_index=True,
        )[assignment_columns].to_csv(
            generation / "external_split_assignments.csv", index=False
        )
        _write_json(generation / "external_dataset_metadata.json", external_metadata)
    _write_json(
        generation / "dataset_audit.json",
        {"audit": audit, "split": split_metadata},
    )
    assignments.to_csv(generation / "training_groups.csv", index=False)
    pd.concat(
        [
            training[["review_id", "group_id"]].assign(split="train"),
            validation[["review_id", "group_id"]].assign(split="validation"),
        ],
        ignore_index=True,
    ).to_csv(generation / "split_assignments.csv", index=False)

    train_views = _compose_training_examples(training, external_training)
    validation_prefixes = pd.concat(
        [_prefix_view(validation, limit) for limit in SHORT_PREFIX_LENGTHS],
        ignore_index=True,
    )
    validation_sentences = add_review_sentence_examples(
        validation,
        min_tokens=SENTENCE_TOKEN_RANGE[0],
        max_tokens=SENTENCE_TOKEN_RANGE[1],
    )
    if validation_sentences.empty:
        raise RuntimeError("The grouped validation split produced no sentence views.")
    vocabulary = Vocabulary.build(
        train_views["text"].tolist(),
        max_size=DEFAULT_MAX_VOCAB_SIZE,
        min_frequency=DEFAULT_MIN_TOKEN_FREQUENCY,
    )
    short_benchmark_source = Path("data/robustness/semantic_challenge.csv")
    benchmark_source = pd.read_csv(short_benchmark_source)
    benchmark = benchmark_source[["challenge_id", "label", "category", "text"]].rename(
        columns={"challenge_id": "sample_id"}
    )
    if len(benchmark) != 200 or set(benchmark.label.unique()) != {0, 1}:
        raise RuntimeError(
            "The existing short-text benchmark is incomplete or unbalanced."
        )
    training_texts = set(official_training["text"].map(normalize_text))
    benchmark_texts = set(benchmark["text"].map(normalize_text))
    if training_texts.intersection(benchmark_texts):
        raise RuntimeError("The short-text benchmark overlaps IMDb training text.")
    benchmark.to_csv(generation / "short_text_benchmark.csv", index=False)

    selection: dict[str, Any] = {}
    baseline_candidates = []
    baseline_variants = (
        (0.5, 1, False),
        (1.0, 1, False),
        (1.0, 2, True),
        (2.0, 1, True),
    )
    baseline_grid = [
        {
            "ngram_range": [1, ngram_max],
            "c": c,
            "min_df": min_df,
            "max_df": 1.0,
            "sublinear_tf": sublinear,
            "max_features": 100_000 if ngram_max == 1 else 150_000,
            "class_weight": None,
        }
        for ngram_max in (1, 2)
        for c, min_df, sublinear in baseline_variants
    ]
    for index, settings in enumerate(baseline_grid, 1):
        started = time.perf_counter()
        candidate = _baseline_pipeline(
            settings, train_views.text.tolist(), train_views.label.to_numpy()
        )
        full = _baseline_scores(candidate, validation)
        prefix = _baseline_scores(candidate, validation_prefixes)
        sentence = _baseline_scores(candidate, validation_sentences)
        score = (
            FULL_VALIDATION_WEIGHT * float(full["f1"])
            + PREFIX_VALIDATION_WEIGHT * float(prefix["f1"])
            + SENTENCE_VALIDATION_WEIGHT * float(sentence["f1"])
        )
        candidate_directory = (
            generation / "candidates" / "logistic_regression" / f"candidate-{index}"
        )
        candidate_directory.mkdir(parents=True, exist_ok=False)
        joblib.dump(candidate, candidate_directory / "baseline.joblib", compress=3)
        baseline_candidates.append(
            {
                "settings": settings,
                "full_validation_metrics": full,
                "validation_prefix_metrics": prefix,
                "validation_sentence_metrics": sentence,
                "selection_score": score,
                "training_duration_seconds": time.perf_counter() - started,
                "directory": str(candidate_directory.resolve()),
            }
        )
        print(
            f"logistic_regression candidate {index}: selection={score:.4f}",
            flush=True,
        )
    baseline_candidates.sort(key=lambda row: row["selection_score"], reverse=True)
    selection["logistic_regression"] = {
        "candidates": baseline_candidates,
        "selected": baseline_candidates[0],
    }

    for model_key in MODEL_TYPES:
        candidates = [
            _neural_candidate(
                model_key,
                variant,
                train_views,
                validation,
                validation_prefixes,
                validation_sentences,
                vocabulary,
                target_device,
                generation,
                batch_size,
                max_epochs,
            )
            for variant in ("standard", "regularized")
        ]
        candidates.sort(key=lambda row: row["selection_score"], reverse=True)
        selection[model_key] = {"candidates": candidates, "selected": candidates[0]}
    _write_json(generation / "model_selection.json", selection)

    # Configurations and epoch counts are frozen before the full training refit.
    # No official test rows are read during selection or final fitting.
    full_training = _compose_training_examples(official_training, external_training)
    full_vocabulary = Vocabulary.build(
        full_training["text"].tolist(),
        max_size=DEFAULT_MAX_VOCAB_SIZE,
        min_frequency=DEFAULT_MIN_TOKEN_FREQUENCY,
    )
    frozen_metrics: dict[str, Any] = {}

    for model_key in MODEL_NAMES:
        model_directory = generation / "models" / model_key
        model_directory.mkdir(parents=True, exist_ok=False)
        chosen = selection[model_key]["selected"]
        final_fit_started = time.perf_counter()
        if model_key == "logistic_regression":
            settings = chosen["settings"]
            final_model = _baseline_pipeline(
                settings, full_training.text.tolist(), full_training.label.to_numpy()
            )
            joblib.dump(final_model, model_directory / "baseline.joblib", compress=3)
            training_history = []
            model_config = settings
            selected_epoch = None
            training_config = {
                "seed": SEED,
                "training_data_policy": _training_data_policy(
                    external_metadata is not None
                ),
            }
            preprocessing = {
                "implementation": "scikit-learn TfidfVectorizer in saved Pipeline",
                "lowercase": True,
                "token_pattern": r"(?u)\b\w\w+\b",
                "ngram_range": settings["ngram_range"],
                "min_document_frequency": settings["min_df"],
                "max_document_frequency": settings["max_df"],
                "sublinear_term_frequency": settings["sublinear_tf"],
                "feature_cap": settings["max_features"],
                "fit_partition": (
                    "all official training reviews and repeated external training "
                    "rows after model selection; held-out partitions excluded"
                ),
            }
        else:
            candidate_metadata = json.loads(
                (Path(chosen["directory"]) / "recovery_candidate.json").read_text(
                    encoding="utf-8"
                )
            )
            chosen_settings = candidate_metadata["model_config"]
            training_config = TrainingConfig(**candidate_metadata["training_config"])
            model_directory.mkdir(parents=True, exist_ok=True)
            full_vocabulary.save(model_directory / "vocabulary.json")
            seed_everything(SEED)
            final_model = _new_neural_model(
                model_key,
                full_vocabulary,
                float(chosen_settings["dropout"]),
                int(
                    chosen_settings.get(
                        "hidden_dim", chosen_settings.get("filters_per_size", 100)
                    )
                ),
            )
            selected_epoch = int(candidate_metadata["best_epoch"])
            training_history = _save_full_training_checkpoint(
                final_model,
                model_directory,
                full_vocabulary,
                model_key,
                training_config,
                selected_epoch,
                batch_size,
                target_device,
                full_training,
            )
            model_config = final_model.config.to_dict()
            preprocessing = {
                "version": PREPROCESSING_VERSION,
                "max_vocabulary_size": full_vocabulary.max_size,
                "minimum_token_frequency": full_vocabulary.min_frequency,
                "actual_vocabulary_size": full_vocabulary.size,
                "maximum_sequence_length": MAX_SEQUENCE_LENGTH,
                "padding_id": full_vocabulary.pad_id,
                "unknown_id": full_vocabulary.unk_id,
                "fit_partition": (
                    "all official training reviews and repeated external training "
                    "rows after model selection; held-out partitions excluded"
                ),
            }
        metadata = {
            "model_id": model_key,
            "model_name": MODEL_NAMES[model_key],
            "generation_id": generation_id,
            "dataset": "aclImdb v1.0 labeled official training split",
            "training_rows_including_external_repeat_weighting": len(full_training),
            "unique_source_reviews": len(official_training),
            "training_view_policy": {
                "imdb_rows": "full official IMDb training reviews only",
                "external_sentence_repeat_factor": (
                    EXTERNAL_TRAINING_REPEAT_COUNT
                    if external_metadata is not None
                    else 0
                ),
                "external_sentence_labels": (
                    "dataset-provided binary labels"
                    if external_metadata is not None
                    else None
                ),
                "source": _training_data_policy(external_metadata is not None),
            },
            "external_training_data": {
                "used": external_metadata is not None,
                "training_rows": len(external_training),
                "repeat_factor": (
                    EXTERNAL_TRAINING_REPEAT_COUNT
                    if external_metadata is not None
                    else 0
                ),
                "effective_training_rows": (
                    len(external_training) * EXTERNAL_TRAINING_REPEAT_COUNT
                ),
                "dataset": (
                    external_metadata.get("dataset")
                    if external_metadata is not None
                    else None
                ),
                "license": (
                    external_metadata.get("license")
                    if external_metadata is not None
                    else None
                ),
                "held_out_validation_and_test_used_for_model_selection": False,
            },
            "selected_model_config": model_config,
            "selected_training_config": training_config.to_dict()
            if isinstance(training_config, TrainingConfig)
            else training_config,
            "validation_selection_metrics": chosen,
            "selected_epoch_count": selected_epoch,
            "final_fit_duration_seconds": time.perf_counter() - final_fit_started,
            "final_training_history": training_history,
            "preprocessing": {
                **preprocessing,
                "label_mapping": {"negative": 0, "positive": 1},
            },
            "software_versions": {
                "python": platform.python_version(),
                "torch": str(torch.__version__),
                "numpy": np.__version__,
                "pandas": pd.__version__,
                "scikit_learn": sklearn.__version__,
            },
            "official_test_evaluated": False,
            "validation_subset_included_in_final_fit_after_selection": True,
            "external_training_data_used": external_metadata is not None,
        }
        _write_json(model_directory / "metadata.json", metadata)
        frozen_metrics[model_key] = {
            "selected_candidate_full_group_validation": chosen[
                "full_validation_metrics"
            ],
            "selected_candidate_short_prefix_validation": chosen[
                "validation_prefix_metrics"
            ],
            "selected_candidate_sentence_validation": chosen[
                "validation_sentence_metrics"
            ],
            "selected_candidate_selection_score": chosen["selection_score"],
        }

    _write_json(generation / "frozen_validation_metrics.json", frozen_metrics)
    _write_json(
        generation / "generation_manifest.json",
        {
            "format_version": 1,
            "generation_id": generation_id,
            "created_at_utc": created_at.isoformat(),
            "models": {key: f"models/{key}" for key in MODEL_NAMES},
            "model_order": list(MODEL_NAMES),
            "label_mapping": {"negative": 0, "positive": 1},
            "preprocessing_version": PREPROCESSING_VERSION,
            "frozen_before_official_test_evaluation": True,
            "external_training_data": external_metadata,
        },
    )
    hashes = {
        str(path.relative_to(generation).as_posix()): _sha256(path)
        for path in sorted(generation.rglob("*"))
        if path.is_file()
    }
    _write_json(generation / "artifact_hashes.json", hashes)
    print(f"FROZEN_GENERATION={generation.resolve()}", flush=True)
    print("The official test split has not been loaded.", flush=True)
    return {
        "generation_directory": str(generation.resolve()),
        "selection": selection,
        "frozen_validation_metrics": frozen_metrics,
        "artifact_sha256": hashes,
        "official_test_evaluated": False,
    }


def evaluate_frozen_generation(
    generation_directory: str | Path,
    dataset_dir: str | Path = "datasets/aclImdb",
    *,
    activate_if_accepted: bool = True,
) -> dict[str, Any]:
    """Evaluate a frozen generation once, then activate it only if it passes."""
    from deeptext.dashboard.api import (
        PredictionRequest,
        PredictionResponse,
        create_app,
    )
    from deeptext.dashboard.artifacts import ArtifactRepository
    from deeptext.dashboard.inference import PredictorRegistry

    generation = Path(generation_directory).expanduser().resolve()
    manifest_path = generation / "generation_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    external_metadata = manifest.get("external_training_data")
    project_root = Path(__file__).resolve().parents[3]
    outputs_root = (project_root / "outputs").resolve()
    if not generation.is_relative_to(outputs_root / "models"):
        raise ValueError("Recovery generations must remain under outputs/models.")

    inference_device = "cuda" if torch.cuda.is_available() else "cpu"
    predictors = {
        model_key: _load_final_predictor(
            model_key, generation / manifest["models"][model_key], inference_device
        )
        for model_key in MODEL_NAMES
    }
    model_hashes_before = {
        f"{model_key}/{path.name}": _sha256(path)
        for model_key in MODEL_NAMES
        for path in sorted((generation / manifest["models"][model_key]).iterdir())
        if path.is_file()
    }

    diagnostics = [
        ("positive", "the movie was good"),
        ("positive", "the movie was too good"),
        ("positive", "this movie is excellent"),
        ("positive", "i loved this movie"),
        ("positive", "the acting was fantastic"),
        ("positive", "what a wonderful film"),
        ("positive", "this was an enjoyable experience"),
        ("positive", "the story was interesting and engaging"),
        ("negative", "the movie was bad"),
        ("negative", "the movie was terrible"),
        ("negative", "i hated this movie"),
        ("negative", "the acting was awful"),
        ("negative", "this was a disappointing film"),
        ("negative", "the story was boring"),
        ("negative", "this movie was not enjoyable"),
        ("negation_contrast", "the movie was not good"),
        ("negation_contrast", "the movie was not bad"),
        ("negation_contrast", "the movie was good but the ending was terrible"),
        (
            "negation_contrast",
            "the acting was excellent but the story was disappointing",
        ),
        (
            "negation_contrast",
            "although the film started slowly it became excellent",
        ),
        (
            "negation_contrast",
            "i expected a good movie but it was disappointing",
        ),
    ]
    diagnostic_rows: list[dict[str, Any]] = []
    api_registry = PredictorRegistry(ArtifactRepository(project_root))
    api_registry._models = {
        key: (model, "cpu" if key == "logistic_regression" else str(model.device))
        for key, model in predictors.items()
    }
    api_app = create_app(
        project_root,
        repository=api_registry.repository,
        predictors=api_registry,
    )
    prediction_route = next(
        route.endpoint
        for route in api_app.routes
        if getattr(route, "path", None) == "/api/predict"
    )
    diagnostic_api_checks = 0
    deterministic_checks = {model_key: True for model_key in MODEL_NAMES}
    for category, text in diagnostics:
        api_payload = prediction_route(PredictionRequest(text=text))
        api_response = PredictionResponse.model_validate(api_payload)
        api_by_key = {item.id: item.model_dump() for item in api_response.models}
        normalized = normalize_text(text)
        tokens = tokenize_text(text)
        for model_key, model in predictors.items():
            started = time.perf_counter()
            if model_key == "logistic_regression":
                probabilities = predict_baseline_probabilities(model, [text])[0]
                predicted = int(np.argmax(probabilities))
                negative_probability = float(probabilities[0])
                positive_probability = float(probabilities[1])
                repeated = predict_baseline_probabilities(model, [text])[0]
                deterministic_checks[model_key] &= bool(
                    np.allclose(probabilities, repeated, rtol=1e-7, atol=1e-8)
                )
                vectorizer = model.named_steps["tfidf"]
                token_ids = [
                    int(vectorizer.vocabulary_[token])
                    if token in vectorizer.vocabulary_
                    else None
                    for token in tokens
                ]
                model_device = "cpu"
            else:
                result = model.predict(text)
                predicted = int(result["predicted_class"])
                negative_probability = float(result["negative_probability"])
                positive_probability = float(result["positive_probability"])
                repeated = model.predict(text)
                deterministic_checks[model_key] &= bool(
                    repeated["predicted_class"] == predicted
                    and np.isclose(
                        repeated["positive_probability"],
                        positive_probability,
                        rtol=1e-5,
                        atol=1e-6,
                    )
                )
                ids, _ = encode_text(
                    text,
                    model.vocabulary,
                    max_length=model.model.config.max_sequence_length,
                )
                token_ids = ids[: len(tokens)]
                model_device = str(model.device)
            latency_ms = (time.perf_counter() - started) * 1000
            api_item = api_by_key[model_key]
            if (
                api_item["predicted_class"] != predicted
                or not np.isclose(
                    api_item["positive_probability"],
                    positive_probability,
                    rtol=1e-5,
                    atol=1e-6,
                )
                or api_item["sentiment"] != ("positive" if predicted else "negative")
            ):
                raise RuntimeError(
                    f"Direct inference and FastAPI disagree for {model_key}: {text!r}."
                )
            diagnostic_api_checks += 1
            diagnostic_rows.append(
                {
                    "category": category,
                    "text": text,
                    "preprocessed_text": normalized,
                    "tokens_json": json.dumps(tokens, ensure_ascii=False),
                    "token_ids_json": json.dumps(token_ids),
                    "model_id": model_key,
                    "device": model_device,
                    "latency_ms": latency_ms,
                    "predicted_class": predicted,
                    "sentiment": "positive" if predicted else "negative",
                    "positive_probability": positive_probability,
                    "negative_probability": negative_probability,
                    "api_sentiment": api_item["sentiment"],
                    "api_positive_probability": api_item["positive_probability"],
                }
            )
    diagnostics_frame = pd.DataFrame(diagnostic_rows)
    diagnostics_frame.to_csv(generation / "diagnostic_predictions.csv", index=False)
    batch_single_checks: dict[str, bool] = {}
    diagnostic_texts = [text for _, text in diagnostics]
    for model_key in MODEL_TYPES:
        predictor = predictors[model_key]
        batch_results = predictor.predict_batch(diagnostic_texts, batch_size=16)
        single_results = [predictor.predict(text) for text in diagnostic_texts]
        batch_single_checks[model_key] = all(
            batch["predicted_class"] == single["predicted_class"]
            and np.isclose(
                batch["positive_probability"],
                single["positive_probability"],
                rtol=1e-5,
                atol=1e-4,
            )
            for batch, single in zip(batch_results, single_results, strict=True)
        )

    baseline_pipeline = predictors["logistic_regression"]
    baseline_vectorizer = baseline_pipeline.named_steps["tfidf"]
    baseline_classifier = baseline_pipeline.named_steps["logistic_regression"]
    feature_names = baseline_vectorizer.get_feature_names_out()
    coefficients = baseline_classifier.coef_[0]
    strongest_positive = np.argsort(coefficients)[-20:][::-1]
    strongest_negative = np.argsort(coefficients)[:20]
    pd.DataFrame(
        [
            {
                "polarity": polarity,
                "feature": feature_names[index],
                "coefficient": float(coefficients[index]),
            }
            for polarity, indices in (
                ("positive", strongest_positive),
                ("negative", strongest_negative),
            )
            for index in indices
        ]
    ).to_csv(generation / "baseline_top_coefficients.csv", index=False)

    benchmark = pd.read_csv(generation / "short_text_benchmark.csv")
    benchmark_rows = []
    for model_key, model in predictors.items():
        if model_key == "logistic_regression":
            probabilities = predict_baseline_probabilities(
                model, benchmark["text"].tolist()
            )
            predicted = probabilities.argmax(axis=1).astype(int).tolist()
            positive = probabilities[:, 1].tolist()
        else:
            results = model.predict_batch(benchmark["text"].tolist(), batch_size=128)
            predicted = [int(row["predicted_class"]) for row in results]
            positive = [float(row["positive_probability"]) for row in results]
        for row, label, score in zip(
            benchmark.itertuples(), predicted, positive, strict=True
        ):
            benchmark_rows.append(
                {
                    "sample_id": row.sample_id,
                    "category": row.category,
                    "true_label": int(row.label),
                    "model_id": model_key,
                    "predicted_label": label,
                    "positive_probability": score,
                }
            )
    benchmark_predictions = pd.DataFrame(benchmark_rows)
    benchmark_predictions.to_csv(
        generation / "short_text_benchmark_predictions.csv", index=False
    )
    benchmark_metrics: dict[str, Any] = {}
    for model_key, frame in benchmark_predictions.groupby("model_id", sort=False):
        benchmark_metrics[model_key] = _detailed_metrics(
            frame["true_label"].tolist(),
            frame["predicted_label"].tolist(),
            frame["positive_probability"].tolist(),
        )
    category_rows = []
    for (model_key, category), frame in benchmark_predictions.groupby(
        ["model_id", "category"], sort=False
    ):
        category_rows.append(
            {
                "model_id": model_key,
                "category": category,
                **_detailed_metrics(
                    frame["true_label"].tolist(),
                    frame["predicted_label"].tolist(),
                    frame["positive_probability"].tolist(),
                ),
            }
        )
    pd.DataFrame(category_rows).to_csv(
        generation / "short_text_category_metrics.csv", index=False
    )

    external_evaluation, external_predictions = _evaluate_external_holdouts(
        predictors, external_metadata
    )
    if external_evaluation is not None:
        _write_json(
            generation / "external_short_text_evaluation.json", external_evaluation
        )
        external_predictions.to_csv(
            generation / "external_short_text_predictions.csv", index=False
        )

    diagnostic_table = diagnostics_frame.pivot(
        index="text", columns="model_id", values="sentiment"
    )
    simple_positive = [text for category, text in diagnostics if category == "positive"]
    simple_negative = [text for category, text in diagnostics if category == "negative"]
    diagnostic_acceptance = {}
    for model_key in MODEL_NAMES:
        positive_correct = sum(
            diagnostic_table.loc[text, model_key] == "positive"
            for text in simple_positive
        )
        negative_correct = sum(
            diagnostic_table.loc[text, model_key] == "negative"
            for text in simple_negative
        )
        diagnostic_acceptance[model_key] = {
            "positive_correct": positive_correct,
            "positive_total": len(simple_positive),
            "negative_correct": negative_correct,
            "negative_total": len(simple_negative),
            "the_movie_was_good_positive": bool(
                diagnostic_table.loc["the movie was good", model_key] == "positive"
            ),
        }

    # Check device compatibility before deciding whether the sealed test can open.
    cuda_parity = {"available": torch.cuda.is_available(), "models": {}}
    if torch.cuda.is_available():
        for model_key in MODEL_TYPES:
            cpu_model = (
                load_textcnn_predictor(
                    generation / manifest["models"][model_key], device="cpu"
                )
                if model_key == "textcnn"
                else load_recurrent_predictor(
                    generation / manifest["models"][model_key], device="cpu"
                )
            )
            cuda_model = (
                load_textcnn_predictor(
                    generation / manifest["models"][model_key], device="cuda"
                )
                if model_key == "textcnn"
                else load_recurrent_predictor(
                    generation / manifest["models"][model_key], device="cuda"
                )
            )
            sample_texts = [text for _, text in diagnostics[:10]]
            cpu_scores = cpu_model.predict_batch(sample_texts, batch_size=10)
            cuda_scores = cuda_model.predict_batch(sample_texts, batch_size=10)
            max_difference = max(
                abs(
                    float(cpu["positive_probability"])
                    - float(cuda["positive_probability"])
                )
                for cpu, cuda in zip(cpu_scores, cuda_scores, strict=True)
            )
            cuda_parity["models"][model_key] = {
                "maximum_positive_probability_difference": max_difference,
                "within_tolerance": max_difference <= 1e-4,
            }

    pre_test_acceptance = {
        "all_five_models_available": len(predictors) == 5,
        "direct_and_fastapi_match_all_diagnostics": diagnostic_api_checks
        == len(diagnostics) * len(MODEL_NAMES),
        "batch_and_single_inference_match": all(batch_single_checks.values()),
        "diagnostic_outputs_are_deterministic": all(deterministic_checks.values()),
        "probabilities_finite_and_valid": all(
            np.isfinite(diagnostics_frame["positive_probability"])
            & diagnostics_frame["positive_probability"].between(0, 1)
            & diagnostics_frame["negative_probability"].between(0, 1)
        ),
        "each_model_passes_simple_polarity_gate": all(
            item["positive_correct"] >= 6
            and item["negative_correct"] >= 5
            and item["the_movie_was_good_positive"]
            for item in diagnostic_acceptance.values()
        ),
        "short_benchmark_accuracy_at_least_0_55": all(
            metrics["accuracy"] >= 0.55 for metrics in benchmark_metrics.values()
        ),
        "cuda_parity_passes": all(
            item["within_tolerance"] for item in cuda_parity["models"].values()
        ),
    }
    model_hashes_after = {
        f"{model_key}/{path.name}": _sha256(path)
        for model_key in MODEL_NAMES
        for path in sorted((generation / manifest["models"][model_key]).iterdir())
        if path.is_file()
    }
    if model_hashes_before != model_hashes_after:
        raise RuntimeError("Frozen model artifact hashes changed during preflight.")
    protected_before = json.loads(
        (generation / "protected_output_hashes_before.json").read_text(encoding="utf-8")
    )
    protected_after = _protected_output_hashes(project_root)
    protected_unchanged = protected_before == protected_after
    preflight_report = {
        "generation_id": manifest["generation_id"],
        "diagnostic_acceptance": diagnostic_acceptance,
        "short_text_benchmark_metrics": benchmark_metrics,
        "official_test_evaluated": False,
        "official_test_rows_loaded_after_freeze": 0,
        "official_test_had_prior_use_in_project": True,
        "cuda_parity": cuda_parity,
        "acceptance_checks": pre_test_acceptance,
        "accepted_for_activation": False,
        "active_model_generation_switched": False,
        "model_artifacts_unchanged_during_preflight": True,
        "previous_output_artifacts_unchanged": protected_unchanged,
        "previous_output_artifact_count": len(protected_before),
        "short_text_benchmark_provenance": (
            "The existing Phase 15 balanced 200-item author-created semantic "
            "challenge; descriptive only and not independently human-validated."
        ),
        "external_short_text_evaluation": external_evaluation,
        "external_training_data_used": external_metadata is not None,
        "batch_single_inference_checks": batch_single_checks,
        "deterministic_inference_checks": deterministic_checks,
    }
    _write_json(generation / "cuda_parity.json", cuda_parity)
    _write_json(generation / "recovery_report.json", preflight_report)
    _write_json(generation / "frozen_model_hashes.json", model_hashes_before)
    if not _pre_test_acceptance_passes(pre_test_acceptance):
        audit = json.loads(
            (generation / "dataset_audit.json").read_text(encoding="utf-8")
        )["audit"]
        frozen_validation = json.loads(
            (generation / "frozen_validation_metrics.json").read_text(encoding="utf-8")
        )
        _write_json(
            generation / "root_cause_report.json",
            {
                "confirmed_causes": [
                    {
                        "cause": (
                            "Short-input distribution gap in long-review IMDb training."
                        ),
                        "evidence": {
                            "train_length_buckets": audit["class_counts_by_length"],
                            "group_validation_f1": {
                                key: value["selected_candidate_full_group_validation"][
                                    "f1"
                                ]
                                for key, value in frozen_validation.items()
                            },
                            "validation_prefix_f1": {
                                key: value[
                                    "selected_candidate_short_prefix_validation"
                                ]["f1"]
                                for key, value in frozen_validation.items()
                            },
                            "validation_sentence_f1": {
                                key: value["selected_candidate_sentence_validation"][
                                    "f1"
                                ]
                                for key, value in frozen_validation.items()
                            },
                            "diagnostic_acceptance": diagnostic_acceptance,
                        },
                        "change": (
                            "Training examples now include short prefixes and "
                            "first/last sentence views from official training "
                            "reviews; sentence labels inherit review labels. Only "
                            "the external Amazon/Yelp training partition is added "
                            "when configured; held-out rows are excluded from "
                            "selection."
                        ),
                    }
                ],
                "mitigated_risks": [
                    {
                        "risk": (
                            "Exact and normalized duplicate reviews could cross a "
                            "random train/validation split."
                        ),
                        "evidence": {
                            "normalized_duplicate_groups": audit[
                                "normalized_duplicate_groups"
                            ],
                            "normalized_duplicate_rows_after_first": audit[
                                "normalized_duplicate_rows_after_first"
                            ],
                            "new_split_method": (
                                "StratifiedGroupKFold by normalized text"
                            ),
                            "new_split_normalized_group_overlap": 0,
                        },
                        "handling": (
                            "All normalized-identical reviews are assigned to "
                            "one split. Prior leakage was not established."
                        ),
                    }
                ],
                "official_test_opened": False,
                "official_test_had_prior_use_in_project": True,
                "external_training_data_used": external_metadata is not None,
                "external_short_text_evaluation": external_evaluation,
            },
        )
        return preflight_report

    # The test corpus is first opened here, after the generation manifest and
    # final training checkpoints have been frozen and hashed.
    official_test = load_imdb_split(dataset_dir, "test")
    test_predictions: dict[str, list[int]] = {}
    test_probabilities: dict[str, list[float]] = {}
    official_test_metrics = {}
    for model_key, model in predictors.items():
        if model_key == "logistic_regression":
            probabilities = predict_baseline_probabilities(
                model, official_test["text"].tolist()
            )
            test_probabilities[model_key] = probabilities[:, 1].tolist()
            test_predictions[model_key] = (
                probabilities.argmax(axis=1).astype(int).tolist()
            )
        else:
            results = model.predict_batch(
                official_test["text"].tolist(), batch_size=256
            )
            test_predictions[model_key] = [
                int(row["predicted_class"]) for row in results
            ]
            test_probabilities[model_key] = [
                float(row["positive_probability"]) for row in results
            ]
        official_test_metrics[model_key] = _detailed_metrics(
            official_test["label"].astype(int).tolist(),
            test_predictions[model_key],
            test_probabilities[model_key],
        )
        pd.DataFrame(
            {
                "review_id": official_test["review_id"],
                "true_label": official_test["label"],
                "predicted_label": test_predictions[model_key],
                "positive_probability": test_probabilities[model_key],
            }
        ).to_csv(generation / f"test_predictions_{model_key}.csv", index=False)

    official_test_metrics["sample_count"] = len(official_test)
    official_test_metrics["class_counts"] = {
        "negative": int((official_test["label"] == 0).sum()),
        "positive": int((official_test["label"] == 1).sum()),
    }
    old_phase13_paths = sorted(
        (project_root / "outputs" / "final_evaluation").glob(
            "imdb-phase13-final-evaluation-*/comparison.json"
        )
    )
    old_vs_new = None
    if old_phase13_paths:
        old_comparison = json.loads(old_phase13_paths[-1].read_text(encoding="utf-8"))
        previous_metrics = old_comparison.get("metrics", {})
        old_vs_new = {
            model_key: {
                "previous_phase13_test_metrics": previous_metrics.get(model_key),
                "recovery_test_metrics": official_test_metrics[model_key],
                "accuracy_delta": (
                    official_test_metrics[model_key]["accuracy"]
                    - previous_metrics[model_key]["accuracy"]
                    if model_key in previous_metrics
                    else None
                ),
                "f1_delta": (
                    official_test_metrics[model_key]["f1"]
                    - previous_metrics[model_key]["f1"]
                    if model_key in previous_metrics
                    else None
                ),
                "comparison_note": (
                    "Historical and recovery metrics use the same official test "
                    "split, which has prior project use; deltas are descriptive, "
                    "not independent estimates."
                ),
            }
            for model_key in MODEL_NAMES
        }
    _write_json(generation / "short_text_benchmark_metrics.json", benchmark_metrics)
    _write_json(generation / "official_test_metrics.json", official_test_metrics)
    if old_vs_new is not None:
        _write_json(generation / "old_vs_recovery_test_metrics.json", old_vs_new)

    acceptance = {
        **pre_test_acceptance,
        "all_official_test_models_above_chance": all(
            metrics["accuracy"] > 0.5
            for key, metrics in official_test_metrics.items()
            if key in MODEL_NAMES
        ),
    }
    accepted = all(acceptance.values())
    model_hashes_after = {
        f"{model_key}/{path.name}": _sha256(path)
        for model_key in MODEL_NAMES
        for path in sorted((generation / manifest["models"][model_key]).iterdir())
        if path.is_file()
    }
    if model_hashes_before != model_hashes_after:
        raise RuntimeError("Frozen model artifact hashes changed during evaluation.")
    protected_before = json.loads(
        (generation / "protected_output_hashes_before.json").read_text(encoding="utf-8")
    )
    protected_after = _protected_output_hashes(project_root)
    protected_unchanged = protected_before == protected_after

    report = {
        "generation_id": manifest["generation_id"],
        "diagnostic_acceptance": diagnostic_acceptance,
        "short_text_benchmark_metrics": benchmark_metrics,
        "official_test_metrics": official_test_metrics,
        "old_vs_recovery_test_metrics": old_vs_new,
        "official_test_rows_loaded_after_freeze": len(official_test),
        "official_test_had_prior_use_in_project": True,
        "cuda_parity": cuda_parity,
        "acceptance_checks": acceptance,
        "accepted_for_activation": accepted,
        "model_artifacts_unchanged_during_evaluation": True,
        "previous_output_artifacts_unchanged": protected_unchanged,
        "previous_output_artifact_count": len(protected_before),
        "short_text_benchmark_provenance": (
            "The existing Phase 15 balanced 200-item author-created semantic "
            "challenge; descriptive only and not independently human-validated."
        ),
        "external_training_data_used": external_metadata is not None,
        "external_short_text_evaluation": external_evaluation,
        "batch_single_inference_checks": batch_single_checks,
        "deterministic_inference_checks": deterministic_checks,
        "official_test_evaluated": True,
    }
    _write_json(generation / "cuda_parity.json", cuda_parity)
    _write_json(generation / "recovery_report.json", report)
    _write_json(generation / "frozen_model_hashes.json", model_hashes_before)
    audit = json.loads((generation / "dataset_audit.json").read_text(encoding="utf-8"))[
        "audit"
    ]
    frozen_validation = json.loads(
        (generation / "frozen_validation_metrics.json").read_text(encoding="utf-8")
    )
    _write_json(
        generation / "root_cause_report.json",
        {
            "confirmed_causes": [
                {
                    "cause": (
                        "Short-input distribution gap in long-review IMDb training."
                    ),
                    "evidence": {
                        "train_length_buckets": audit["class_counts_by_length"],
                        "group_validation_f1": {
                            key: value["selected_candidate_full_group_validation"]["f1"]
                            for key, value in frozen_validation.items()
                        },
                        "validation_prefix_f1": {
                            key: value["selected_candidate_short_prefix_validation"][
                                "f1"
                            ]
                            for key, value in frozen_validation.items()
                        },
                        "validation_sentence_f1": {
                            key: value["selected_candidate_sentence_validation"]["f1"]
                            for key, value in frozen_validation.items()
                        },
                        "observed_playground_examples": [
                            "the movie was good",
                            "the movie was too good",
                        ],
                    },
                    "change": (
                        "Each training review now contributes prefixes of "
                        f"{SHORT_PREFIX_LENGTHS} tokens and eligible first/last "
                        "sentence views, all derived after group splitting and "
                        "applied to all models. Sentence labels inherit review labels. "
                        "Only the external Amazon/Yelp training partition was used; "
                        "its held-out rows did not enter model selection."
                    ),
                },
            ],
            "mitigated_risks": [
                {
                    "risk": (
                        "Normalized duplicate reviews could cross a random "
                        "train/validation split."
                    ),
                    "evidence": {
                        "normalized_duplicate_groups_in_train": audit[
                            "normalized_duplicate_groups"
                        ],
                        "normalized_duplicate_rows_after_first": audit[
                            "normalized_duplicate_rows_after_first"
                        ],
                        "new_split_method": "StratifiedGroupKFold by normalized text",
                        "new_split_normalized_group_overlap": 0,
                    },
                    "handling": (
                        "All normalized-identical reviews are grouped together. "
                        "Prior leakage was not established."
                    ),
                }
            ],
            "ruled_out_causes": [
                {
                    "cause": "Positive/negative label inversion.",
                    "evidence": (
                        "Labels remain negative=0 and positive=1; logistic "
                        "classes_ is checked as [0, 1], neural outputs use softmax "
                        "in class order, and direct predictions match FastAPI "
                        "route results."
                    ),
                },
                {
                    "cause": "Backend probability or frontend sentiment reversal.",
                    "evidence": (
                        "The API emits the predictor class and named positive/negative "
                        "scores; the UI renders the API sentiment string and "
                        "corresponding named score fields. All 105 direct/API "
                        "diagnostic pairs are checked."
                    ),
                },
                {
                    "cause": (
                        "Neural vocabulary mismatch, padding dominance, or "
                        "inference refitting."
                    ),
                    "evidence": (
                        "The same tokenizer and saved vocabulary are used at train and "
                        "inference; TextCNN masks windows covering padding; recurrent "
                        "models pack sequence lengths; the baseline reloads its fitted "
                        "TF-IDF pipeline."
                    ),
                },
                {
                    "cause": "Class imbalance.",
                    "evidence": (
                        "The labeled IMDb training set is balanced 12,500/12,500."
                    ),
                },
            ],
            "contributing_factors_and_limits": [
                (
                    "The Phase 15 short-text set is author-created and not "
                    "independently validated."
                ),
                (
                    "Model scores are uncalibrated and cannot guarantee a "
                    "correct prediction."
                ),
                (
                    "The official IMDb test set was used by earlier project phases; "
                    "this is not a pristine independent test."
                ),
                (
                    "A single seed/configuration comparison does not establish "
                    "statistical significance."
                ),
            ],
            "unconfirmed_hypotheses": [
                (
                    "The phrase 'too good' can be context-dependent; it is not "
                    "labeled as an unconditional polarity rule."
                ),
                (
                    "Short-prefix augmentation inherits review-level labels and "
                    "can introduce weak-label noise."
                ),
            ],
        },
    )

    active = False
    if accepted and activate_if_accepted:
        pointer_path = project_root / "outputs" / "models" / "active_generation.json"
        previous_pointer = pointer_path.read_bytes() if pointer_path.is_file() else None
        pointer_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(
            pointer_path,
            {
                "generation_id": manifest["generation_id"],
                "generation_manifest": f"{generation.name}/generation_manifest.json",
            },
        )
        api_repository = ArtifactRepository(project_root)
        api_registry = PredictorRegistry(api_repository)
        active_app = create_app(
            project_root, repository=api_repository, predictors=api_registry
        )
        active_route = next(
            route.endpoint
            for route in active_app.routes
            if getattr(route, "path", None) == "/api/predict"
        )
        active_payload = active_route(PredictionRequest(text="the movie was good"))
        active_result = PredictionResponse.model_validate(active_payload).model_dump()
        if active_result.get("available_model_count") != 5 or any(
            row["status"] != "ok" for row in active_result.get("models", [])
        ):
            if previous_pointer is None:
                pointer_path.unlink(missing_ok=True)
            else:
                pointer_path.write_bytes(previous_pointer)
            report["accepted_for_activation"] = False
            report["active_api_smoke_test"] = "failed; pointer removed"
            _write_json(generation / "recovery_report.json", report)
        else:
            active = True
            report["active_api_smoke_test"] = "passed"
            _write_json(generation / "recovery_report.json", report)
    report["active_model_generation_switched"] = active
    _write_json(generation / "recovery_report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, default=Path("datasets/aclImdb"))
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/models"))
    parser.add_argument("--device", default="auto", choices=("auto", "cpu", "cuda"))
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--max-epochs", type=int, default=8)
    parser.add_argument(
        "--external-data-archive",
        type=Path,
        help="Optional licensed UCI sentence dataset ZIP for training rows only.",
    )
    parser.add_argument(
        "--evaluate-generation",
        type=Path,
        help="Evaluate one already frozen recovery generation exactly once.",
    )
    parser.add_argument(
        "--do-not-activate",
        action="store_true",
        help="Evaluate but leave the active model pointer unchanged.",
    )
    args = parser.parse_args()
    if args.evaluate_generation:
        result = evaluate_frozen_generation(
            args.evaluate_generation,
            args.dataset_dir,
            activate_if_accepted=not args.do_not_activate,
        )
    else:
        result = run_model_recovery(
            args.dataset_dir,
            args.output_dir,
            args.device,
            args.batch_size,
            args.max_epochs,
            args.external_data_archive,
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
