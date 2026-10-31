"""Focused integrity tests for the Phase 15 robustness evaluation."""

import numpy as np
import pandas as pd
import pytest

from deeptext.experiments.robustness import (
    MODEL_KEYS,
    TRANSFORMATION_TYPES,
    _automatic_summaries,
    _prediction_frame,
    generate_automatic_perturbations,
    validate_semantic_challenge,
)


def test_automatic_perturbations_are_deterministic_and_traceable():
    reviews = pd.DataFrame(
        [
            {
                "review_id": "train/pos/1.txt",
                "label": 1,
                "text": "The excellent movie has a wonderful story and funny actors.",
            },
            {
                "review_id": "train/neg/1.txt",
                "label": 0,
                "text": "The terrible movie has a boring story and wooden actors.",
            },
        ]
    )

    variants, rejected = generate_automatic_perturbations(
        reviews, seed=23, max_variants_per_review=len(TRANSFORMATION_TYPES)
    )
    repeated, repeated_rejected = generate_automatic_perturbations(
        reviews, seed=23, max_variants_per_review=len(TRANSFORMATION_TYPES)
    )

    pd.testing.assert_frame_equal(variants, repeated)
    pd.testing.assert_frame_equal(rejected, repeated_rejected)
    assert variants.sample_id.is_unique
    assert variants.label_preserving_track.all()
    assert set(variants.review_id) == set(reviews.review_id)
    assert set(variants.category) <= {
        "spelling",
        "capitalization",
        "punctuation",
        "whitespace_formatting",
        "word_level",
    }
    assert (variants.transformed_text != variants.original_text).all()
    assert "rejected_reason" in rejected


def test_semantic_challenge_csv_is_balanced_and_covers_all_categories():
    challenge = pd.read_csv("data/robustness/semantic_challenge.csv")

    summary = validate_semantic_challenge(challenge)

    assert summary["sample_count"] == 200
    assert summary["class_distribution"] == {"0": 100, "1": 100}
    assert set(summary["category_distribution"].values()) == {20}
    assert challenge.challenge_id.is_unique
    assert challenge.text.map(str.casefold).is_unique


def test_semantic_challenge_rejects_normalized_duplicates():
    challenge = pd.DataFrame(
        [
            {
                "challenge_id": "SC-1",
                "text": "A very good film.",
                "label": 1,
                "category": "negation",
                "rationale": "Positive example.",
                "phenomenon": "negation",
            },
            {
                "challenge_id": "SC-2",
                "text": "  A VERY good film. ",
                "label": 1,
                "category": "negation",
                "rationale": "Duplicate after normalization.",
                "phenomenon": "negation",
            },
        ]
    )

    with pytest.raises(ValueError, match="normalized duplicates"):
        validate_semantic_challenge(challenge, require_all_categories=False)


def test_prediction_frame_supports_author_created_challenge_records():
    records = pd.DataFrame(
        [
            {
                "challenge_id": "SC-001",
                "label": 1,
                "category": "negation",
                "text": "This is not a bad film.",
            }
        ]
    )
    predictions = {key: (np.array([1]), np.array([0.8])) for key in MODEL_KEYS}

    frame = _prediction_frame(records, predictions)

    assert frame.sample_id.tolist() == ["SC-001"]
    assert frame.review_id.tolist() == ["SC-001"]
    assert frame.true_label.tolist() == [1]
    assert frame.category.tolist() == ["negation"]
    assert frame.logistic_regression_confidence.tolist() == pytest.approx([0.8])


def test_automatic_summary_uses_per_review_units_and_pairs_originals():
    records = []
    for review_id, label in (("r1", 1), ("r2", 0)):
        for variant, prediction in enumerate((label, 1 - label, label)):
            row = {
                "review_id": review_id,
                "true_label": label,
                "category": "spelling",
                "transformation_type": "common_typo",
            }
            for key in MODEL_KEYS:
                row[f"{key}_prediction"] = prediction
                row[f"{key}_positive_probability"] = 0.8 if prediction else 0.2
                row[f"{key}_original_prediction"] = label
                row[f"{key}_original_probability"] = 0.8 if label else 0.2
            records.append(row)

    summary, review_summary, overall = _automatic_summaries(pd.DataFrame(records))

    lr = summary[
        (summary.summary_level == "transformation_type")
        & (summary.group == "common_typo")
        & (summary.model == "logistic_regression")
    ].iloc[0]
    assert lr.unique_review_count == 2
    assert lr.variant_count == 6
    assert lr.original_accuracy_matched_reviews == 1.0
    assert lr.robustness_accuracy_review_level == 1.0
    assert lr.prediction_flip_rate == pytest.approx(1 / 3)
    assert len(review_summary) == 2 * len(MODEL_KEYS)
    assert overall["logistic_regression"]["variant_count"] == 6
