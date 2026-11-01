"""API and artifact-backed dashboard behavior tests."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from deeptext.dashboard.api import create_app
from deeptext.dashboard.artifacts import ArtifactRepository, _records
from deeptext.dashboard.inference import PredictorRegistry


class FakePredictors:
    def __init__(self) -> None:
        self.requested_models: list[str] | None = None

    def refresh(self) -> None:
        pass

    def predict(self, text: str, model_ids: list[str] | None = None) -> dict[str, Any]:
        assert text == "A useful review"
        self.requested_models = model_ids
        return {
            "models": [
                {
                    "id": f"model-{index}",
                    "name": f"Model {index}",
                    "status": "ok",
                    "sentiment": "positive",
                    "predicted_class": 1,
                    "positive_probability": 0.8,
                    "negative_probability": 0.2,
                    "latency_ms": 1.0,
                    "device": "cpu",
                }
                for index in range(5)
            ],
            "agreement": True,
            "available_model_count": 5,
            "label_distribution": {"negative": 0, "positive": 5},
            "scores_are_calibrated_probabilities": False,
        }


def test_saved_artifact_endpoints_expose_complete_summaries() -> None:
    client = TestClient(
        create_app(repository=ArtifactRepository(), predictors=FakePredictors())
    )
    assert client.get("/api/health").json()["models_available"] == 5
    readiness = client.get("/api/ready")
    assert readiness.status_code == 200
    assert readiness.json()["status"] == "ready"
    assert readiness.json()["models_available"] == 5
    assert len(client.get("/api/models").json()["models"]) == 5
    assert (
        client.get("/api/overview").json()["dataset"]["official_test_review_count"]
        == 25_000
    )
    assert len(client.get("/api/experiments").json()["experiments"]) >= 7
    assert len(client.get("/api/comparison?split=validation").json()["models"]) == 5
    assert len(client.get("/api/comparison?split=official_test").json()["models"]) == 5
    assert client.get("/api/final-evaluation").json()["integrity_verified"] is True
    assert client.get("/api/interpretability").json()["available"] is True
    assert client.get("/api/robustness").json()["available"] is True
    efficiency = client.get("/api/efficiency").json()
    assert efficiency["available"] is True
    efficiency_json = json.dumps(efficiency)
    assert "artifact_directory" not in efficiency_json
    assert "sample_source" not in efficiency_json
    assert "source_model_artifacts" not in efficiency_json
    quality = client.get("/api/quality").json()
    assert quality["separate_tracks"] is True
    assert len(quality["tracks"]) == 10
    quality_json = json.dumps(quality)
    assert "source_artifact_location" not in quality_json
    assert "source_model_artifacts" not in quality_json
    assert client.get("/api/efficiency/plots/quality_accuracy.png").status_code == 200
    assert client.get("/api/efficiency/plots/../../outside.png").status_code in {
        404,
        422,
    }


def test_playground_validates_and_returns_all_five_model_rows() -> None:
    predictors = FakePredictors()
    client = TestClient(
        create_app(repository=ArtifactRepository(), predictors=predictors)
    )
    result = client.post("/api/predict", json={"text": "A useful review"})
    assert result.status_code == 200
    assert len(result.json()["models"]) == 5
    assert result.json()["agreement"] is True
    assert predictors.requested_models is None
    for invalid in ("", " \t\n", "x" * 10_001):
        assert client.post("/api/predict", json={"text": invalid}).status_code == 422
    for invalid in (["bad_model"], [], ["rnn", "rnn"]):
        assert (
            client.post(
                "/api/predict", json={"text": "A useful review", "model_ids": invalid}
            ).status_code
            == 422
        )


def test_playground_accepts_one_selected_model() -> None:
    predictors = FakePredictors()
    client = TestClient(
        create_app(repository=ArtifactRepository(), predictors=predictors)
    )
    response = client.post(
        "/api/predict",
        json={"text": "A useful review", "model_ids": ["textcnn"]},
    )
    assert response.status_code == 200
    assert predictors.requested_models == ["textcnn"]


def test_unknown_experiment_and_invalid_split_are_rejected() -> None:
    client = TestClient(
        create_app(repository=ArtifactRepository(), predictors=FakePredictors())
    )
    assert client.get("/api/experiments/../../outside").status_code in {404, 422}
    assert client.get("/api/experiments/not-a-real-run").status_code == 404
    assert client.get("/api/comparison?split=training").status_code == 422


def test_missing_project_artifacts_return_clear_unavailable_states(tmp_path) -> None:
    repository = ArtifactRepository(tmp_path)
    assert repository.models()["models"] == []
    assert "issue" in repository.models()
    readiness = TestClient(
        create_app(repository=repository, predictors=FakePredictors())
    ).get("/api/ready")
    assert readiness.status_code == 503
    assert readiness.json()["detail"]["status"] == "not_ready"
    assert repository.interpretability()["available"] is False
    assert repository.robustness()["available"] is False
    assert repository.efficiency()["available"] is False


def test_csv_reader_is_bounded(tmp_path) -> None:
    path = tmp_path / "summary.csv"
    path.write_text("item,value\na,1\nb,2\nc,3\n", encoding="utf-8")
    assert len(_records(path, limit=2)) == 2


def test_real_local_models_predict_one_review() -> None:
    """Integration coverage over the installed, frozen project checkpoints."""
    client = TestClient(create_app())
    response = client.post(
        "/api/predict",
        json={
            "text": "The movie was beautifully directed, but the story was "
            "disappointing."
        },
    )
    assert response.status_code == 200
    result = response.json()
    assert len(result["models"]) == 5
    assert result["available_model_count"] == 5
    assert all(model["status"] == "ok" for model in result["models"])
    assert all(0 <= model["positive_probability"] <= 1 for model in result["models"])

    positive_response = client.post("/api/predict", json={"text": "the movie was good"})
    assert positive_response.status_code == 200
    positive_models = positive_response.json()["models"]
    assert len(positive_models) == 5
    assert all(model["sentiment"] == "positive" for model in positive_models)
    single_response = client.post(
        "/api/predict",
        json={"text": "the movie was good", "model_ids": ["logistic_regression"]},
    )
    assert single_response.status_code == 200
    assert len(single_response.json()["models"]) == 1
    assert single_response.json()["models"][0]["id"] == "logistic_regression"


def test_registry_falls_back_to_cpu_when_cuda_loading_fails(
    monkeypatch, caplog
) -> None:
    import deeptext.dashboard.inference as inference

    registry = PredictorRegistry(ArtifactRepository())
    calls: list[str] = []

    def load(_model_id: str, device: str) -> object:
        calls.append(device)
        if device == "cuda":
            raise RuntimeError("simulated CUDA allocation failure")
        return object()

    monkeypatch.setattr(inference.torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(inference.torch.cuda, "empty_cache", lambda: None)
    monkeypatch.setattr(registry, "_load", load)
    model, device = registry._get("textcnn")
    assert model is not None
    assert device == "cpu"
    assert calls == ["cuda", "cpu"]
    assert "CUDA loading failed for textcnn; retrying on CPU" in caplog.text


def test_registry_returns_five_predictions_for_identical_review(monkeypatch) -> None:
    import deeptext.dashboard.inference as inference

    class Neural:
        def predict(self, text: str) -> dict[str, Any]:
            assert text == "same review"
            return {
                "predicted_class": 1,
                "negative_probability": 0.25,
                "positive_probability": 0.75,
            }

    registry = PredictorRegistry(ArtifactRepository())
    registry._models = {
        "logistic_regression": (object(), "cpu"),
        "textcnn": (Neural(), "cpu"),
        "rnn": (Neural(), "cpu"),
        "lstm": (Neural(), "cpu"),
        "gru": (Neural(), "cpu"),
    }
    monkeypatch.setattr(
        inference,
        "predict_baseline_probabilities",
        lambda _model, texts: [[0.25, 0.75] for _ in texts],
    )
    result = registry.predict("same review")
    assert len(result["models"]) == 5
    assert result["available_model_count"] == 5
    assert result["agreement"] is True


def test_registry_retries_cuda_inference_on_cpu(monkeypatch, caplog) -> None:
    import deeptext.dashboard.inference as inference

    class Neural:
        def __init__(self, device: str) -> None:
            self.device = device

        def predict(self, _text: str) -> dict[str, Any]:
            if self.device == "cuda":
                raise RuntimeError("simulated CUDA inference failure")
            return {
                "predicted_class": 1,
                "negative_probability": 0.25,
                "positive_probability": 0.75,
            }

    registry = PredictorRegistry(ArtifactRepository())
    registry._models = {
        "logistic_regression": (object(), "cpu"),
        "textcnn": (Neural("cuda"), "cuda"),
        "rnn": (Neural("cpu"), "cpu"),
        "lstm": (Neural("cpu"), "cpu"),
        "gru": (Neural("cpu"), "cpu"),
    }
    monkeypatch.setattr(
        inference,
        "predict_baseline_probabilities",
        lambda _model, texts: [[0.25, 0.75] for _ in texts],
    )
    monkeypatch.setattr(inference.torch.cuda, "empty_cache", lambda: None)
    monkeypatch.setattr(registry, "_load", lambda _model_id, device: Neural(device))
    result = registry.predict("review")
    cnn = next(row for row in result["models"] if row["id"] == "textcnn")
    assert cnn["status"] == "ok"
    assert cnn["device"] == "cpu"
    assert "CUDA prediction failed for textcnn; retrying on CPU" in caplog.text


def test_registry_reports_model_load_failures(caplog, monkeypatch) -> None:
    import deeptext.dashboard.inference as inference

    class Neural:
        def predict(self, _text: str) -> dict[str, Any]:
            return {
                "predicted_class": 1,
                "negative_probability": 0.25,
                "positive_probability": 0.75,
            }

    registry = PredictorRegistry(ArtifactRepository())
    registry._models["logistic_regression"] = (object(), "cpu")
    monkeypatch.setattr(inference.torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(
        inference,
        "predict_baseline_probabilities",
        lambda _model, _texts: [[0.25, 0.75]],
    )

    def load(model_id: str, _device: str) -> object:
        if model_id == "textcnn":
            raise FileNotFoundError("TextCNN checkpoint is missing.")
        return Neural()

    monkeypatch.setattr(registry, "_load", load)

    result = registry.predict("A useful review")

    missing_model = next(
        model for model in result["models"] if model["id"] == "textcnn"
    )
    assert missing_model["status"] == "unavailable"
    assert missing_model["sentiment"] is None
    assert missing_model["positive_probability"] is None
    assert result["available_model_count"] == 4
    assert any(
        "Prediction failed for model textcnn" in record.getMessage()
        for record in caplog.records
    )


def test_registry_loads_active_generation_manifest(tmp_path) -> None:
    import json

    from deeptext.dashboard.artifacts import MODEL_KEYS

    output_models = tmp_path / "outputs" / "models"
    generation = output_models / "recovery-test"
    generation.mkdir(parents=True)
    model_paths = {}
    for model_id in MODEL_KEYS:
        relative_path = f"models/{model_id}"
        (generation / relative_path).mkdir(parents=True)
        model_paths[model_id] = relative_path
    (generation / "generation_manifest.json").write_text(
        json.dumps({"models": model_paths}), encoding="utf-8"
    )
    (output_models / "active_generation.json").write_text(
        json.dumps({"generation_manifest": "recovery-test/generation_manifest.json"}),
        encoding="utf-8",
    )

    registry = PredictorRegistry(ArtifactRepository(tmp_path))

    assert registry._active_artifact("textcnn") == str(
        (generation / "models" / "textcnn").resolve()
    )


def test_registry_rejects_active_generation_path_escape(tmp_path) -> None:
    import json

    output_models = tmp_path / "outputs" / "models"
    output_models.mkdir(parents=True)
    (output_models / "active_generation.json").write_text(
        json.dumps({"generation_manifest": "../../outside.json"}),
        encoding="utf-8",
    )

    registry = PredictorRegistry(ArtifactRepository(tmp_path))
    with pytest.raises(ValueError, match="within outputs/models"):
        registry._active_artifact("textcnn")
