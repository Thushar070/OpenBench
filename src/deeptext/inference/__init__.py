"""Inference interfaces for locally trained models."""

from deeptext.inference.predictor import (
    RecurrentSentimentPredictor,
    TextCNNSentimentPredictor,
    load_recurrent_predictor,
    load_textcnn_predictor,
)

__all__ = [
    "RecurrentSentimentPredictor",
    "TextCNNSentimentPredictor",
    "load_recurrent_predictor",
    "load_textcnn_predictor",
]
