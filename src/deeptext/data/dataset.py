"""Load, split, validate, and safely extract the local IMDB review dataset."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Literal

import pandas as pd
from sklearn.model_selection import train_test_split

LABELS = {"neg": 0, "pos": 1}
STANDARD_COUNTS = {
    "train": {"neg": 12_500, "pos": 12_500},
    "test": {"neg": 12_500, "pos": 12_500},
}
STANDARD_UNLABELED_COUNT = 50_000


def _review_files(directory: Path) -> tuple[list[Path], list[str]]:
    review_files: list[Path] = []
    unexpected_entries: list[str] = []

    with os.scandir(directory) as entries:
        for entry in entries:
            if (
                entry.is_symlink()
                or not entry.is_file(follow_symlinks=False)
                or Path(entry.name).suffix.lower() != ".txt"
            ):
                unexpected_entries.append(entry.name)
            else:
                review_files.append(directory / entry.name)

    return sorted(review_files), unexpected_entries


def _archive_path(name: str) -> PurePosixPath:
    if "\\" in name:
        raise ValueError(f"Archive member uses an unsafe path separator: {name!r}")

    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or ":" in name
        or path.parts[0] != "aclImdb"
    ):
        raise ValueError(
            f"Archive member is outside the expected aclImdb root: {name!r}"
        )
    return path


def extract_imdb_archive(archive_path: str | Path, dataset_dir: str | Path) -> Path:
    """Safely extract an aclImdb archive without replacing an existing destination."""
    archive = Path(archive_path).expanduser().resolve()
    destination = Path(dataset_dir).expanduser().resolve()

    if not archive.is_file():
        raise FileNotFoundError(f"IMDB archive does not exist: {archive}")
    if destination.exists():
        raise FileExistsError(
            "Extraction destination already exists; inspect it before extracting: "
            f"{destination}"
        )
    if destination.name != "aclImdb":
        raise ValueError("The destination directory must be named 'aclImdb'.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, mode="r:gz") as archive_file:
        members = archive_file.getmembers()
        seen_paths: set[str] = set()
        expanded_bytes = 0
        has_root_directory = False

        for member in members:
            member_path = _archive_path(member.name)
            normalized_name = member_path.as_posix()
            if normalized_name in seen_paths:
                raise ValueError(f"Archive contains a duplicate path: {member.name!r}")
            seen_paths.add(normalized_name)

            if not (member.isfile() or member.isdir()):
                raise ValueError(f"Archive contains a non-file entry: {member.name!r}")
            if member.size < 0:
                raise ValueError(
                    f"Archive contains a negative file size: {member.name!r}"
                )
            if member.name == "aclImdb" and member.isdir():
                has_root_directory = True
            if member.isfile():
                expanded_bytes += member.size

        if not has_root_directory:
            raise ValueError(
                "Archive does not contain the expected aclImdb root directory."
            )

        required_bytes = expanded_bytes + max(50 * 1024 * 1024, expanded_bytes // 10)
        free_bytes = shutil.disk_usage(destination.parent).free
        if free_bytes < required_bytes:
            raise OSError(
                "Insufficient free space for extraction: "
                f"need approximately {required_bytes:,} bytes, have {free_bytes:,}."
            )

        staging_dir = Path(
            tempfile.mkdtemp(prefix=".aclImdb-extract-", dir=destination.parent)
        )
        try:
            archive_file.extractall(path=staging_dir, members=members, filter="data")
            staged_dataset = staging_dir / "aclImdb"
            if not staged_dataset.is_dir():
                raise OSError(
                    "Extraction did not produce the expected aclImdb directory."
                )
            if destination.exists():
                raise FileExistsError(
                    f"Extraction destination appeared during extraction: {destination}"
                )
            staged_dataset.rename(destination)
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)

    return destination


def validate_records(records: pd.DataFrame) -> None:
    """Raise a clear error when text/label records are missing or invalid."""
    if not isinstance(records, pd.DataFrame):
        raise TypeError("Records must be provided as a pandas DataFrame.")

    missing_columns = {"text", "label"}.difference(records.columns)
    if missing_columns:
        names = ", ".join(sorted(missing_columns))
        raise ValueError(f"Records are missing required column(s): {names}.")
    if records.empty:
        raise ValueError("The review records are empty.")

    invalid_text = records["text"].map(
        lambda value: not isinstance(value, str) or not value.strip()
    )
    if invalid_text.any():
        raise ValueError(
            f"Records contain {int(invalid_text.sum())} missing or empty text value(s)."
        )

    valid_labels = records["label"].isin([0, 1])
    if not valid_labels.all():
        raise ValueError(
            "Labels must be non-missing binary integers: 0 (negative), 1 (positive)."
        )


def _split_directories(dataset_dir: Path, split: str) -> dict[str, Path]:
    if split not in ("train", "test"):
        raise ValueError("Split must be either 'train' or 'test'.")
    if not dataset_dir.exists():
        raise FileNotFoundError(f"Dataset directory does not exist: {dataset_dir}")
    if not dataset_dir.is_dir():
        raise NotADirectoryError(f"Dataset path is not a directory: {dataset_dir}")

    split_dir = dataset_dir / split
    if not split_dir.is_dir():
        raise FileNotFoundError(f"Missing {split} split directory: {split_dir}")

    allowed_directories = (
        {"neg", "pos", "unsup"} if split == "train" else {"neg", "pos"}
    )
    unexpected = sorted(
        child.name
        for child in split_dir.iterdir()
        if child.is_dir() and child.name not in allowed_directories
    )
    if unexpected:
        raise ValueError(
            f"Unexpected directory(ies) in {split_dir}: {', '.join(unexpected)}"
        )

    class_directories: dict[str, Path] = {}
    for class_name in LABELS:
        class_dir = split_dir / class_name
        if not class_dir.is_dir():
            raise FileNotFoundError(f"Missing sentiment directory: {class_dir}")

        text_files, unexpected_entries = _review_files(class_dir)
        if unexpected_entries:
            raise ValueError(
                f"Unexpected entries in {class_dir}: "
                f"{', '.join(sorted(unexpected_entries)[:5])}"
            )

        if not text_files:
            raise ValueError(f"No .txt review files found in {class_dir}")
        class_directories[class_name] = class_dir

    return class_directories


def load_imdb_split(
    dataset_dir: str | Path, split: Literal["train", "test"]
) -> pd.DataFrame:
    """Load one labeled split as columns text, label, split, and review_id."""
    root = Path(dataset_dir).expanduser()
    class_directories = _split_directories(root, split)
    rows: list[dict[str, str | int]] = []

    for class_name, label in LABELS.items():
        for review_path in sorted(class_directories[class_name].glob("*.txt")):
            try:
                text = review_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as error:
                raise ValueError(
                    f"Could not read review file {review_path}: {error}"
                ) from error
            if not text.strip():
                raise ValueError(f"Review file is empty: {review_path}")

            rows.append(
                {
                    "text": text,
                    "label": label,
                    "split": split,
                    "review_id": review_path.relative_to(root).as_posix(),
                }
            )

    records = pd.DataFrame.from_records(rows)
    validate_records(records)
    return records


def load_imdb_data(dataset_dir: str | Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load official training and test splits separately; omit unlabeled reviews."""
    training_records = load_imdb_split(dataset_dir, "train")
    test_records = load_imdb_split(dataset_dir, "test")
    return training_records, test_records


