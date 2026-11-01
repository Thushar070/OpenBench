"""Tests for the recovery generation's model-training data composition."""

import pandas as pd

from deeptext.experiments.recovery import (
    EXTERNAL_TRAINING_REPEAT_COUNT,
    _compose_training_examples,
)


def test_external_short_sentences_are_weighted_without_weak_imdb_views() -> None:
    imdb = pd.DataFrame(
        [
            {"review_id": "imdb-1", "text": "A long review.", "label": 1},
            {"review_id": "imdb-2", "text": "Another review.", "label": 0},
        ]
    )
    external = pd.DataFrame(
        [
            {
                "review_id": "uci-1",
                "text": "This product works well.",
                "label": 1,
                "group_id": "group-1",
                "domain": "amazon",
            },
            {
                "review_id": "uci-2",
                "text": "The food was disappointing.",
                "label": 0,
                "group_id": "group-2",
                "domain": "yelp",
            },
        ]
    )

    training = _compose_training_examples(imdb, external)

    expected_external_rows = len(external) * EXTERNAL_TRAINING_REPEAT_COUNT
    assert len(training) == len(imdb) + expected_external_rows
    assert (
        training.loc[: len(imdb) - 1, "review_id"].tolist() == imdb.review_id.tolist()
    )
    assert training.iloc[: len(imdb)].training_source.eq("imdb_full_review").all()

    external_rows = training.iloc[len(imdb) :]
    assert external_rows.training_source.eq("uci_short_sentence").all()
    assert external_rows.groupby("text").size().to_dict() == {
        "The food was disappointing.": EXTERNAL_TRAINING_REPEAT_COUNT,
        "This product works well.": EXTERNAL_TRAINING_REPEAT_COUNT,
    }
    assert external_rows.review_id.is_unique
    assert external_rows.groupby("text").label.nunique().eq(1).all()


def test_training_composition_uses_full_imdb_reviews_when_external_is_absent() -> None:
    imdb = pd.DataFrame([{"review_id": "imdb-1", "text": "Full review.", "label": 1}])
    external = pd.DataFrame(columns=["review_id", "text", "label"])

    training = _compose_training_examples(imdb, external)

    assert len(training) == 1
    assert training.iloc[0].text == "Full review."
    assert training.iloc[0].training_source == "imdb_full_review"
