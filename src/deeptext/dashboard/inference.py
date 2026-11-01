"""Lazy, cached inference over the five frozen OpenBench models."""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

import numpy as np
import torch

from deeptext.dashboard.artifacts import MODEL_KEYS, MODEL_LABELS, ArtifactRepository
from deeptext.inference.predictor import (
    load_recurrent_predictor,
    load_textcnn_predictor,
)
from deeptext.models.baseline import (
    load_baseline_artifacts,
    predict_baseline_probabilities,
)

logger = logging.getLogger(__name__)


class PredictorRegistry:
    """Load only the known local model artifacts and serialize predictions."""

    def __init__(self, repository: ArtifactRepository) -> None:
        self.repository = repository
        self._lock = threading.RLock()
        self._models: dict[str, tuple[Any, str]] = {}

    def refresh(self) -> None:
        with self._lock:
            self._models.clear()

    def _load(self, model_id: str, device: str) -> Any:
        source = self._active_artifact(model_id)
        if model_id == "logistic_regression":
            return load_baseline_artifacts(source)[0]
        if model_id == "textcnn":
            return load_textcnn_predictor(source, device=device)
        return load_recurrent_predictor(source, device=device)

    def _active_artifact(self, model_id: str) -> str:
        pointer = self.repository.outputs / "models" / "active_generation.json"
        if not pointer.is_file():
            _, report = self.repository._phase12()
            source_key = "baseline" if model_id == "logistic_regression" else model_id
            return report["source_artifacts"][source_key]

        pointer_data = json.loads(pointer.read_text(encoding="utf-8"))
        manifest_path = (pointer.parent / pointer_data["generation_manifest"]).resolve()
        models_root = (self.repository.outputs / "models").resolve()
        if not manifest_path.is_relative_to(models_root):
            raise ValueError("Active model manifest must remain within outputs/models.")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        model_paths = manifest.get("models", {})
        if model_id not in model_paths:
            raise ValueError(f"Active model manifest is missing {model_id}.")
        model_directory = (manifest_path.parent / model_paths[model_id]).resolve()
        if not model_directory.is_relative_to(manifest_path.parent.resolve()):
            raise ValueError("Active model path must remain within its generation.")
        if not model_directory.is_dir():
            raise FileNotFoundError(
                f"Active model artifacts are missing: {model_directory}"
            )
        return str(model_directory)

    def _get(self, model_id: str) -> tuple[Any, str]:
        if model_id in self._models:
            return self._models[model_id]
        preferred = (
            "cpu"
            if model_id == "logistic_regression"
            else ("cuda" if torch.cuda.is_available() else "cpu")
        )
        try:
            model = self._load(model_id, preferred)
            loaded = (model, preferred)
        except (RuntimeError, OSError, ValueError, KeyError) as error:
            if preferred != "cuda":
                raise
            logger.warning(
                "CUDA loading failed for %s; retrying on CPU: %s", model_id, error
            )
            torch.cuda.empty_cache()
            loaded = (self._load(model_id, "cpu"), "cpu")
        self._models[model_id] = loaded
        return loaded

    def _run_model(self, model_id: str, text: str) -> tuple[int, list[float], str]:
        model, device = self._get(model_id)
        if model_id == "logistic_regression":
            probabilities = predict_baseline_probabilities(model, [text])[0]
            predicted = int(np.argmax(probabilities))
            scores = [float(probabilities[0]), float(probabilities[1])]
        else:
            prediction = model.predict(text)
            predicted = int(prediction["predicted_class"])
            scores = [
                float(prediction["negative_probability"]),
                float(prediction["positive_probability"]),
            ]
        return predicted, scores, device

    def _run_model_with_cpu_fallback(
        self, model_id: str, text: str
    ) -> tuple[int, list[float], str]:
        try:
            return self._run_model(model_id, text)
        except Exception as error:
            cached = self._models.get(model_id)
            if (
                model_id == "logistic_regression"
                or cached is None
                or cached[1] != "cuda"
            ):
                raise

            logger.warning(
                "CUDA prediction failed for %s; retrying on CPU: %s", model_id, error
            )
            self._models.pop(model_id, None)
            torch.cuda.empty_cache()
            try:
                self._models[model_id] = (self._load(model_id, "cpu"), "cpu")
                return self._run_model(model_id, text)
            except Exception:
                self._models.pop(model_id, None)
                raise

    def predict(self, text: str, model_ids: list[str] | None = None) -> dict[str, Any]:
        selected = MODEL_KEYS if model_ids is None else tuple(model_ids)
        if not selected or len(selected) != len(set(selected)):
            raise ValueError("Select at least one unique model.")
        if any(model_id not in MODEL_KEYS for model_id in selected):
            raise ValueError("Prediction requested an unknown model.")
        selected = tuple(model_id for model_id in MODEL_KEYS if model_id in selected)
        results: list[dict[str, Any]] = []
        with self._lock:
            for model_id in selected:
                started = time.perf_counter()
                try:
                    predicted, scores, device = self._run_model_with_cpu_fallback(
                        model_id, text
                    )
                except Exception:
                    logger.exception("Prediction failed for model %s", model_id)
                    results.append(
                        {
                            "id": model_id,
                            "name": MODEL_LABELS[model_id],
                            "status": "unavailable",
                            "sentiment": None,
                            "predicted_class": None,
                            "negative_probability": None,
                            "positive_probability": None,
                            "latency_ms": (time.perf_counter() - started) * 1000,
                            "device": None,
                        }
                    )
                    continue
                results.append(
                    {
                        "id": model_id,
                        "name": MODEL_LABELS[model_id],
                        "status": "ok",
                        "sentiment": "positive" if predicted else "negative",
                        "predicted_class": predicted,
                        "negative_probability": scores[0],
                        "positive_probability": scores[1],
                        "latency_ms": (time.perf_counter() - started) * 1000,
                        "device": device,
                    }
                )
        available = [row["sentiment"] for row in results if row["status"] == "ok"]
        counts = {label: available.count(label) for label in ("negative", "positive")}
        return {
            "models": results,
            "agreement": len(set(available)) <= 1 and bool(available),
            "available_model_count": len(available),
            "label_distribution": counts,
            "scores_are_calibrated_probabilities": False,
        }
