"""Tests for the licensed external short-sentence corpus loader."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from deeptext.data.external_sentiment import load_uci_short_sentiment_splits


def _write_uci_archive(path: Path, *, invalid_label: bool = False) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for domain, filename in (
            ("amazon", "amazon_cells_labelled.txt"),
            ("yelp", "yelp_labelled.txt"),
        ):
            lines = []
            for label in (0, 1):
                for index in range(20):
                    text = (
                        f"{domain} {label} {index} review words"
                        if index != 1
                        else f"{domain} {label} duplicate sentiment sentence"
                    )
                    lines.append(f"{text}\t{label}")
                lines.append(f"{domain} {label} duplicate sentiment sentence\t{label}")
            if invalid_label and domain == "amazon":
                lines[0] = "malformed sentence\tpositive"
            archive.writestr(
                f"sentiment labelled sentences/{filename}", "\n".join(lines)
            )
        archive.writestr(
            "sentiment labelled sentences/imdb_labelled.txt",
            "excluded IMDb source\t1\n",
        )


def test_uci_loader_splits_amazon_and_yelp_by_normalized_group(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "uci-sentiment.zip"
    _write_uci_archive(archive_path)

    training, validation, test, metadata = load_uci_short_sentiment_splits(archive_path)
    repeated = load_uci_short_sentiment_splits(archive_path)

    assert set(training.domain) == {"amazon", "yelp"}
    assert set(training.label) == {0, 1}
    assert set(validation.label) == {0, 1}
    assert set(test.label) == {0, 1}
    assert all("excluded IMDb source" not in text for text in training.text)
    assert all(
        not (set(left.group_id) & set(right.group_id))
        for left, right in (
            (training, validation),
            (training, test),
            (validation, test),
        )
    )
    assert training.review_id.tolist() == repeated[0].review_id.tolist()
    assert metadata["license"] == "CC BY 4.0"
    assert metadata["imdb_source_file_excluded"] is True
    assert metadata["validation_used_for_model_selection"] is False
    assert metadata["test_used_for_model_selection"] is False


def test_uci_loader_rejects_invalid_labels_and_missing_archives(
    tmp_path: Path,
) -> None:
    invalid_archive = tmp_path / "invalid.zip"
    _write_uci_archive(invalid_archive, invalid_label=True)
    with pytest.raises(ValueError, match="binary label"):
        load_uci_short_sentiment_splits(invalid_archive)

    with pytest.raises(FileNotFoundError, match="archive not found"):
        load_uci_short_sentiment_splits(tmp_path / "missing.zip")
