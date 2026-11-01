"""Local FastAPI service for the OpenBench read-only dashboard."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from deeptext.dashboard.artifacts import MODEL_KEYS, ArtifactRepository
from deeptext.dashboard.inference import PredictorRegistry


class PredictionRequest(BaseModel):
    text: str = Field(min_length=1, max_length=10_000)
    model_ids: (
        list[Literal["logistic_regression", "textcnn", "rnn", "lstm", "gru"]] | None
    ) = None

    @field_validator("text")
    @classmethod
    def text_must_contain_non_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Review text must contain non-whitespace characters.")
        return value

    @field_validator("model_ids")
    @classmethod
    def model_selection_must_be_unique_and_nonempty(
        cls, value: list[str] | None
    ) -> list[str] | None:
        if value is not None and (not value or len(value) != len(set(value))):
            raise ValueError("Select at least one unique model, or omit model_ids.")
        return value


class RefreshResponse(BaseModel):
    refreshed: bool


class ModelPrediction(BaseModel):
    id: str
    name: str
    status: Literal["ok", "unavailable"]
    sentiment: Literal["negative", "positive"] | None
    predicted_class: Literal[0, 1] | None
    negative_probability: float | None = Field(ge=0, le=1)
    positive_probability: float | None = Field(ge=0, le=1)
    latency_ms: float = Field(ge=0)
    device: Literal["cpu", "cuda"] | None


class PredictionResponse(BaseModel):
    models: list[ModelPrediction]
    agreement: bool
    available_model_count: int = Field(ge=0, le=5)
    label_distribution: dict[Literal["negative", "positive"], int]
    scores_are_calibrated_probabilities: Literal[False]


def create_app(
    project_root: str | Path | None = None,
    *,
    repository: ArtifactRepository | None = None,
    predictors: PredictorRegistry | None = None,
) -> FastAPI:
    repo = repository or ArtifactRepository(project_root)
    registry = predictors or PredictorRegistry(repo)
    app = FastAPI(
        title="OpenBench Local API",
        version="0.1.0",
        description=(
            "Read-only benchmark results and local inference for frozen IMDb "
            "sentiment models."
        ),
    )
    app.state.repository = repo
    app.state.predictors = registry
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        model_count = len(repo.models().get("models", []))
        return {
            "status": "ok" if model_count else "degraded",
            "project": "OpenBench",
            "models_available": model_count,
        }

    @app.get("/api/ready")
    async def readiness() -> dict[str, Any]:
        model_data = repo.models()
        models = model_data.get("models", [])
        available = [
            model.get("id") for model in models if model.get("available") is True
        ]
        result = {
            "status": "ready" if len(available) == len(MODEL_KEYS) else "not_ready",
            "models_available": len(available),
            "models_required": len(MODEL_KEYS),
            "active_generation_id": model_data.get("active_generation_id"),
            "available_models": available,
        }
        if result["status"] != "ready":
            raise HTTPException(status_code=503, detail=result)
        return result

    @app.get("/api/models")
    async def models() -> dict[str, Any]:
        return repo.models()

    @app.get("/api/overview")
    async def overview() -> dict[str, Any]:
        return repo.overview()

    @app.get("/api/experiments")
    async def experiments() -> dict[str, Any]:
        return {"experiments": repo.experiments()}

    @app.get("/api/experiments/{run_id}")
    async def experiment_detail(run_id: str) -> dict[str, Any]:
        result = repo.experiment_detail(run_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Experiment not found.")
        return result

    @app.get("/api/comparison")
    async def comparison(
        split: str = Query(
            default="official_test", pattern="^(validation|official_test)$"
        ),
    ) -> dict[str, Any]:
        return repo.comparison(split)

    @app.get("/api/final-evaluation")
    async def final_evaluation() -> dict[str, Any]:
        return repo.final_evaluation()

    @app.get("/api/interpretability")
    async def interpretability() -> dict[str, Any]:
        return repo.interpretability()

    @app.get("/api/robustness")
    async def robustness() -> dict[str, Any]:
        return repo.robustness()

    @app.get("/api/efficiency")
    async def efficiency() -> dict[str, Any]:
        return repo.efficiency()

    @app.get("/api/quality")
    async def quality() -> dict[str, Any]:
        return repo.quality()

    @app.get("/api/efficiency/plots/{filename}")
    async def efficiency_plot(filename: str) -> FileResponse:
        path = repo.efficiency_plot_path(filename)
        if path is None:
            raise HTTPException(status_code=404, detail="Plot not found.")
        return FileResponse(path)

    @app.post("/api/predict", response_model=PredictionResponse)
    def predict(request: PredictionRequest) -> dict[str, Any]:
        return registry.predict(request.text, model_ids=request.model_ids)

    @app.post("/api/refresh", response_model=RefreshResponse)
    async def refresh() -> RefreshResponse:
        repo.refresh()
        registry.refresh()
        return RefreshResponse(refreshed=True)

    return app


app = create_app()
