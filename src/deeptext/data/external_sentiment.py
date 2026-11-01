"""Load and split the licensed UCI short-sentence sentiment corpus."""

from __future__ import annotations

import hashlib
import zipfile
from collections import Counter
from pathlib import Path

import pandas as pd
from sklearn.model_selection import StratifiedGroupKFold

from deeptext.data.dataset import validate_records
from deeptext.data.recovery import normalized_review_hash

_UCI_DIRECTORY = "sentiment labelled sentences/"
_SOURCE_FILES = {
    "amazon": "amazon_cells_labelled.txt",
    "yelp": "yelp_labelled.txt",
}


def _read_source_file(
    archive: zipfile.ZipFile, domain: str, filename: str
) -> list[dict[str, object]]:
    member = f"{_UCI_DIRECTORY}{filename}"
    try:
        content = archive.read(member).decode("utf-8-sig")
    except KeyError as error:
        raise ValueError(f"UCI archive is missing {member!r}.") from error
    except UnicodeError as error:
        raise ValueError(f"UCI source file is not valid UTF-8: {member!r}.") from error

    rows: list[dict[str, object]] = []
    for row_number, line in enumerate(content.splitlines(), start=1):
        if not line.strip():
            continue
        text, separator, raw_label = line.rpartition("\t")
        if not separator or not text.strip() or raw_label not in {"0", "1"}:
            raise ValueError(
                f"Invalid sentence or binary label in {member!r} row {row_number}."
            )
        rows.append(
            {
                "review_id": f"uci331:{domain}:{row_number}",
                "text": text.strip(),
                "label": int(raw_label),
                "domain": domain,
            }
        )
    if not rows:
        raise ValueError(f"UCI source file contains no labeled rows: {member!r}.")
    return rows


def _counts_by_domain_and_label(records: pd.DataFrame) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for domain in sorted(records["domain"].unique()):
        labels = Counter(
            int(label) for label in records.loc[records.domain == domain, "label"]
        )
        counts[domain] = {
            "negative": labels.get(0, 0),
            "positive": labels.get(1, 0),
        }
    return counts


def load_uci_short_sentiment_splits(
    archive_path: str | Path, *, seed: int = 42
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, object]]:
    """Return train/validation/test partitions from UCI's Amazon and Yelp rows.

    The UCI IMDb source file is intentionally excluded because its reviews
    originate from the same IMDb corpus family as OpenBench's sealed test set.
    Splits are deterministic, stratified by domain and label, and grouped by
    normalized sentence text. Only the returned training partition is intended
    for model fitting; the other partitions remain held out.
    """
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer.")
    archive_file = Path(archive_path).expanduser().resolve()
    if not archive_file.is_file():
        raise FileNotFoundError(f"UCI sentiment archive not found: {archive_file}")

    with zipfile.ZipFile(archive_file) as archive:
        corrupt_member = archive.testzip()
        if corrupt_member is not None:
            raise ValueError(
                f"UCI archive member failed CRC validation: {corrupt_member}"
            )
        rows = [
            row
            for domain, filename in _SOURCE_FILES.items()
            for row in _read_source_file(archive, domain, filename)
        ]

    records = pd.DataFrame(rows)
    validate_records(records)
    records["group_id"] = records["text"].map(normalized_review_hash)
    records["stratum"] = records["domain"] + ":" + records["label"].astype(str)

    group_splitter = StratifiedGroupKFold(n_splits=10, shuffle=True, random_state=seed)
    development_indices, test_indices = next(
        group_splitter.split(records, records["stratum"], groups=records["group_id"])
    )
    development = records.iloc[development_indices].reset_index(drop=True)
    test = records.iloc[test_indices].copy().reset_index(drop=True)

    validation_splitter = StratifiedGroupKFold(
        n_splits=9, shuffle=True, random_state=seed + 1
    )
    training_indices, validation_indices = next(
        validation_splitter.split(
            development,
            development["stratum"],
            groups=development["group_id"],
        )
    )
    training = development.iloc[training_indices].copy().reset_index(drop=True)
    validation = development.iloc[validation_indices].copy().reset_index(drop=True)

    group_sets = [set(frame["group_id"]) for frame in (training, validation, test)]
    if (
        group_sets[0] & group_sets[1]
        or group_sets[0] & group_sets[2]
        or group_sets[1] & group_sets[2]
    ):
        raise RuntimeError("Normalized duplicate sentence groups crossed partitions.")

    for name, frame in (
        ("train", training),
        ("validation", validation),
        ("test", test),
    ):
        frame["split"] = name
        frame["source_view"] = "uci_short_sentence"

    group_labels = records.groupby("group_id", sort=False)["label"].nunique()
    duplicate_counts = records["group_id"].value_counts()
    metadata: dict[str, object] = {
        "dataset": "UCI Sentiment Labelled Sentences, dataset 331",
        "source_url": (
            "https://archive.ics.uci.edu/static/public/331/"
            "sentiment%2Blabelled%2Bsentences.zip"
        ),
        "dataset_page": "https://archive.ics.uci.edu/dataset/331/sentiment+labelled+sentencese",
        "license": "CC BY 4.0",
        "citation": (
            "Kotzias, D. (2015). Sentiment Labelled Sentences [Dataset]. "
            "UCI Machine Learning Repository. https://doi.org/10.24432/C57604."
        ),
        "archive_sha256": hashlib.sha256(archive_file.read_bytes()).hexdigest(),
        "source_domains_included": sorted(_SOURCE_FILES),
        "source_file_names": list(_SOURCE_FILES.values()),
        "imdb_source_file_excluded": True,
        "imdb_exclusion_reason": (
            "Avoid potential overlap with the sealed IMDb test source corpus."
        ),
        "source_rows": len(records),
        "normalized_duplicate_groups": int((duplicate_counts > 1).sum()),
        "conflicting_label_groups": int((group_labels > 1).sum()),
        "split_seed": seed,
        "split_method": (
            "Two-stage StratifiedGroupKFold by domain-label stratum and "
            "SHA256(normalize_text(sentence)); approximately 80/10/10."
        ),
        "partition_counts": {
            name: {
                "rows": len(frame),
                "domain_label_counts": _counts_by_domain_and_label(frame),
            }
            for name, frame in (
                ("train", training),
                ("validation", validation),
                ("test", test),
            )
        },
        "validation_used_for_model_selection": False,
        "test_used_for_model_selection": False,
    }
    training = training.drop(columns="stratum")
    validation = validation.drop(columns="stratum")
    test = test.drop(columns="stratum")
    return training, validation, test, metadata
