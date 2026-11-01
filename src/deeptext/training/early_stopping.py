"""Validation-F1 early stopping with a separately tracked best epoch."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class EarlyStoppingUpdate:
    """Outcome of recording one epoch's validation F1."""

    is_best: bool
    improved_enough: bool
    should_stop: bool
    bad_epochs: int


class EarlyStopping:
    """Stop after a patience window without a ``min_delta`` score improvement.

    ``best_score`` and ``best_epoch`` track every strict F1 improvement, so
    checkpoint selection is independent from the threshold used for patience.
    """

    def __init__(self, patience: int = 3, min_delta: float = 0.001) -> None:
        if isinstance(patience, bool) or not isinstance(patience, int) or patience < 1:
            raise ValueError("patience must be a positive integer.")
        if (
            isinstance(min_delta, bool)
            or not isinstance(min_delta, (int, float))
            or not math.isfinite(min_delta)
            or min_delta < 0
        ):
            raise ValueError("min_delta must be a finite non-negative number.")
        self.patience = patience
        self.min_delta = min_delta
        self.best_score: float | None = None
        self.best_epoch: int | None = None
        self.bad_epochs = 0
        self.stopping_reason: str | None = None
        self._patience_score: float | None = None

    def update(self, score: float, epoch: int) -> EarlyStoppingUpdate:
        """Record a validation score and report best, significant gain, and stop."""
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
            or not 0 <= score <= 1
        ):
            raise ValueError("validation score must be finite and between 0 and 1.")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            raise ValueError("epoch must be a positive integer.")

        is_best = self.best_score is None or score > self.best_score
        if is_best:
            self.best_score = score
            self.best_epoch = epoch

        improved_enough = (
            self._patience_score is None
            or score > self._patience_score + self.min_delta
        )
        if improved_enough:
            self._patience_score = score
            self.bad_epochs = 0
        else:
            self.bad_epochs += 1

        should_stop = self.bad_epochs >= self.patience
        if should_stop:
            self.stopping_reason = (
                f"Validation score did not improve by more than {self.min_delta:g} "
                f"for {self.patience} consecutive epoch(s)."
            )
        return EarlyStoppingUpdate(
            is_best, improved_enough, should_stop, self.bad_epochs
        )
