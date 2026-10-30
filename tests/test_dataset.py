import io
import shutil
import tarfile
from pathlib import Path

import pandas as pd
import pytest

from deeptext.data.dataset import (
    extract_imdb_archive,
    load_imdb_data,
    load_imdb_split,
    split_training_data,
    validate_imdb_dataset,
    validate_records,
)


def _make_dataset(root: Path) -> Path:
    for split in ("train", "test"):
        for sentiment in ("neg", "pos"):
            review_dir = root / split / sentiment
            review_dir.mkdir(parents=True, exist_ok=True)
            for number in range(2):
                text = f"{split} {sentiment} review {number}"
                (review_dir / f"{number}.txt").write_text(text, encoding="utf-8")

    unlabeled_dir = root / "train" / "unsup"
    unlabeled_dir.mkdir()
    (unlabeled_dir / "unlabeled.txt").write_text("unlabeled review", encoding="utf-8")
    return root


def _make_archive(archive_path: Path, members: list[tuple[str, bytes]]) -> Path:
    with tarfile.open(archive_path, mode="w:gz") as archive:
        root_info = tarfile.TarInfo("aclImdb")
        root_info.type = tarfile.DIRTYPE
        archive.addfile(root_info)

        for name, content in members:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return archive_path


def test_load_imdb_data_maps_labels_and_keeps_splits_separate(tmp_path: Path) -> None:
    dataset_dir = _make_dataset(tmp_path / "aclImdb")

    training, test = load_imdb_data(dataset_dir)

    assert set(training["label"]) == {0, 1}
    assert training.groupby("label").size().to_dict() == {0: 2, 1: 2}
    assert set(training["split"]) == {"train"}
    assert set(test["split"]) == {"test"}
    assert not set(training["review_id"]).intersection(test["review_id"])
    assert not training["text"].str.contains("unlabeled").any()


def test_load_imdb_split_rejects_invalid_split_and_missing_dataset(
    tmp_path: Path,
) -> None:
    with pytest.raises(FileNotFoundError, match="does not exist"):
        load_imdb_split(tmp_path / "missing", "train")

    dataset_dir = _make_dataset(tmp_path / "aclImdb")
    with pytest.raises(ValueError, match="either 'train' or 'test'"):
        load_imdb_split(dataset_dir, "validation")  # type: ignore[arg-type]


def test_load_imdb_split_rejects_missing_class_directory(tmp_path: Path) -> None:
    dataset_dir = _make_dataset(tmp_path / "aclImdb")
    shutil.rmtree(dataset_dir / "train" / "pos")

    with pytest.raises(FileNotFoundError, match="Missing sentiment directory"):
        load_imdb_split(dataset_dir, "train")


def test_empty_and_unreadable_reviews_are_reported(tmp_path: Path) -> None:
    dataset_dir = _make_dataset(tmp_path / "aclImdb")
    empty_review = dataset_dir / "train" / "neg" / "0.txt"
    empty_review.write_text("  \n", encoding="utf-8")

    with pytest.raises(ValueError, match="Review file is empty"):
        load_imdb_split(dataset_dir, "train")

    report = validate_imdb_dataset(dataset_dir, require_standard_counts=False)
    assert report["empty_file_count"] == 1
    assert report["valid"] is False

    empty_review.write_bytes(b"\xff")
    report = validate_imdb_dataset(dataset_dir, require_standard_counts=False)
    assert report["unreadable_file_count"] == 1


def test_validate_records_rejects_missing_fields_empty_text_and_invalid_labels() -> (
    None
):
    with pytest.raises(ValueError, match="missing required column"):
        validate_records(pd.DataFrame({"text": ["review"]}))
    with pytest.raises(ValueError, match="empty text"):
        validate_records(pd.DataFrame({"text": ["  "], "label": [0]}))
    with pytest.raises(ValueError, match="binary integers"):
        validate_records(pd.DataFrame({"text": ["review"], "label": [2]}))


def test_training_validation_split_is_deterministic_stratified_and_disjoint() -> None:
    records = pd.DataFrame(
        {
            "text": [f"review {index}" for index in range(20)],
            "label": [0] * 10 + [1] * 10,
            "split": ["train"] * 20,
            "review_id": [f"train/{index}.txt" for index in range(20)],
        }
    )

    training, validation = split_training_data(records, validation_size=0.2, seed=17)
    training_again, validation_again = split_training_data(
        records, validation_size=0.2, seed=17
    )

    assert training["review_id"].is_unique
    assert validation["review_id"].is_unique
    assert not set(training["review_id"]).intersection(validation["review_id"])
    assert training.equals(training_again)
    assert validation.equals(validation_again)
    assert validation.groupby("label").size().to_dict() == {0: 2, 1: 2}
    assert set(training["split"]) == {"train"}
    assert set(validation["split"]) == {"validation"}


def test_split_training_data_rejects_test_split() -> None:
    records = pd.DataFrame({"text": ["review"], "label": [0], "split": ["test"]})

    with pytest.raises(ValueError, match="Only the official training split"):
        split_training_data(records)


def test_validation_reports_counts_and_unlabeled_reviews_separately(
    tmp_path: Path,
) -> None:
    dataset_dir = _make_dataset(tmp_path / "aclImdb")

    report = validate_imdb_dataset(dataset_dir, require_standard_counts=False)

    assert report["valid"] is True
    assert report["counts"] == {
        "train": {"negative": 2, "positive": 2},
        "test": {"negative": 2, "positive": 2},
    }
    assert report["unlabeled_train_count"] == 1


def test_validation_reports_duplicate_labeled_text_without_removing_it(
    tmp_path: Path,
) -> None:
    dataset_dir = _make_dataset(tmp_path / "aclImdb")
    original = dataset_dir / "train" / "neg" / "0.txt"
    duplicate = dataset_dir / "test" / "pos" / "0.txt"
    duplicate.write_text(original.read_text(encoding="utf-8"), encoding="utf-8")

    report = validate_imdb_dataset(dataset_dir, require_standard_counts=False)

    assert report["duplicate_labeled_reviews"] == 1
    assert report["valid"] is True
    assert original.exists()
    assert duplicate.exists()


def test_validation_rejects_missing_dataset_directory(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="does not exist"):
        validate_imdb_dataset(tmp_path / "missing", require_standard_counts=False)


def test_extract_archive_is_safe_and_refuses_an_existing_destination(
    tmp_path: Path,
) -> None:
    archive_path = _make_archive(
        tmp_path / "aclImdb_v1.tar.gz",
        [("aclImdb/train/pos/0.txt", b"positive review")],
    )
    dataset_dir = tmp_path / "aclImdb"

    extracted = extract_imdb_archive(archive_path, dataset_dir)

    assert extracted == dataset_dir
    assert (dataset_dir / "train" / "pos" / "0.txt").read_text(encoding="utf-8") == (
        "positive review"
    )
    with pytest.raises(FileExistsError, match="already exists"):
        extract_imdb_archive(archive_path, dataset_dir)


def test_extract_archive_rejects_path_traversal(tmp_path: Path) -> None:
    archive_path = _make_archive(
        tmp_path / "unsafe.tar.gz",
        [("aclImdb/../outside.txt", b"must not escape")],
    )

    with pytest.raises(ValueError, match="outside the expected aclImdb root"):
        extract_imdb_archive(archive_path, tmp_path / "aclImdb")
    assert not (tmp_path / "outside.txt").exists()
