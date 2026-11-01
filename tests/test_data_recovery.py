"""Tests for the duplicate-aware recovery data workflow."""

import pandas as pd
import pytest

from deeptext.data.recovery import (
    add_review_sentence_examples,
    audit_training_records,
    split_training_records_by_group,
)


def _records() -> pd.DataFrame:
    rows = [
        {"review_id": f"review-{index}", "text": f"sample {index}", "label": index % 2}
        for index in range(20)
    ]
    rows.extend(
        [
            {"review_id": "duplicate-a", "text": "Good <b>movie</b>", "label": 1},
            {"review_id": "duplicate-b", "text": "good movie", "label": 1},
        ]
    )
    return pd.DataFrame(rows)


def test_group_split_is_reproducible_balanced_and_leak_free() -> None:
    records = _records()
    first = split_training_records_by_group(records, validation_size=0.2, seed=17)
    second = split_training_records_by_group(records, validation_size=0.2, seed=17)
    training, validation, metadata = first

    assert training.review_id.tolist() == second[0].review_id.tolist()
    assert validation.review_id.tolist() == second[1].review_id.tolist()
    assert training.group_id.is_unique
    assert set(training.group_id).isdisjoint(validation.group_id)
    assert metadata["normalized_group_overlap"] == 0
    assert len(training) + len(validation) == len(records)
    assert set(training.label) == {0, 1}
    assert set(validation.label) == {0, 1}


def test_audit_counts_normalized_duplicates_and_label_conflicts() -> None:
    records = _records()
    records.loc[records.review_id == "duplicate-b", "label"] = 0

    report, assignments = audit_training_records(records)

    assert report["normalized_duplicate_rows_after_first"] == 1
    assert report["normalized_duplicate_groups"] == 1
    assert report["conflicting_label_groups"] == 1
    duplicate_groups = assignments.loc[
        assignments.review_id.str.startswith("duplicate-"), "group_id"
    ]
    assert duplicate_groups.nunique() == 1


def test_recovery_split_rejects_nontraining_rows() -> None:
    records = _records()
    records["split"] = "test"

    with pytest.raises(ValueError, match="Only official training"):
        split_training_records_by_group(records)


def test_review_sentence_views_keep_source_labels_and_group_ids() -> None:
    records = pd.DataFrame(
        [
            {
                "review_id": "review-a",
                "text": (
                    "The film opened slowly. The acting was excellent throughout. "
                    "I would recommend this movie."
                ),
                "label": 1,
                "split": "train",
                "group_id": "group-a",
            }
        ]
    )

    views = add_review_sentence_examples(records)

    assert views.text.tolist() == [
        "the film opened slowly.",
        "i would recommend this movie.",
    ]
    assert views.label.tolist() == [1, 1]
    assert views.group_id.tolist() == ["group-a", "group-a"]
    assert views.source_view.tolist() == ["first_sentence", "last_sentence"]
    assert views.review_id.tolist() == [
        "review-a::sentence-first",
        "review-a::sentence-last",
    ]


def test_review_sentence_views_accept_validation_but_reject_test_rows() -> None:
    records = pd.DataFrame(
        [
            {
                "review_id": "review-a",
                "text": "The acting was excellent throughout.",
                "label": 1,
                "split": "validation",
            }
        ]
    )

    assert len(add_review_sentence_examples(records)) == 1
    with pytest.raises(ValueError, match="training or validation"):
        add_review_sentence_examples(records.assign(split="test"))
