"""Inference interfaces for locally trained models."""

from deeptext.inference.predictor import (
    TextCNNSentimentPredictor,
    load_textcnn_predictor,
)

__all__ = ["TextCNNSentimentPredictor", "load_textcnn_predictor"]
