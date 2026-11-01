"""Training and validation loops for length-aware binary text classifiers."""

from __future__ import annotations

import math
import random
import time
from dataclasses import asdict, dataclass

import numpy as np
import torch
from torch import Tensor, nn
from torch.utils.data import DataLoader

from deeptext.evaluation.metrics import evaluate_binary_predictions
from deeptext.models.cnn import TextCNN
from deeptext.models.recurrent import RECURRENT_MODELS
from deeptext.training.checkpoint import CheckpointManager
from deeptext.training.early_stopping import EarlyStopping


@dataclass(frozen=True)
class TrainingConfig:
    """Adam, early-stopping, seed, and execution-device settings."""

    max_epochs: int = 10
    learning_rate: float = 0.001
    patience: int = 3
    min_delta: float = 0.001
    seed: int = 42
    device: str = "auto"
    deterministic: bool = True
    max_gradient_norm: float | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_epochs, bool)
            or not isinstance(self.max_epochs, int)
            or self.max_epochs < 1
        ):
            raise ValueError("max_epochs must be a positive integer.")
        if (
            isinstance(self.learning_rate, bool)
            or not isinstance(self.learning_rate, (int, float))
            or not math.isfinite(self.learning_rate)
            or self.learning_rate <= 0
        ):
            raise ValueError("learning_rate must be a finite positive number.")
        if (
            isinstance(self.patience, bool)
            or not isinstance(self.patience, int)
            or self.patience < 1
        ):
            raise ValueError("patience must be a positive integer.")
        if (
            isinstance(self.min_delta, bool)
            or not isinstance(self.min_delta, (int, float))
            or not math.isfinite(self.min_delta)
            or self.min_delta < 0
        ):
            raise ValueError("min_delta must be a finite non-negative number.")
        if (
            isinstance(self.seed, bool)
            or not isinstance(self.seed, int)
            or not 0 <= self.seed < 2**32
        ):
            raise ValueError("seed must be an integer in the range [0, 2**32).")
        if not isinstance(self.device, str):
            raise TypeError("device must be 'auto', 'cpu', or a torch device string.")
        if not isinstance(self.deterministic, bool):
            raise TypeError("deterministic must be a boolean.")
        if self.max_gradient_norm is not None and (
            isinstance(self.max_gradient_norm, bool)
            or not isinstance(self.max_gradient_norm, (int, float))
            or not math.isfinite(self.max_gradient_norm)
            or self.max_gradient_norm <= 0
        ):
            raise ValueError("max_gradient_norm must be positive or None.")

    def to_dict(self) -> dict[str, int | float | str | bool | None]:
        """Return a JSON-friendly training configuration."""
        return asdict(self)


@dataclass
class TrainingResult:
    """History and best-validation information returned after training."""

    history: list[dict[str, int | float]]
    best_epoch: int
    best_validation_f1: float
    best_validation_metrics: dict[str, float]
    stopped_early: bool
    stopping_reason: str
    device: str
    training_duration_seconds: float


def seed_everything(seed: int, deterministic: bool = True) -> None:
    """Seed Python, NumPy, PyTorch CPU/CUDA, and deterministic kernels."""
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**32:
        raise ValueError("seed must be an integer in the range [0, 2**32).")
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    torch.use_deterministic_algorithms(deterministic, warn_only=True)
    if hasattr(torch.backends, "cudnn"):
        torch.backends.cudnn.deterministic = deterministic
        torch.backends.cudnn.benchmark = not deterministic


def resolve_device(device: str = "auto") -> torch.device:
    """Choose CUDA when requested and available, otherwise use CPU for auto."""
    if not isinstance(device, str):
        raise TypeError("device must be a device string.")
    if device == "auto":
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")
    target = torch.device(device)
    if target.type == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested but is not available.")
    return target


def run_epoch(
    model: nn.Module,
    data_loader: DataLoader,
    loss_function: nn.Module,
    device: str | torch.device,
    optimizer: torch.optim.Optimizer | None = None,
    max_gradient_norm: float | None = None,
) -> dict[str, float]:
    """Run one training epoch or one gradient-free evaluation pass."""
    if len(data_loader) == 0:
        raise ValueError("Cannot run an epoch with an empty DataLoader.")
    target_device = torch.device(device)
    training = optimizer is not None
    model.train(training)
    total_loss = 0.0
    sample_count = 0
    true_labels: list[int] = []
    predicted_labels: list[int] = []
    context = torch.enable_grad() if training else torch.no_grad()

    with context:
        for batch in data_loader:
            if not isinstance(batch, dict) or not {
                "input_ids",
                "length",
                "label",
            }.issubset(batch):
                raise ValueError(
                    "Batches must contain input_ids, length, and label tensors."
                )
            input_ids = batch["input_ids"].to(target_device, non_blocking=True)
            lengths = batch["length"].to(target_device, non_blocking=True)
            labels = batch["label"].to(target_device, non_blocking=True)
            if labels.dtype != torch.long:
                raise TypeError("Batch labels must use torch.long class IDs.")
            if labels.ndim != 1 or labels.shape[0] != input_ids.shape[0]:
                raise ValueError("Batch labels must have shape (batch_size,).")

            if training:
                optimizer.zero_grad(set_to_none=True)
            logits = model(input_ids, lengths)
            loss = loss_function(logits, labels)
            if loss.ndim != 0:
                raise ValueError("loss_function must return a scalar loss.")
            if training:
                loss.backward()
                if max_gradient_norm is not None:
                    torch.nn.utils.clip_grad_norm_(
                        model.parameters(), max_gradient_norm
                    )
                optimizer.step()

            batch_size = labels.size(0)
            total_loss += float(loss.detach().item()) * batch_size
            sample_count += batch_size
            true_labels.extend(labels.detach().cpu().tolist())
            predicted_labels.extend(logits.detach().argmax(dim=1).cpu().tolist())

    if sample_count == 0:
        raise ValueError("DataLoader produced no samples.")
    metrics = evaluate_binary_predictions(true_labels, predicted_labels)
    return {
        "loss": total_loss / sample_count,
        "accuracy": float(metrics["accuracy"]),
        "precision": float(metrics["precision"]),
        "recall": float(metrics["recall"]),
        "f1": float(metrics["f1"]),
    }


