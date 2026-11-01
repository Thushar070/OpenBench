"""Training-only data audit and duplicate-group validation splitting."""

from __future__ import annotations

import hashlib
import re
from collections import Counter

import pandas as pd
from sklearn.model_selection import StratifiedGroupKFold

from deeptext.data.dataset import validate_records
from deeptext.preprocessing.tokenizer import normalize_text, tokenize_text

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


def normalized_review_hash(text: str) -> str:
    """Return a stable group ID for text after shared inference normalization."""
    normalized = normalize_text(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def add_review_sentence_examples(
    records: pd.DataFrame,
    *,
    min_tokens: int = 4,
    max_tokens: int = 32,
) -> pd.DataFrame:
    """Derive first and last short sentence views with their review labels.

    Call this after splitting source reviews into one partition. Sentence views
    inherit review-level labels, so they are weakly labeled examples rather
    than independent sentence annotations. Test-split text is rejected.
    """
    validate_records(records)
    if (
        isinstance(min_tokens, bool)
        or not isinstance(min_tokens, int)
        or min_tokens < 1
    ):
        raise ValueError("min_tokens must be a positive integer.")
    if (
        isinstance(max_tokens, bool)
        or not isinstance(max_tokens, int)
        or max_tokens < min_tokens
    ):
        raise ValueError("max_tokens must be an integer at least min_tokens.")
    if "split" in records:
        splits = set(records["split"].dropna().unique())
        if len(splits) > 1 or not splits.issubset({"train", "validation"}):
            raise ValueError(
                "Review sentence views accept one training or validation split only."
            )

    rows: list[dict[str, object]] = []
    for record in records.to_dict(orient="records"):
        normalized = normalize_text(record["text"])
        candidates = [
            sentence.strip()
            for sentence in _SENTENCE_BOUNDARY.split(normalized)
            if min_tokens <= len(tokenize_text(sentence)) <= max_tokens
        ]
        if not candidates:
            continue

        selected = [("first", candidates[0]), ("last", candidates[-1])]
        seen: set[str] = set()
        for position, sentence in selected:
            if sentence in seen:
                continue
            seen.add(sentence)
            row = dict(record)
            row["text"] = sentence
            row["source_view"] = f"{position}_sentence"
            if "review_id" in row:
                row["review_id"] = f"{row['review_id']}::sentence-{position}"
            rows.append(row)

    columns = [*records.columns, "source_view"]
    return pd.DataFrame(rows, columns=columns)


def audit_training_records(
    records: pd.DataFrame,
) -> tuple[dict[str, object], pd.DataFrame]:
    """Summarize labeled training data without reading the official test split."""
    validate_records(records)
    if "split" in records and not records["split"].eq("train").all():
        raise ValueError("Only official IMDb training records may be audited here.")

    normalized = records["text"].map(normalize_text)
    group_ids = normalized.map(
        lambda text: hashlib.sha256(text.encode("utf-8")).hexdigest()
    )
    audit_rows = records[["review_id", "label"]].copy()
    audit_rows["group_id"] = group_ids

    groups = audit_rows.groupby("group_id", sort=False).agg(
        row_count=("review_id", "size"), label_count=("label", "nunique")
    )
    repeated = groups[groups["row_count"] > 1]
    lengths = normalized.str.split().str.len()
    length_buckets = pd.cut(
        lengths,
        bins=[-1, 10, 50, 200, float("inf")],
        labels=["0-10", "11-50", "51-200", "201+"],
    )
    length_distribution: dict[str, dict[str, int]] = {}
    for bucket in length_buckets.cat.categories:
        selected = records.loc[length_buckets == bucket, "label"]
        counts = Counter(int(value) for value in selected)
        length_distribution[str(bucket)] = {
            "negative": counts.get(0, 0),
            "positive": counts.get(1, 0),
        }

    report: dict[str, object] = {
        "split_read": "official labeled training partition only",
        "rows": len(records),
        "class_counts": {
            "negative": int((records["label"] == 0).sum()),
            "positive": int((records["label"] == 1).sum()),
        },
        "missing_or_empty_reviews": int(
            records["text"].map(lambda text: not text.strip()).sum()
        ),
        "invalid_label_count": int((~records["label"].isin([0, 1])).sum()),
        "exact_duplicate_rows_after_first": int(
            records["text"].duplicated(keep="first").sum()
        ),
        "normalized_duplicate_rows_after_first": int(
            normalized.duplicated(keep="first").sum()
        ),
        "normalized_duplicate_groups": int(len(repeated)),
        "rows_in_normalized_duplicate_groups": int(repeated["row_count"].sum()),
        "conflicting_label_groups": int((groups["label_count"] > 1).sum()),
        "reviews_with_html_markup": int(
            records["text"].str.contains(r"<[^>]+>", regex=True).sum()
        ),
        "replacement_character_occurrences": int(
            records["text"].map(lambda text: text.count("\ufffd")).sum()
        ),
        "whitespace_token_length_quantiles": {
            str(key): float(value)
            for key, value in lengths.quantile([0, 0.1, 0.5, 0.9, 0.99, 1]).items()
        },
        "class_counts_by_length": length_distribution,
        "official_test_review_texts_loaded": False,
    }
    return report, audit_rows


def split_training_records_by_group(
    records: pd.DataFrame,
    validation_size: float = 0.2,
    seed: int = 42,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object]]:
    """Make deterministic stratified train/validation folds without text leakage."""
    validate_records(records)
    if "split" in records and not records["split"].eq("train").all():
        raise ValueError("Only official training records may be split.")
    if not 0 < validation_size < 1:
        raise ValueError("validation_size must be between 0 and 1.")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer.")
    if "review_id" in records and records["review_id"].duplicated().any():
        raise ValueError("Training review IDs must be unique before splitting.")

    group_ids = records["text"].map(normalized_review_hash)
    group_count = int(group_ids.nunique())
    split_count = round(1 / validation_size)
    if split_count < 2 or group_count < split_count:
        raise ValueError("Not enough normalized review groups for this split size.")

    splitter = StratifiedGroupKFold(
        n_splits=split_count, shuffle=True, random_state=seed
    )
    folds = splitter.split(records, records["label"], groups=group_ids)
    train_indices, validation_indices = next(folds)
    training = records.iloc[train_indices].copy().reset_index(drop=True)
    validation = records.iloc[validation_indices].copy().reset_index(drop=True)
    training["split"] = "train"
    validation["split"] = "validation"
    training["group_id"] = group_ids.iloc[train_indices].reset_index(drop=True)
    validation["group_id"] = group_ids.iloc[validation_indices].reset_index(drop=True)

    overlap = set(training["group_id"]) & set(validation["group_id"])
    if overlap:
        raise RuntimeError("Normalized duplicate groups crossed the split boundary.")

    def counts(frame: pd.DataFrame) -> dict[str, int]:
        labels = Counter(int(value) for value in frame["label"])
        return {"negative": labels.get(0, 0), "positive": labels.get(1, 0)}

    metadata: dict[str, object] = {
        "source": "official labeled IMDb training partition only",
        "method": "StratifiedGroupKFold over SHA256(normalize_text(review))",
        "seed": seed,
        "requested_validation_fraction": validation_size,
        "actual_validation_fraction": len(validation) / len(records),
        "training_rows": len(training),
        "validation_rows": len(validation),
        "unique_normalized_groups": group_count,
        "training_unique_groups": int(training["group_id"].nunique()),
        "validation_unique_groups": int(validation["group_id"].nunique()),
        "training_class_counts": counts(training),
        "validation_class_counts": counts(validation),
        "normalized_group_overlap": len(overlap),
    }
    return training, validation, metadata