def split_training_data(
    training_records: pd.DataFrame,
    validation_size: float = 0.1,
    seed: int = 42,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Create deterministic, stratified training and validation subsets."""
    validate_records(training_records)
    if not 0 < validation_size < 1:
        raise ValueError("validation_size must be greater than 0 and less than 1.")
    if "split" in training_records and not training_records["split"].eq("train").all():
        raise ValueError(
            "Only the official training split may be divided into train/validation."
        )

    try:
        training, validation = train_test_split(
            training_records,
            test_size=validation_size,
            random_state=seed,
            stratify=training_records["label"],
        )
    except ValueError as error:
        raise ValueError(
            f"Could not create a stratified validation split: {error}"
        ) from error

    training = training.copy()
    validation = validation.copy()
    training["split"] = "train"
    validation["split"] = "validation"
    return training.reset_index(drop=True), validation.reset_index(drop=True)


def validate_imdb_dataset(
    dataset_dir: str | Path,
    require_standard_counts: bool = True,
) -> dict[str, object]:
    """Inspect layout, text readability, empty reviews, duplicates, and counts."""
    root = Path(dataset_dir).expanduser()
    if not root.exists():
        raise FileNotFoundError(f"Dataset directory does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"Dataset path is not a directory: {root}")

    issues: list[str] = []
    missing_directories: list[str] = []
    unexpected_directories: list[str] = []
    unexpected_files: list[str] = []
    empty_examples: list[str] = []
    unreadable_examples: list[str] = []
    duplicate_examples: list[str] = []
    empty_file_count = 0
    unreadable_file_count = 0
    duplicate_labeled_reviews = 0
    seen_reviews: dict[str, str] = {}
    counts = {
        "train": {"neg": 0, "pos": 0},
        "test": {"neg": 0, "pos": 0},
    }
    unlabeled_count: int | None = None

    expected_split_dirs = {"train": {"neg", "pos", "unsup"}, "test": {"neg", "pos"}}
    for split, allowed in expected_split_dirs.items():
        split_dir = root / split
        if not split_dir.is_dir():
            missing_directories.append(split_dir.relative_to(root).as_posix())
            continue
        for child in split_dir.iterdir():
            if child.is_dir() and child.name not in allowed:
                unexpected_directories.append(child.relative_to(root).as_posix())

    review_directories: list[tuple[str, str | None, Path]] = []
    for split in ("train", "test"):
        for class_name in LABELS:
            class_dir = root / split / class_name
            if class_dir.is_dir():
                review_directories.append((split, class_name, class_dir))
            else:
                missing_directories.append(class_dir.relative_to(root).as_posix())

    unlabeled_dir = root / "train" / "unsup"
    if unlabeled_dir.is_dir():
        review_directories.append(("train", None, unlabeled_dir))

    for split, class_name, review_dir in review_directories:
        files, unexpected_entries = _review_files(review_dir)
        unexpected_files.extend(
            (review_dir / name).relative_to(root).as_posix()
            for name in unexpected_entries
        )
        for entry in files:
            if class_name is None:
                unlabeled_count = (unlabeled_count or 0) + 1
            else:
                counts[split][class_name] += 1

            try:
                text = entry.read_text(encoding="utf-8")
            except OSError, UnicodeError:
                unreadable_file_count += 1
                if len(unreadable_examples) < 10:
                    unreadable_examples.append(entry.relative_to(root).as_posix())
                continue

            if not text.strip():
                empty_file_count += 1
                if len(empty_examples) < 10:
                    empty_examples.append(entry.relative_to(root).as_posix())
                continue

            if class_name is not None:
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                if digest in seen_reviews:
                    duplicate_labeled_reviews += 1
                    if len(duplicate_examples) < 10:
                        duplicate_examples.append(entry.relative_to(root).as_posix())
                else:
                    seen_reviews[digest] = entry.relative_to(root).as_posix()

    if missing_directories:
        issues.append("Missing expected directories: " + ", ".join(missing_directories))
    if unexpected_directories:
        issues.append(
            "Unexpected directories: " + ", ".join(unexpected_directories[:10])
        )
    if unexpected_files:
        issues.append(
            "Unexpected files in review directories: "
            + ", ".join(unexpected_files[:10])
        )
    if empty_file_count:
        issues.append(
            f"Found {empty_file_count} empty or whitespace-only review file(s)."
        )
    if unreadable_file_count:
        issues.append(f"Found {unreadable_file_count} unreadable review file(s).")

    standard_counts_match = (
        all(
            counts[split][class_name] == expected_count
            for split, expected in STANDARD_COUNTS.items()
            for class_name, expected_count in expected.items()
        )
        and unlabeled_count == STANDARD_UNLABELED_COUNT
    )
    if require_standard_counts and not standard_counts_match:
        issues.append("Review counts do not match the standard aclImdb v1 dataset.")

    return {
        "dataset_dir": str(root.resolve()),
        "counts": {
            "train": {
                "negative": counts["train"]["neg"],
                "positive": counts["train"]["pos"],
            },
            "test": {
                "negative": counts["test"]["neg"],
                "positive": counts["test"]["pos"],
            },
        },
        "unlabeled_train_count": unlabeled_count,
        "empty_file_count": empty_file_count,
        "empty_file_examples": empty_examples,
        "unreadable_file_count": unreadable_file_count,
        "unreadable_file_examples": unreadable_examples,
        "duplicate_labeled_reviews": duplicate_labeled_reviews,
        "duplicate_examples": duplicate_examples,
        "missing_directories": missing_directories,
        "unexpected_directories": unexpected_directories,
        "unexpected_files": unexpected_files[:20],
        "standard_counts_match": standard_counts_match,
        "valid": not issues,
        "issues": issues,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract or validate the local aclImdb dataset."
    )
    parser.add_argument("--dataset-dir", type=Path, default=Path("datasets/aclImdb"))
    parser.add_argument(
        "--extract", action="store_true", help="Safely extract the archive first."
    )
    parser.add_argument("--archive", type=Path, help="Path to aclImdb_v1.tar.gz.")
    arguments = parser.parse_args()

    try:
        if arguments.extract:
            if arguments.archive is None:
                parser.error("--archive is required when --extract is specified.")
            extracted = extract_imdb_archive(arguments.archive, arguments.dataset_dir)
            print(f"Extracted dataset to {extracted}")

        report = validate_imdb_dataset(arguments.dataset_dir)
    except (OSError, tarfile.TarError, ValueError) as error:
        print(f"Dataset error: {error}", file=sys.stderr)
        return 2

    print(json.dumps(report, indent=2))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