def _snapshot_state_dict(model: nn.Module) -> dict[str, Tensor]:
    return {
        name: tensor.detach().cpu().clone()
        for name, tensor in model.state_dict().items()
    }


def train_neural_model(
    model: nn.Module,
    training_loader: DataLoader,
    validation_loader: DataLoader,
    config: TrainingConfig | None = None,
    loss_function: nn.Module | None = None,
    checkpoint_manager: CheckpointManager | None = None,
) -> TrainingResult:
    """Train a supported text model with validation-F1 early stopping."""
    supported_models = (TextCNN, *RECURRENT_MODELS.values())
    if not isinstance(model, supported_models):
        supported_names = ", ".join(
            model_type.__name__ for model_type in supported_models
        )
        raise TypeError(f"model must be one of: {supported_names}.")
    for name, loader in (
        ("training_loader", training_loader),
        ("validation_loader", validation_loader),
    ):
        dataset = getattr(loader, "dataset", None)
        if dataset is None or len(dataset) == 0 or len(loader) == 0:
            raise ValueError(f"{name} must contain at least one batch and one sample.")

    settings = config or TrainingConfig()
    target_device = resolve_device(settings.device)
    seed_everything(settings.seed, settings.deterministic)
    model.to(target_device)
    criterion = (loss_function or nn.CrossEntropyLoss()).to(target_device)
    optimizer = torch.optim.Adam(model.parameters(), lr=settings.learning_rate)
    early_stopping = EarlyStopping(settings.patience, settings.min_delta)
    history: list[dict[str, int | float]] = []
    best_model_state: dict[str, Tensor] | None = None
    best_metrics: dict[str, float] | None = None
    started = time.perf_counter()

    for epoch in range(1, settings.max_epochs + 1):
        epoch_started = time.perf_counter()
        training_metrics = run_epoch(
            model,
            training_loader,
            criterion,
            target_device,
            optimizer,
            settings.max_gradient_norm,
        )
        validation_metrics = run_epoch(
            model, validation_loader, criterion, target_device
        )
        epoch_duration = time.perf_counter() - epoch_started
        stopping_update = early_stopping.update(validation_metrics["f1"], epoch)
        history_row: dict[str, int | float] = {"epoch": epoch}
        history_row.update(
            {f"training_{name}": value for name, value in training_metrics.items()}
        )
        history_row.update(
            {f"validation_{name}": value for name, value in validation_metrics.items()}
        )
        history_row["epoch_duration_seconds"] = epoch_duration
        history.append(history_row)

        if stopping_update.is_best:
            best_model_state = _snapshot_state_dict(model)
            best_metrics = validation_metrics.copy()
            if checkpoint_manager is not None:
                saved = checkpoint_manager.save_best(model, epoch, validation_metrics)
                if not saved:
                    raise RuntimeError("Checkpoint manager rejected a new best model.")

        if stopping_update.should_stop:
            break

    if (
        best_model_state is None
        or best_metrics is None
        or early_stopping.best_epoch is None
    ):
        raise RuntimeError("Training completed without a valid best epoch.")
    model.load_state_dict(best_model_state)
    stopped_early = early_stopping.stopping_reason is not None
    stopping_reason = early_stopping.stopping_reason or (
        f"Maximum epoch count ({settings.max_epochs}) reached."
    )
    return TrainingResult(
        history=history,
        best_epoch=early_stopping.best_epoch,
        best_validation_f1=early_stopping.best_score or 0.0,
        best_validation_metrics=best_metrics,
        stopped_early=stopped_early,
        stopping_reason=stopping_reason,
        device=str(target_device),
        training_duration_seconds=time.perf_counter() - started,
    )


def train_textcnn(
    model: TextCNN,
    training_loader: DataLoader,
    validation_loader: DataLoader,
    config: TrainingConfig | None = None,
    loss_function: nn.Module | None = None,
    checkpoint_manager: CheckpointManager | None = None,
) -> TrainingResult:
    """Compatibility entry point for the existing TextCNN experiment."""
    if not isinstance(model, TextCNN):
        raise TypeError("model must be a TextCNN instance.")
    return train_neural_model(
        model,
        training_loader,
        validation_loader,
        config=config,
        loss_function=loss_function,
        checkpoint_manager=checkpoint_manager,
    )
