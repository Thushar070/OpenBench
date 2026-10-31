"""Phase 15 robustness evaluation for the five frozen IMDb classifiers."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import string
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import sklearn
import torch

from deeptext.data.dataset import load_imdb_split, split_training_data
from deeptext.evaluation.comparison import evaluate_comparison_model
from deeptext.inference.predictor import (
    NeuralSentimentPredictor,
    load_recurrent_predictor,
    load_textcnn_predictor,
)
from deeptext.models.baseline import (
    load_baseline_artifacts,
    predict_baseline_probabilities,
)
from deeptext.preprocessing.tokenizer import normalize_text

SEED = 42
VALIDATION_SIZE = 0.2
MODEL_KEYS = ("logistic_regression", "textcnn", "rnn", "lstm", "gru")
MODEL_NAMES = {
    "logistic_regression": "TF-IDF + Logistic Regression",
    "textcnn": "TextCNN",
    "rnn": "Vanilla RNN",
    "lstm": "LSTM",
    "gru": "GRU",
}
COLORS = ("#18A558", "#F28E2B", "#E45756", "#39A99B", "#C49A2A")
CHALLENGE_CATEGORIES = (
    "negation",
    "negation_scope",
    "mixed_sentiment",
    "contrastive_clauses",
    "intensifiers_diminishers",
    "concessive_compositional",
    "implicit_sentiment",
    "lexical_sentiment_traps",
    "long_context_sentiment",
    "ambiguous_borderline",
)

TRANSFORM_CATEGORIES = {
    **{
        name: "spelling"
        for name in (
            "char_deletion",
            "char_insertion",
            "char_substitution",
            "adjacent_transposition",
            "repeated_character",
            "common_typo",
        )
    },
    **{
        name: "capitalization"
        for name in (
            "full_lowercase",
            "full_uppercase",
            "randomized_capitalization",
            "selected_word_capitalization",
        )
    },
    **{
        name: "punctuation"
        for name in (
            "add_exclamation",
            "add_question_mark",
            "repeated_punctuation",
            "reduced_punctuation",
            "punctuation_removal",
        )
    },
    **{
        name: "whitespace_formatting"
        for name in (
            "multiple_spaces",
            "edge_whitespace",
            "newline_formatting",
            "tab_formatting",
            "repeated_line_breaks",
            "html_break_remnant",
        )
    },
    **{
        name: "word_level"
        for name in (
            "contraction_expansion",
            "contraction",
            "safe_synonym",
        )
    },
}
TRANSFORMATION_TYPES = tuple(TRANSFORM_CATEGORIES)

_POSITIVE_WORDS = set(
    "amazing awesome beautiful best brilliant charming clever delightful enjoyable "
    "excellent exciting fantastic favorite funny good great hilarious impressive "
    "incredible lovely moving original perfect pleasant powerful recommend "
    "remarkable satisfying spectacular strong superb terrific touching wonderful "
    "worth worthwhile warm witty enjoyable engaging fun masterpiece love loved "
    "solid effective compelling entertaining cleverest fresh humane earned".split()
)
_NEGATIVE_WORDS = set(
    "awful bad boring broken confusing disappointing dull empty failure flat "
    "flawed forced frustrating horrible lifeless messy miserable miss predictable "
    "poor pointless regrettable repetitive rotten silly slow terrible tedious "
    "thin tiresome tragic weak worst waste wooden unfunny incoherent irritating "
    "unconvincing unpleasant forgettable".split()
)
_NEGATORS = {
    "not",
    "no",
    "never",
    "hardly",
    "barely",
    "without",
    "cannot",
    "nor",
    "neither",
    "n't",
}
_PROTECTED_WORDS = (
    _POSITIVE_WORDS
    | _NEGATIVE_WORDS
    | _NEGATORS
    | {
        "isn't",
        "aren't",
        "wasn't",
        "weren't",
        "don't",
        "doesn't",
        "didn't",
        "can't",
        "couldn't",
        "wouldn't",
        "shouldn't",
        "won't",
        "haven't",
        "hasn't",
        "hadn't",
        "mustn't",
        "shan't",
    }
)
_CHARACTER_WORD = re.compile(r"\b[A-Za-z]{5,}\b")
_TYPO_MAP = {
    "because": "becuase",
    "their": "thier",
    "really": "realy",
    "movie": "moive",
    "people": "peopel",
    "friend": "freind",
    "which": "whcih",
    "there": "therre",
    "receive": "recieve",
    "beautiful": "beautfiul",
    "separate": "seperate",
}
_SAFE_SYNONYMS = {
    "movie": "film",
    "film": "movie",
    "actor": "performer",
    "actors": "performers",
    "story": "narrative",
    "scene": "sequence",
    "director": "filmmaker",
    "audience": "viewers",
}
_EXPANSIONS = {
    "didn't": "did not",
    "wasn't": "was not",
    "isn't": "is not",
    "weren't": "were not",
    "aren't": "are not",
    "doesn't": "does not",
    "don't": "do not",
    "couldn't": "could not",
    "wouldn't": "would not",
    "shouldn't": "should not",
    "haven't": "have not",
    "hasn't": "has not",
    "hadn't": "had not",
    "can't": "cannot",
    "won't": "will not",
}
_CONTRACTIONS = {value: key for key, value in _EXPANSIONS.items()}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_hashes(run_directories: dict[str, Path]) -> dict[str, dict[str, str]]:
    return {
        key: {
            path.relative_to(directory).as_posix(): _sha256_file(path)
            for path in sorted(directory.rglob("*"))
            if path.is_file()
        }
        for key, directory in run_directories.items()
    }


def _fingerprint(records: pd.DataFrame) -> str:
    digest = hashlib.sha256()
    for review_id, label, text in records[["review_id", "label", "text"]].itertuples(
        index=False, name=None
    ):
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        digest.update(f"{review_id}\0{int(label)}\0{text_hash}\n".encode())
    return digest.hexdigest()


def _stable_rng(seed: int, review_id: str, transform: str) -> random.Random:
    token = f"{seed}\0{review_id}\0{transform}".encode("utf-8")
    return random.Random(int.from_bytes(hashlib.sha256(token).digest()[:8], "big"))


def _case_like(original: str, replacement: str) -> str:
    if original.isupper():
        return replacement.upper()
    if original.istitle():
        return replacement.title()
    return replacement


def _eligible_words(text: str) -> list[re.Match[str]]:
    return [
        match
        for match in _CHARACTER_WORD.finditer(text)
        if match.group().lower() not in _PROTECTED_WORDS
    ]


def _replace_match(text: str, match: re.Match[str], value: str) -> str:
    return text[: match.start()] + value + text[match.end() :]


def _apply_transformation(
    text: str, transformation_type: str, rng: random.Random
) -> tuple[str, dict[str, Any]]:
    """Apply one deterministic, inspectable text transformation."""
    if transformation_type not in TRANSFORM_CATEGORIES:
        raise ValueError(f"Unsupported transformation type: {transformation_type}")

    if transformation_type in {
        "char_deletion",
        "char_insertion",
        "char_substitution",
        "adjacent_transposition",
        "repeated_character",
    }:
        choices = _eligible_words(text)
        if not choices:
            return text, {"reason": "no_eligible_non_sentiment_word"}
        word_match = rng.choice(choices)
        word = word_match.group()
        position = rng.randrange(1, len(word) - 1)
        if transformation_type == "char_deletion":
            changed = word[:position] + word[position + 1 :]
            operation = {"word": word, "position": position, "deleted": word[position]}
        elif transformation_type == "char_insertion":
            char = rng.choice(string.ascii_lowercase)
            changed = word[:position] + char + word[position:]
            operation = {"word": word, "position": position, "inserted": char}
        elif transformation_type == "char_substitution":
            original_char = word[position].lower()
            alphabet = string.ascii_lowercase.replace(original_char, "")
            char = rng.choice(alphabet)
            changed = word[:position] + char + word[position + 1 :]
            operation = {
                "word": word,
                "position": position,
                "from": word[position],
                "to": char,
            }
        elif transformation_type == "adjacent_transposition":
            if word[position] == word[position + 1]:
                return text, {"reason": "no_distinct_adjacent_characters"}
            changed = (
                word[:position]
                + word[position + 1]
                + word[position]
                + word[position + 2 :]
            )
            operation = {"word": word, "position": position, "operation": "transpose"}
        else:
            changed = word[:position] + word[position] + word[position:]
            operation = {"word": word, "position": position, "operation": "repeat"}
        return _replace_match(text, word_match, changed), operation

    if transformation_type == "common_typo":
        choices = [
            match
            for match in re.finditer(r"\b[A-Za-z]+\b", text)
            if match.group().lower() in _TYPO_MAP
        ]
        if not choices:
            return text, {"reason": "no_mapped_common_typo_word"}
        match = rng.choice(choices)
        return _replace_match(
            text, match, _case_like(match.group(), _TYPO_MAP[match.group().lower()])
        ), {"word": match.group(), "replacement": _TYPO_MAP[match.group().lower()]}

    if transformation_type == "full_lowercase":
        return text.lower(), {"operation": "lowercase_all_characters"}
    if transformation_type == "full_uppercase":
        return text.upper(), {"operation": "uppercase_all_characters"}
    if transformation_type == "randomized_capitalization":
        output = "".join(
            char.upper()
            if char.isalpha() and rng.random() < 0.5
            else char.lower()
            if char.isalpha()
            else char
            for char in text
        )
        if output == text:
            match = next((m for m in re.finditer(r"[A-Za-z]", text)), None)
            if match is None:
                return text, {"reason": "no_alphabetic_characters"}
            char = match.group()
            output = _replace_match(text, match, char.swapcase())
        return output, {"operation": "seeded_random_character_case", "seeded": True}
    if transformation_type == "selected_word_capitalization":
        choices = _eligible_words(text)
        if not choices:
            return text, {"reason": "no_eligible_non_sentiment_word"}
        match = rng.choice(choices)
        return _replace_match(text, match, match.group().upper()), {
            "word": match.group(),
            "operation": "uppercase_selected_word",
        }

    if transformation_type in {"add_exclamation", "add_question_mark"}:
        mark = "!" if transformation_type == "add_exclamation" else "?"
        output = text.rstrip()
        output = re.sub(r"[.!?]+$", "", output) + mark
        return output, {"appended_mark": mark}
    if transformation_type == "repeated_punctuation":
        output = text.rstrip()
        match = re.search(r"[.!?]$", output)
        mark = match.group() if match else "!"
        if match:
            output = output[:-1]
        return output + mark * 3, {"repeated_mark": mark, "count": 3}
    if transformation_type == "reduced_punctuation":
        match = re.search(r"[,;:!?\.]", text)
        if not match:
            return text, {"reason": "no_punctuation_to_reduce"}
        return text[: match.start()] + text[match.end() :], {
            "removed_mark": match.group(),
            "position": match.start(),
        }
    if transformation_type == "punctuation_removal":
        output = re.sub(r"[!?,.;:]+", "", text)
        return output, {"removed": "!?,.;:"}

    if transformation_type == "multiple_spaces":
        match = re.search(r"\s+", text)
        if not match:
            return text, {"reason": "no_internal_whitespace"}
        return text[: match.start()] + "   " + text[match.end() :], {
            "position": match.start(),
            "replacement": "three spaces",
        }
    if transformation_type == "edge_whitespace":
        return f"  {text}\t ", {"prefix_spaces": 2, "suffix": "tab and space"}
    if transformation_type in {"newline_formatting", "tab_formatting"}:
        match = re.search(r"\s+", text)
        if not match:
            return text, {"reason": "no_internal_whitespace"}
        replacement = "\n" if transformation_type == "newline_formatting" else "\t"
        return text[: match.start()] + replacement + text[match.end() :], {
            "position": match.start(),
            "replacement": repr(replacement),
        }
    if transformation_type == "repeated_line_breaks":
        if "\n" in text:
            return text.replace("\n", "\n\n", 1), {
                "operation": "double_first_line_break"
            }
        match = re.search(r"\s+", text)
        if not match:
            return text, {"reason": "no_internal_whitespace"}
        return text[: match.start()] + "\n\n" + text[match.end() :], {
            "position": match.start(),
            "operation": "insert_two_line_breaks",
        }
    if transformation_type == "html_break_remnant":
        match = re.search(r"\s+", text)
        if not match:
            return text, {"reason": "no_internal_whitespace"}
        return text[: match.start()] + "<br />" + text[match.end() :], {
            "position": match.start(),
            "inserted_markup": "<br />",
        }

    if transformation_type in {"contraction_expansion", "contraction"}:
        mapping = (
            _EXPANSIONS
            if transformation_type == "contraction_expansion"
            else _CONTRACTIONS
        )
        options = sorted(mapping.items(), key=lambda item: -len(item[0]))
        matches = []
        for source, replacement in options:
            found = re.search(rf"\b{re.escape(source)}\b", text, flags=re.IGNORECASE)
            if found:
                matches.append((found, replacement))
        if not matches:
            return text, {"reason": "no_applicable_contraction"}
        match, replacement = rng.choice(matches)
        return _replace_match(text, match, replacement), {
            "source": match.group(),
            "replacement": replacement,
        }
    if transformation_type == "safe_synonym":
        choices = [
            match
            for match in re.finditer(r"\b[A-Za-z]+\b", text)
            if match.group().lower() in _SAFE_SYNONYMS
        ]
        if not choices:
            return text, {"reason": "no_safe_synonym_candidate"}
        match = rng.choice(choices)
        replacement = _SAFE_SYNONYMS[match.group().lower()]
        return _replace_match(text, match, _case_like(match.group(), replacement)), {
            "source": match.group(),
            "replacement": replacement,
            "mapping_source": "fixed Phase 15 synonym mapping",
        }
    raise ValueError(f"Unsupported transformation type: {transformation_type}")


def _expanded_tokens(text: str) -> list[str]:
    output = text.lower().replace("’", "'")
    for contraction, expansion in _EXPANSIONS.items():
        output = re.sub(rf"\b{re.escape(contraction)}\b", expansion, output)
    return re.findall(r"[a-z]+", output)


def _sentiment_signature(text: str) -> tuple[tuple[str, int], ...]:
    tokens = _expanded_tokens(text)
    counts = Counter(
        word
        for word in tokens
        if word in _POSITIVE_WORDS or word in _NEGATIVE_WORDS or word in _NEGATORS
    )
    return tuple(sorted(counts.items()))


def _rejection_reason(original: str, transformed: str, seen: set[str]) -> str | None:
    if not isinstance(transformed, str) or not transformed.strip():
        return "empty_or_invalid_output"
    if len(transformed) > 200_000:
        return "output_exceeds_text_limit"
    if transformed == original:
        return "transformation_did_not_change_text"
    if _sentiment_signature(original) != _sentiment_signature(transformed):
        return "sentiment_or_negation_lexicon_changed"
    if transformed in seen:
        return "duplicate_variant_for_source_review"
    return None


def generate_automatic_perturbations(
    reviews: pd.DataFrame,
    *,
    seed: int = SEED,
    max_variants_per_review: int = 14,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Generate deterministic, label-preserving candidates and QC rejections."""
    if not isinstance(reviews, pd.DataFrame):
        raise TypeError("reviews must be a pandas DataFrame.")
    required = {"review_id", "label", "text"}
    if not required.issubset(reviews.columns) or reviews.empty:
        raise ValueError(
            "reviews must contain nonempty review_id, label, and text columns."
        )
    if reviews.review_id.duplicated().any():
        raise ValueError("source review IDs must be unique.")
    if isinstance(max_variants_per_review, bool) or max_variants_per_review < 1:
        raise ValueError("max_variants_per_review must be a positive integer.")

    variants = []
    rejected = []
    for row in reviews.itertuples(index=False):
        original = row.text
        order_rng = _stable_rng(seed, str(row.review_id), "type-order")
        types = list(TRANSFORMATION_TYPES)
        order_rng.shuffle(types)
        seen: set[str] = set()
        accepted = 0
        for transformation_type in types:
            if accepted >= max_variants_per_review:
                rejected.append(
                    {
                        "review_id": str(row.review_id),
                        "transformation_type": transformation_type,
                        "category": TRANSFORM_CATEGORIES[transformation_type],
                        "rejected_reason": "per_review_variant_limit",
                    }
                )
                continue
            rng = _stable_rng(seed, str(row.review_id), transformation_type)
            transformed, parameters = _apply_transformation(
                original, transformation_type, rng
            )
            reason = parameters.pop("reason", None) or _rejection_reason(
                original, transformed, seen
            )
            if reason:
                rejected.append(
                    {
                        "review_id": str(row.review_id),
                        "transformation_type": transformation_type,
                        "category": TRANSFORM_CATEGORIES[transformation_type],
                        "rejected_reason": reason,
                    }
                )
                continue
            seen.add(transformed)
            accepted += 1
            variant_id = hashlib.sha256(
                f"{row.review_id}\0{transformation_type}\0{seed}".encode()
            ).hexdigest()[:16]
            variants.append(
                {
                    "sample_id": f"PV-{variant_id}",
                    "review_id": str(row.review_id),
                    "label": int(row.label),
                    "category": TRANSFORM_CATEGORIES[transformation_type],
                    "transformation_type": transformation_type,
                    "transformation_parameters": json.dumps(
                        parameters, sort_keys=True, ensure_ascii=False
                    ),
                    "original_text": original,
                    "transformed_text": transformed,
                    "label_preserving_track": True,
                }
            )
    return pd.DataFrame(variants), pd.DataFrame(rejected)


def validate_semantic_challenge(
    challenge: pd.DataFrame, *, require_all_categories: bool = True
) -> dict[str, Any]:
    required = {"challenge_id", "text", "label", "category", "rationale", "phenomenon"}
    if not isinstance(challenge, pd.DataFrame) or not required.issubset(
        challenge.columns
    ):
        raise ValueError("Challenge dataset is missing required columns.")
    if challenge.empty or challenge[list(required)].isna().any().any():
        raise ValueError(
            "Challenge dataset must be nonempty and have no missing values."
        )
    if challenge.challenge_id.duplicated().any():
        raise ValueError("Challenge IDs must be unique.")
    if challenge.text.map(
        lambda value: not isinstance(value, str) or not value.strip()
    ).any():
        raise ValueError("Challenge texts must be nonempty strings.")
    if challenge.rationale.map(
        lambda value: not isinstance(value, str) or not value.strip()
    ).any():
        raise ValueError("Each challenge needs a nonempty rationale.")
    labels = pd.to_numeric(challenge.label, errors="coerce")
    if labels.isna().any() or not labels.isin((0, 1)).all():
        raise ValueError("Challenge labels must be binary integers 0 or 1.")
    if not np.array_equal(labels.to_numpy(dtype=np.int64), labels.to_numpy()):
        raise ValueError("Challenge labels must be integers.")
    normalized = challenge.text.map(normalize_text)
    if normalized.duplicated().any():
        raise ValueError("Challenge texts contain normalized duplicates.")
    categories = set(challenge.category)
    if require_all_categories and categories != set(CHALLENGE_CATEGORIES):
        raise ValueError("Challenge dataset must cover all ten semantic categories.")
    counts = (
        challenge.assign(label=labels.astype(int))
        .groupby(["category", "label"], observed=True)
        .size()
    )
    if require_all_categories and any(
        counts.get((category, label), 0) == 0
        for category in CHALLENGE_CATEGORIES
        for label in (0, 1)
    ):
        raise ValueError("Every semantic category must contain both labels.")
    distribution = (
        challenge.assign(label=labels.astype(int)).label.value_counts().to_dict()
    )
    return {
        "sample_count": len(challenge),
        "unique_ids": int(challenge.challenge_id.nunique()),
        "class_distribution": {
            str(key): int(value) for key, value in distribution.items()
        },
        "category_distribution": {
            str(key): int(value)
            for key, value in challenge.category.value_counts().sort_index().items()
        },
        "all_required_categories_present": categories == set(CHALLENGE_CATEGORIES),
    }


def validate_no_challenge_leakage(
    challenge: pd.DataFrame, source_reviews: pd.DataFrame
) -> None:
    challenge_texts = set(challenge.text.map(normalize_text))
    source_texts = set(source_reviews.text.map(normalize_text))
    overlap = challenge_texts & source_texts
    if overlap:
        raise ValueError(
            "Semantic challenge contains "
            f"{len(overlap)} exact normalized source review(s)."
        )


def _phase12_sources(root: Path) -> tuple[dict[str, Path], Path, dict[str, Any]]:
    candidates = sorted(
        (root / "outputs/neural_comparison").glob(
            "imdb-neural-comparison-*/comparison.json"
        )
    )
    if not candidates:
        raise FileNotFoundError("No Phase 12 validation comparison artifact exists.")
    comparison_path = candidates[-1]
    comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
    if (
        comparison.get("validation_only") is not True
        or comparison.get("official_test_evaluated") is not False
        or set(comparison.get("models", [])) != set(MODEL_KEYS)
    ):
        raise ValueError(
            "Phase 12 source comparison is not validation-only for all models."
        )
    recorded = comparison["source_artifacts"]
    sources = {
        "logistic_regression": Path(recorded["baseline"]).resolve(),
        **{
            key: Path(recorded[key]).resolve()
            for key in ("textcnn", "rnn", "lstm", "gru")
        },
    }
    phase6_directory = Path(recorded["phase6_comparison"]).resolve()
    phase6_metadata = json.loads(
        (phase6_directory / "comparison.json").read_text(encoding="utf-8")
    )
    phase6_predictions = (
        phase6_directory / phase6_metadata["artifacts"]["validation_predictions"]
    )
    return sources, phase6_predictions, comparison


def _validate_source_metadata(
    sources: dict[str, Path],
    comparison: dict[str, Any],
    training: pd.DataFrame,
    validation: pd.DataFrame,
) -> None:
    training_hash, validation_hash = _fingerprint(training), _fingerprint(validation)
    split = comparison.get("split", {})
    if (
        split.get("training_review_fingerprint_sha256") != training_hash
        or split.get("validation_review_fingerprint_sha256") != validation_hash
        or split.get("validation_rows") != len(validation)
    ):
        raise ValueError("Current train/validation partition differs from Phase 12.")
    for key, directory in sources.items():
        metadata_path = directory / "metadata.json"
        if not metadata_path.is_file():
            raise FileNotFoundError(f"Missing {key} metadata: {metadata_path}")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        split_metadata = metadata.get("split", {})
        if (
            metadata.get("official_test_evaluated") is not False
            or split_metadata.get("source") != "official labeled training split only"
            or split_metadata.get("validation_fraction") != VALIDATION_SIZE
            or split_metadata.get("random_state", metadata.get("seed")) != SEED
        ):
            raise ValueError(f"{key} metadata is not the expected frozen Phase 12 run.")


def _sample_validation(
    validation: pd.DataFrame, sample_limit: int, seed: int
) -> pd.DataFrame:
    if (
        isinstance(sample_limit, bool)
        or not isinstance(sample_limit, int)
        or sample_limit < 2
    ):
        raise ValueError("sample_limit must be an integer of at least 2.")
    rng = np.random.default_rng(seed)
    class_zero = validation.index[validation.label == 0].to_numpy()
    class_one = validation.index[validation.label == 1].to_numpy()
    zero_count = min(len(class_zero), sample_limit // 2)
    one_count = min(len(class_one), sample_limit - zero_count)
    selected = np.concatenate(
        (
            rng.choice(class_zero, size=zero_count, replace=False),
            rng.choice(class_one, size=one_count, replace=False),
        )
    )
    selected.sort()
    return validation.loc[selected].reset_index(drop=True)


def _load_models(
    sources: dict[str, Path], device: torch.device
) -> tuple[Any, dict[str, NeuralSentimentPredictor]]:
    baseline, _ = load_baseline_artifacts(sources["logistic_regression"])
    predictors: dict[str, NeuralSentimentPredictor] = {
        "textcnn": load_textcnn_predictor(sources["textcnn"], device)
    }
    for key in ("rnn", "lstm", "gru"):
        predictors[key] = load_recurrent_predictor(sources[key], device)
    return baseline, predictors


def _predict_all(
    texts: list[str],
    baseline: Any,
    predictors: dict[str, NeuralSentimentPredictor],
    batch_size: int,
) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    if not texts:
        raise ValueError("Cannot predict an empty text collection.")
    predictions: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    labels = []
    probabilities = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        labels.extend(baseline.predict(batch).astype(int).tolist())
        probabilities.extend(
            predict_baseline_probabilities(baseline, batch)[:, 1].tolist()
        )
    predictions["logistic_regression"] = (
        np.asarray(labels, dtype=np.int64),
        np.asarray(probabilities, dtype=np.float64),
    )
    for key, predictor in predictors.items():
        rows = predictor.predict_batch(texts, batch_size=batch_size)
        predictions[key] = (
            np.asarray([row["predicted_class"] for row in rows], dtype=np.int64),
            np.asarray([row["positive_probability"] for row in rows], dtype=np.float64),
        )
    return predictions


def _saved_validation_predictions(
    validation: pd.DataFrame, phase6_path: Path, sources: dict[str, Path]
) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    expected_ids = validation.review_id.tolist()
    expected_labels = validation.label.to_numpy(dtype=np.int64)
    phase6 = pd.read_csv(phase6_path)
    if phase6.review_id.tolist() != expected_ids or not np.array_equal(
        phase6.true_label.to_numpy(dtype=np.int64), expected_labels
    ):
        raise ValueError(
            "Phase 6 predictions do not align with Phase 12 validation rows."
        )
    result = {
        "logistic_regression": (
            phase6.baseline_prediction.to_numpy(dtype=np.int64),
            phase6.baseline_positive_probability.to_numpy(dtype=np.float64),
        ),
        "textcnn": (
            phase6.textcnn_prediction.to_numpy(dtype=np.int64),
            phase6.textcnn_positive_probability.to_numpy(dtype=np.float64),
        ),
    }
    for key in ("rnn", "lstm", "gru"):
        frame = pd.read_csv(sources[key] / "validation_predictions.csv")
        if frame.review_id.tolist() != expected_ids or not np.array_equal(
            frame.true_label.to_numpy(dtype=np.int64), expected_labels
        ):
            raise ValueError(f"Saved {key} predictions do not match validation rows.")
        result[key] = (
            frame.predicted_label.to_numpy(dtype=np.int64),
            frame.positive_probability.to_numpy(dtype=np.float64),
        )
    return result


def _prediction_frame(
    records: pd.DataFrame,
    predictions: dict[str, tuple[np.ndarray, np.ndarray]],
    *,
    include_text: bool = False,
) -> pd.DataFrame:
    identifier = next(
        column
        for column in ("review_id", "challenge_id", "sample_id")
        if column in records
    )
    frame = pd.DataFrame(
        {
            "sample_id": records[identifier].astype(str).to_list(),
            "true_label": records.label.to_numpy(dtype=np.int64),
        }
    )
    frame["review_id"] = (
        records.review_id.astype(str).to_list()
        if "review_id" in records
        else frame.sample_id
    )
    if "category" in records:
        frame["category"] = records.category.to_list()
    if "transformation_type" in records:
        frame["transformation_type"] = records.transformation_type.to_list()
    if include_text:
        frame["text"] = records.text.to_list()
    for key in MODEL_KEYS:
        predicted, probability = predictions[key]
        frame[f"{key}_prediction"] = predicted
        frame[f"{key}_positive_probability"] = probability
        frame[f"{key}_confidence"] = np.maximum(probability, 1 - probability)
    return frame


def _metric_values(
    labels: np.ndarray, predictions: np.ndarray, probabilities: np.ndarray
) -> dict[str, Any]:
    values = evaluate_comparison_model(labels, predictions, probabilities)
    matrix = values.pop("confusion_matrix")
    values["true_negative"] = matrix[0][0]
    values["false_positive"] = matrix[0][1]
    values["false_negative"] = matrix[1][0]
    values["true_positive"] = matrix[1][1]
    return values


def _majority_prediction(group: pd.DataFrame, key: str) -> tuple[int, float]:
    counts = group[f"{key}_prediction"].value_counts()
    original = int(group[f"{key}_original_prediction"].iloc[0])
    best_count = counts.max()
    winners = [int(label) for label, count in counts.items() if count == best_count]
    prediction = original if original in winners else min(winners)
    return prediction, float(group[f"{key}_positive_probability"].mean())


def _summarize_automatic_group(
    frame: pd.DataFrame, model_key: str, *, group_name: str, group_value: str
) -> dict[str, Any]:
    labels = frame.true_label.to_numpy(dtype=np.int64)
    variant_pred = frame[f"{model_key}_prediction"].to_numpy(dtype=np.int64)
    variant_prob = frame[f"{model_key}_positive_probability"].to_numpy(dtype=np.float64)
    review_rows = []
    for review_id, group in frame.groupby("review_id", sort=False):
        prediction, probability = _majority_prediction(group, model_key)
        original_prediction = int(group[f"{model_key}_original_prediction"].iloc[0])
        review_rows.append(
            {
                "review_id": review_id,
                "label": int(group.true_label.iloc[0]),
                "prediction": prediction,
                "probability": probability,
                "original_prediction": original_prediction,
                "correctness_rate": float(
                    (group[f"{model_key}_prediction"] == group.true_label).mean()
                ),
                "flip_rate": float(
                    (
                        group[f"{model_key}_prediction"]
                        != group[f"{model_key}_original_prediction"]
                    ).mean()
                ),
            }
        )
    review_frame = pd.DataFrame(review_rows)
    comparison = _metric_values(
        review_frame.label.to_numpy(dtype=np.int64),
        review_frame.prediction.to_numpy(dtype=np.int64),
        review_frame.probability.to_numpy(dtype=np.float64),
    )
    original = _metric_values(
        review_frame.label.to_numpy(dtype=np.int64),
        review_frame.original_prediction.to_numpy(dtype=np.int64),
        np.asarray(
            [
                frame.loc[
                    frame.review_id == review_id, f"{model_key}_original_probability"
                ].iloc[0]
                for review_id in review_frame.review_id
            ],
            dtype=np.float64,
        ),
    )
    sample = _metric_values(labels, variant_pred, variant_prob)
    type_level = group_name == "transformation_type"
    return {
        "summary_level": group_name,
        "group": group_value,
        "model": model_key,
        "variant_count": len(frame),
        "unique_review_count": frame.review_id.nunique(),
        "variant_accuracy_descriptive": sample["accuracy"],
        "variant_f1_descriptive": sample["f1"],
        "original_accuracy_matched_reviews": original["accuracy"],
        "robustness_accuracy_review_level": comparison["accuracy"],
        "accuracy_degradation": comparison["accuracy"] - original["accuracy"],
        "original_f1_matched_reviews": original["f1"],
        "robustness_f1_review_level": comparison["f1"],
        "f1_degradation": comparison["f1"] - original["f1"],
        "precision_review_level": comparison["precision"],
        "recall_review_level": comparison["recall"],
        "prediction_consistency": 1.0 - float(review_frame.flip_rate.mean()),
        "prediction_flip_rate": float(review_frame.flip_rate.mean()),
        "mean_variant_correctness_per_review": float(
            review_frame.correctness_rate.mean()
        ),
        "comparison_unit": "one variant per review"
        if type_level
        else "per-review majority; ties keep original prediction",
        "true_negative": comparison["true_negative"],
        "false_positive": comparison["false_positive"],
        "false_negative": comparison["false_negative"],
        "true_positive": comparison["true_positive"],
    }


def _automatic_summaries(
    frame: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    rows = []
    for transformation_type, group in frame.groupby("transformation_type", sort=True):
        for key in MODEL_KEYS:
            rows.append(
                _summarize_automatic_group(
                    group,
                    key,
                    group_name="transformation_type",
                    group_value=transformation_type,
                )
            )
    for category, group in frame.groupby("category", sort=True):
        for key in MODEL_KEYS:
            rows.append(
                _summarize_automatic_group(
                    group, key, group_name="category", group_value=category
                )
            )
    for key in MODEL_KEYS:
        rows.append(
            _summarize_automatic_group(
                frame,
                key,
                group_name="overall",
                group_value="all_label_preserving_variants",
            )
        )
    summary = pd.DataFrame(rows)

    review_rows = []
    for review_id, group in frame.groupby("review_id", sort=False):
        for model_key in MODEL_KEYS:
            source_rows = group
            flips = (
                source_rows[f"{model_key}_prediction"].to_numpy()
                != source_rows[f"{model_key}_original_prediction"].to_numpy()
            )
            correct = (
                source_rows[f"{model_key}_prediction"].to_numpy()
                == source_rows.true_label.to_numpy()
            )
            review_rows.append(
                {
                    "review_id": review_id,
                    "model": model_key,
                    "variant_count": len(source_rows),
                    "mean_correctness": float(correct.mean()),
                    "flip_rate": float(flips.mean()),
                }
            )
    review_summary = pd.DataFrame(review_rows)
    overall = (
        summary[summary.summary_level == "overall"].set_index("model").to_dict("index")
    )
    return summary, review_summary, overall


def _generate_length_variants(
    reviews: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    variants, rejected = [], []
    for row in reviews.itertuples(index=False):
        words = row.text.split()
        for keep, side in (
            (0.75, "prefix"),
            (0.5, "prefix"),
            (0.75, "suffix"),
            (0.5, "suffix"),
        ):
            count = max(1, int(len(words) * keep))
            selected = words[:count] if side == "prefix" else words[-count:]
            transformed = " ".join(selected)
            name = f"{side}_keep_{int(keep * 100)}pct"
            if transformed == row.text or not transformed.strip():
                rejected.append(
                    {
                        "review_id": row.review_id,
                        "transformation_type": name,
                        "rejected_reason": "review_too_short",
                    }
                )
                continue
            variants.append(
                {
                    "sample_id": (
                        f"LEN-{hashlib.sha256(f'{row.review_id}:{name}'.encode()).hexdigest()[:16]}"
                    ),
                    "review_id": str(row.review_id),
                    "label": int(row.label),
                    "category": "information_loss",
                    "transformation_type": name,
                    "transformation_parameters": json.dumps(
                        {"keep_fraction": keep, "side": side}
                    ),
                    "original_text": row.text,
                    "transformed_text": transformed,
                    "label_preserving_track": False,
                    "information_loss_warning": (
                        "Truncation may remove sentiment-bearing content; original "
                        "label is retained only for sensitivity analysis."
                    ),
                }
            )
        sentences = re.split(r"(?<=[.!?])\s+", row.text.strip())
        removable = [
            index
            for index, sentence in enumerate(sentences)
            if len(sentences) > 1
            and not set(_expanded_tokens(sentence))
            & (_POSITIVE_WORDS | _NEGATIVE_WORDS | _NEGATORS)
        ]
        if removable:
            index = removable[0]
            transformed = " ".join(sentences[:index] + sentences[index + 1 :])
            variants.append(
                {
                    "sample_id": (
                        "LEN-"
                        f"{hashlib.sha256(f'{row.review_id}:sentence_ablation'.encode()).hexdigest()[:16]}"
                    ),
                    "review_id": str(row.review_id),
                    "label": int(row.label),
                    "category": "information_loss",
                    "transformation_type": "non_sentiment_sentence_ablation",
                    "transformation_parameters": json.dumps(
                        {
                            "removed_sentence_index": index,
                            "selection_rule": (
                                "first sentence without protected "
                                "sentiment/negation lexicon"
                            ),
                        }
                    ),
                    "original_text": row.text,
                    "transformed_text": transformed,
                    "label_preserving_track": False,
                    "information_loss_warning": (
                        "Lexicon-free sentences can still carry essential context; "
                        "this is an information-loss ablation, not a "
                        "label-preserving perturbation."
                    ),
                }
            )
        else:
            rejected.append(
                {
                    "review_id": row.review_id,
                    "transformation_type": "non_sentiment_sentence_ablation",
                    "rejected_reason": "no_removable_sentence_under_lexicon_heuristic",
                }
            )
    return pd.DataFrame(variants), pd.DataFrame(rejected)


def _attach_original_predictions(
    variants: pd.DataFrame,
    original_predictions: pd.DataFrame,
    transformed_predictions: dict[str, tuple[np.ndarray, np.ndarray]],
) -> pd.DataFrame:
    frame = (
        variants[["sample_id", "review_id", "label", "category", "transformation_type"]]
        .rename(columns={"label": "true_label"})
        .copy()
    )
    lookup = original_predictions.set_index("review_id")
    for key in MODEL_KEYS:
        labels, probs = transformed_predictions[key]
        frame[f"{key}_prediction"] = labels
        frame[f"{key}_positive_probability"] = probs
        frame[f"{key}_original_prediction"] = frame.review_id.map(
            lookup[f"{key}_prediction"]
        ).astype(int)
        frame[f"{key}_original_probability"] = frame.review_id.map(
            lookup[f"{key}_positive_probability"]
        ).astype(float)
    return frame


def _semantic_summaries(
    challenge_predictions: pd.DataFrame, challenge: pd.DataFrame
) -> tuple[pd.DataFrame, dict[str, Any], pd.DataFrame]:
    category_rows = []
    metrics: dict[str, Any] = {}
    joined = challenge_predictions.merge(
        challenge[["challenge_id", "rationale", "phenomenon"]],
        left_on="sample_id",
        right_on="challenge_id",
        validate="one_to_one",
    )
    for key in MODEL_KEYS:
        model_metrics = _metric_values(
            joined.true_label.to_numpy(dtype=np.int64),
            joined[f"{key}_prediction"].to_numpy(dtype=np.int64),
            joined[f"{key}_positive_probability"].to_numpy(dtype=np.float64),
        )
        model_metrics["mean_confidence"] = float(joined[f"{key}_confidence"].mean())
        model_metrics["per_class"] = {}
        for label, label_name in ((0, "negative"), (1, "positive")):
            subset = joined[joined.true_label == label]
            class_metrics = _metric_values(
                subset.true_label.to_numpy(dtype=np.int64),
                subset[f"{key}_prediction"].to_numpy(dtype=np.int64),
                subset[f"{key}_positive_probability"].to_numpy(dtype=np.float64),
            )
            model_metrics["per_class"][label_name] = {
                field: class_metrics[field] for field in ("precision", "recall", "f1")
            }
        metrics[key] = model_metrics
        for category, group in joined.groupby("category", sort=True):
            values = _metric_values(
                group.true_label.to_numpy(dtype=np.int64),
                group[f"{key}_prediction"].to_numpy(dtype=np.int64),
                group[f"{key}_positive_probability"].to_numpy(dtype=np.float64),
            )
            category_rows.append(
                {
                    "model": key,
                    "category": category,
                    "sample_count": len(group),
                    "accuracy": values["accuracy"],
                    "precision": values["precision"],
                    "recall": values["recall"],
                    "f1": values["f1"],
                    "mean_confidence": float(group[f"{key}_confidence"].mean()),
                    "true_negative": values["true_negative"],
                    "false_positive": values["false_positive"],
                    "false_negative": values["false_negative"],
                    "true_positive": values["true_positive"],
                }
            )
    # Long-form failures keep each row attributable to one model and example.
    failure_rows = []
    for key in MODEL_KEYS:
        wrong = joined[joined[f"{key}_prediction"] != joined.true_label]
        for row in wrong.itertuples(index=False):
            values = row._asdict()
            failure_rows.append(
                {
                    "track": "semantic_challenge",
                    "sample_id": values["sample_id"],
                    "review_id": values["sample_id"],
                    "category": values["category"],
                    "model": key,
                    "true_label": int(values["true_label"]),
                    "predicted_label": int(values[f"{key}_prediction"]),
                    "positive_probability": float(
                        values[f"{key}_positive_probability"]
                    ),
                    "text": challenge.loc[
                        challenge.challenge_id == values["sample_id"], "text"
                    ].iloc[0],
                    "rationale": values["rationale"],
                }
            )
    return pd.DataFrame(category_rows), metrics, pd.DataFrame(failure_rows)


def _pairwise_agreement(
    frames: list[tuple[str, pd.DataFrame]],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    agreement_rows, disagreement_rows = [], []
    for track, frame in frames:
        for left_index, left in enumerate(MODEL_KEYS):
            for right in MODEL_KEYS[left_index + 1 :]:
                left_pred = frame[f"{left}_prediction"].to_numpy(dtype=np.int64)
                right_pred = frame[f"{right}_prediction"].to_numpy(dtype=np.int64)
                agree = left_pred == right_pred
                agreement_rows.append(
                    {
                        "track": track,
                        "left_model": left,
                        "right_model": right,
                        "sample_count": len(frame),
                        "agreement_count": int(agree.sum()),
                        "agreement_rate": float(agree.mean()),
                    }
                )
                for index in np.flatnonzero(~agree):
                    row = frame.iloc[index]
                    disagreement_rows.append(
                        {
                            "track": track,
                            "sample_id": row.sample_id,
                            "review_id": row.get("review_id", row.sample_id),
                            "category": row.get("category", ""),
                            "transformation_type": row.get("transformation_type", ""),
                            "true_label": int(row.true_label),
                            "left_model": left,
                            "left_prediction": int(left_pred[index]),
                            "left_positive_probability": float(
                                row[f"{left}_positive_probability"]
                            ),
                            "right_model": right,
                            "right_prediction": int(right_pred[index]),
                            "right_positive_probability": float(
                                row[f"{right}_positive_probability"]
                            ),
                        }
                    )
    return pd.DataFrame(agreement_rows), pd.DataFrame(disagreement_rows)


def _write_robustness_plots(
    output: Path,
    automatic: pd.DataFrame,
    challenge: pd.DataFrame,
    pairwise: pd.DataFrame,
    semantic_summary: pd.DataFrame,
) -> list[str]:
    plot_directory = output / "plots"
    files = []
    overall = (
        automatic[automatic.summary_level == "overall"]
        .set_index("model")
        .loc[list(MODEL_KEYS)]
    )
    x = np.arange(len(MODEL_KEYS))
    fig, ax = plt.subplots(figsize=(11, 6), constrained_layout=True)
    ax.bar(
        x - 0.18,
        overall.original_accuracy_matched_reviews,
        0.36,
        label="Original",
        color=COLORS[0],
    )
    ax.bar(
        x + 0.18,
        overall.robustness_accuracy_review_level,
        0.36,
        label="Perturbed vote",
        color=COLORS[1],
    )
    ax.set(
        xticks=x,
        xticklabels=MODEL_KEYS,
        ylim=(0, 1),
        ylabel="Accuracy",
        title="Original vs automatic robustness accuracy",
    )
    ax.legend()
    path = plot_directory / "original_vs_perturbed_performance.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    files.append(path.name)

    categories = sorted(
        automatic.loc[automatic.summary_level == "category", "group"].unique()
    )
    for metric, title in (
        ("accuracy_degradation", "Accuracy degradation"),
        ("f1_degradation", "F1 degradation"),
        ("prediction_flip_rate", "Prediction flip rate"),
    ):
        matrix = (
            automatic[automatic.summary_level == "category"]
            .pivot(index="model", columns="group", values=metric)
            .reindex(index=MODEL_KEYS, columns=categories)
        )
        fig, ax = plt.subplots(
            figsize=(max(10, len(categories) * 1.1), 5), constrained_layout=True
        )
        image = ax.imshow(
            matrix.to_numpy(),
            aspect="auto",
            cmap="RdYlGn" if "degradation" in metric else "YlOrRd",
        )
        fig.colorbar(image, ax=ax, label=metric.replace("_", " "))
        ax.set(
            yticks=np.arange(len(MODEL_KEYS)),
            yticklabels=MODEL_KEYS,
            xticks=np.arange(len(categories)),
            xticklabels=categories,
            title=f"{title} by automatic transformation category",
        )
        plt.setp(ax.get_xticklabels(), rotation=30, ha="right")
        path = plot_directory / f"{metric}_by_category.png"
        fig.savefig(path, dpi=150)
        plt.close(fig)
        files.append(path.name)

    heat = (
        automatic[automatic.summary_level == "transformation_type"]
        .pivot(
            index="model", columns="group", values="robustness_accuracy_review_level"
        )
        .reindex(index=MODEL_KEYS)
    )
    fig, ax = plt.subplots(
        figsize=(max(12, heat.shape[1] * 0.55), 5), constrained_layout=True
    )
    image = ax.imshow(heat.to_numpy(), aspect="auto", vmin=0, vmax=1, cmap="RdYlGn")
    fig.colorbar(image, ax=ax, label="Review-level accuracy")
    ax.set(
        yticks=np.arange(len(MODEL_KEYS)),
        yticklabels=MODEL_KEYS,
        xticks=np.arange(len(heat.columns)),
        xticklabels=heat.columns,
        title="Model versus automatic transformation type",
    )
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
    path = plot_directory / "model_transformation_accuracy_heatmap.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    files.append(path.name)

    semantic = semantic_summary.pivot(
        index="model", columns="category", values="accuracy"
    ).reindex(index=MODEL_KEYS)
    fig, ax = plt.subplots(
        figsize=(max(12, semantic.shape[1] * 1.1), 5), constrained_layout=True
    )
    image = ax.imshow(semantic.to_numpy(), aspect="auto", vmin=0, vmax=1, cmap="RdYlGn")
    fig.colorbar(image, ax=ax, label="Accuracy")
    ax.set(
        yticks=np.arange(len(MODEL_KEYS)),
        yticklabels=MODEL_KEYS,
        xticks=np.arange(len(semantic.columns)),
        xticklabels=semantic.columns,
        title="Semantic challenge category accuracy",
    )
    plt.setp(ax.get_xticklabels(), rotation=35, ha="right")
    path = plot_directory / "semantic_category_accuracy_heatmap.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    files.append(path.name)

    agreement = pairwise[pairwise.track == "semantic_challenge"]
    matrix = np.eye(len(MODEL_KEYS))
    model_index = {key: i for i, key in enumerate(MODEL_KEYS)}
    for row in agreement.itertuples(index=False):
        i, j = model_index[row.left_model], model_index[row.right_model]
        matrix[i, j] = matrix[j, i] = row.agreement_rate
    fig, ax = plt.subplots(figsize=(7, 6), constrained_layout=True)
    image = ax.imshow(matrix, vmin=0, vmax=1, cmap="YlGn")
    fig.colorbar(image, ax=ax, label="Pairwise agreement")
    ax.set(
        xticks=np.arange(len(MODEL_KEYS)),
        xticklabels=MODEL_KEYS,
        yticks=np.arange(len(MODEL_KEYS)),
        yticklabels=MODEL_KEYS,
        title="Semantic challenge pairwise prediction agreement",
    )
    path = plot_directory / "pairwise_prediction_agreement.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    files.append(path.name)

    for key in MODEL_KEYS:
        source = automatic[automatic.summary_level == "overall"]
        row = source[source.model == key].iloc[0]
        matrix_values = np.array(
            [
                [row.true_negative, row.false_positive],
                [row.false_negative, row.true_positive],
            ]
        )
        fig, ax = plt.subplots(figsize=(5, 4), constrained_layout=True)
        image = ax.imshow(matrix_values, cmap="YlGn")
        for (i, j), value in np.ndenumerate(matrix_values):
            ax.text(j, i, str(value), ha="center", va="center")
        fig.colorbar(image, ax=ax, label="Review-level count")
        ax.set(
            xticks=[0, 1],
            xticklabels=["Predicted negative", "Predicted positive"],
            yticks=[0, 1],
            yticklabels=["Actual negative", "Actual positive"],
            title=f"Automatic robustness majority-vote confusion — {key}",
        )
        path = plot_directory / f"automatic_confusion_{key}.png"
        fig.savefig(path, dpi=150)
        plt.close(fig)
        files.append(path.name)
    return files


def run_robustness_evaluation(
    *,
    dataset_directory: str | Path = "datasets/aclImdb",
    challenge_path: str | Path = "data/robustness/semantic_challenge.csv",
    output_root: str | Path = "outputs/robustness",
    sample_limit: int = 250,
    max_variants_per_review: int = len(TRANSFORMATION_TYPES),
    seed: int = 42,
    batch_size: int = 128,
    device: str = "auto",
) -> dict[str, Any]:
    """Run separate automatic-perturbation, information-loss, and semantic tracks."""
    if device not in {"auto", "cpu", "cuda"}:
        raise ValueError("device must be auto, cpu, or cuda.")
    selected_device = torch.device(
        "cuda"
        if device == "auto" and torch.cuda.is_available()
        else "cpu"
        if device == "auto"
        else device
    )
    if selected_device.type == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested but is not available.")
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or batch_size < 1
    ):
        raise ValueError("batch_size must be a positive integer.")
    root = Path.cwd().resolve()
    phase12_candidates = sorted(
        (root / "outputs/neural_comparison").glob(
            "imdb-neural-comparison-*/comparison.json"
        )
    )
    if not phase12_candidates:
        raise FileNotFoundError("Phase 12 comparison is missing.")
    phase12_path = phase12_candidates[-1]
    sources, phase6_path, phase12 = _phase12_sources(root)
    # Deliberately read only the official labeled training split.
    training_records = load_imdb_split(dataset_directory, "train")
    training, validation = split_training_data(
        training_records, validation_size=VALIDATION_SIZE, seed=SEED
    )
    _validate_source_metadata(sources, phase12, training, validation)
    challenge_file = Path(challenge_path)
    if not challenge_file.is_absolute():
        challenge_file = root / challenge_file
    challenge = pd.read_csv(challenge_file)
    challenge_info = validate_semantic_challenge(challenge)
    validate_no_challenge_leakage(challenge, training_records)
    originals = _sample_validation(validation, sample_limit, seed)
    variants, rejected = generate_automatic_perturbations(
        originals, seed=seed, max_variants_per_review=max_variants_per_review
    )
    if variants.empty:
        raise RuntimeError("No valid automatic perturbations were generated.")
    if variants.sample_id.duplicated().any():
        raise RuntimeError("Generated perturbation IDs are not unique.")
    missing_types = sorted(
        set(TRANSFORMATION_TYPES) - set(variants.transformation_type)
    )
    missing_categories = sorted(
        set(TRANSFORM_CATEGORIES.values()) - set(variants.category)
    )

    hashes_before = _directory_hashes(sources)
    baseline, predictors = _load_models(sources, selected_device)
    original_predictions = _predict_all(
        originals.text.tolist(), baseline, predictors, batch_size
    )
    saved = _saved_validation_predictions(validation, phase6_path, sources)
    original_indices = originals.review_id.map(
        {review_id: index for index, review_id in enumerate(validation.review_id)}
    ).to_numpy(dtype=np.int64)
    for key in MODEL_KEYS:
        if not np.array_equal(
            original_predictions[key][0], saved[key][0][original_indices]
        ):
            raise RuntimeError(
                f"{key} original predictions failed Phase 12 reproduction."
            )
    original_frame = _prediction_frame(
        originals, original_predictions, include_text=True
    )
    variant_predictions = _predict_all(
        variants.transformed_text.tolist(), baseline, predictors, batch_size
    )
    auto_predictions = _attach_original_predictions(
        variants, original_frame, variant_predictions
    )
    auto_predictions["true_label"] = variants.label.to_numpy(dtype=np.int64)
    auto_predictions["category"] = variants.category.to_numpy()
    auto_predictions["transformation_type"] = variants.transformation_type.to_numpy()
    auto_summary, review_summary, overall_metrics = _automatic_summaries(
        auto_predictions
    )

    length_variants, rejected_length = _generate_length_variants(originals)
    length_prediction_arrays = (
        _predict_all(
            length_variants.transformed_text.tolist(), baseline, predictors, batch_size
        )
        if not length_variants.empty
        else {}
    )
    length_predictions = (
        _attach_original_predictions(
            length_variants, original_frame, length_prediction_arrays
        )
        if length_prediction_arrays
        else pd.DataFrame()
    )
    length_predictions["true_label"] = length_variants.label.to_numpy(dtype=np.int64)
    length_predictions["category"] = "information_loss"
    length_predictions["transformation_type"] = (
        length_variants.transformation_type.to_numpy()
    )
    length_summary_rows = []
    if not length_predictions.empty:
        for name, group in length_predictions.groupby("transformation_type", sort=True):
            for key in MODEL_KEYS:
                length_summary_rows.append(
                    _summarize_automatic_group(
                        group, key, group_name="information_loss_type", group_value=name
                    )
                )
    length_summary = pd.DataFrame(length_summary_rows)

    challenge_prediction_arrays = _predict_all(
        challenge.text.tolist(), baseline, predictors, batch_size
    )
    challenge_predictions = _prediction_frame(challenge, challenge_prediction_arrays)
    semantic_summary, semantic_metrics, semantic_failures = _semantic_summaries(
        challenge_predictions, challenge
    )
    pairwise_summary, disagreements = _pairwise_agreement(
        [
            ("automatic_perturbations", auto_predictions),
            ("semantic_challenge", challenge_predictions),
        ]
    )
    wrong_rows = []
    variant_details = variants.set_index("sample_id")
    for key in MODEL_KEYS:
        wrong = auto_predictions[
            auto_predictions[f"{key}_prediction"] != auto_predictions.true_label
        ]
        wrong_rows.extend(
            {
                "track": "automatic_perturbations",
                "sample_id": row.sample_id,
                "review_id": row.review_id,
                "category": row.category,
                "transformation_type": row.transformation_type,
                "model": key,
                "true_label": int(row.true_label),
                "predicted_label": int(getattr(row, f"{key}_prediction")),
                "positive_probability": float(
                    getattr(row, f"{key}_positive_probability")
                ),
                "original_text": variant_details.at[row.sample_id, "original_text"],
                "transformed_text": variant_details.at[
                    row.sample_id, "transformed_text"
                ],
                "transformation_parameters": variant_details.at[
                    row.sample_id, "transformation_parameters"
                ],
            }
            for row in wrong.itertuples(index=False)
        )
    failures = pd.concat(
        [pd.DataFrame(wrong_rows), semantic_failures], ignore_index=True
    )

    hashes_after = _directory_hashes(sources)
    if hashes_after != hashes_before:
        raise RuntimeError(
            "A frozen model artifact changed during robustness evaluation."
        )

    output = Path(output_root)
    if not output.is_absolute():
        output = root / output
    output = (
        output.resolve()
        / f"imdb-robustness-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S.%fZ')}"
    )
    directories = (
        "automatic_perturbations",
        "semantic_challenge",
        "cross_model",
        "plots",
    )
    for name in directories:
        (output / name).mkdir(parents=True, exist_ok=False)

    automatic_dir = output / "automatic_perturbations"
    variants.to_csv(automatic_dir / "variants.csv", index=False)
    rejected.to_csv(automatic_dir / "rejected_variants.csv", index=False)
    original_frame.to_csv(automatic_dir / "original_predictions.csv", index=False)
    auto_predictions.to_csv(automatic_dir / "predictions.csv", index=False)
    auto_summary.to_csv(automatic_dir / "transformation_summary.csv", index=False)
    review_summary.to_csv(automatic_dir / "review_level_summary.csv", index=False)
    length_variants.to_csv(
        automatic_dir / "length_sensitivity_variants.csv", index=False
    )
    rejected_length.to_csv(
        automatic_dir / "length_sensitivity_rejections.csv", index=False
    )
    length_predictions.to_csv(
        automatic_dir / "length_sensitivity_predictions.csv", index=False
    )
    length_summary.to_csv(automatic_dir / "length_summary.csv", index=False)

    semantic_dir = output / "semantic_challenge"
    challenge_predictions.to_csv(semantic_dir / "predictions.csv", index=False)
    semantic_summary.to_csv(semantic_dir / "category_summary.csv", index=False)
    semantic_failures.to_csv(semantic_dir / "failure_examples.csv", index=False)
    all_wrong = []
    for _, row in challenge_predictions.iterrows():
        wrong_keys = [
            key for key in MODEL_KEYS if row[f"{key}_prediction"] != row.true_label
        ]
        if len(wrong_keys) == len(MODEL_KEYS):
            wrong_predictions = {int(row[f"{key}_prediction"]) for key in wrong_keys}
            all_wrong.append(
                {
                    "sample_id": row.sample_id,
                    "category": row.category,
                    "true_label": int(row.true_label),
                    "wrong_models": ",".join(wrong_keys),
                    "models_all_wrong_same_prediction": len(wrong_predictions) == 1,
                    "shared_wrong_prediction": (
                        next(iter(wrong_predictions))
                        if len(wrong_predictions) == 1
                        else None
                    ),
                }
            )
    pd.DataFrame(all_wrong).to_csv(semantic_dir / "all_models_wrong.csv", index=False)

    cross_dir = output / "cross_model"
    pairwise_summary.to_csv(cross_dir / "pairwise_agreement.csv", index=False)
    disagreements.to_csv(cross_dir / "prediction_disagreements.csv", index=False)
    failures.to_csv(cross_dir / "failure_examples.csv", index=False)
    matrix = auto_summary[auto_summary.summary_level == "transformation_type"][
        ["model", "group", "robustness_accuracy_review_level"]
    ].pivot(index="model", columns="group", values="robustness_accuracy_review_level")
    matrix.to_csv(cross_dir / "transformation_accuracy_matrix.csv")

    plots = _write_robustness_plots(
        output, auto_summary, challenge_predictions, pairwise_summary, semantic_summary
    )
    automatic_metrics = {
        "aggregation_method": {
            "transformation_type": (
                "one generated variant per source review and type; paired original "
                "and transformed metrics use exactly the same unique review IDs"
            ),
            "category": (
                "per-review majority vote across variants; ties retain that review's "
                "original prediction; pooled variant metrics are descriptive only"
            ),
            "overall": (
                "per-review majority vote across label-preserving variants; repeated "
                "variants do not count as independent reviews"
            ),
            "macro_review_statistics": (
                "review-level means of correctness and prediction-flip rates "
                "across each review's variants"
            ),
        },
        "original_validation_sample": {
            key: _metric_values(
                original_frame.true_label.to_numpy(dtype=np.int64),
                original_frame[f"{key}_prediction"].to_numpy(dtype=np.int64),
                original_frame[f"{key}_positive_probability"].to_numpy(
                    dtype=np.float64
                ),
            )
            for key in MODEL_KEYS
        },
        "overall_review_level": overall_metrics,
        "transformation_type_rows": len(variants),
        "unique_source_reviews": int(originals.review_id.nunique()),
        "rejected_candidate_count": len(rejected),
        "missing_transformation_types": missing_types,
        "missing_transformation_categories": missing_categories,
        "length_sensitivity": {
            "separate_information_loss_track": True,
            "rows": len(length_variants),
            "summary": length_summary.to_dict(orient="records"),
            "warning": (
                "Truncation and sentence ablation may remove sentiment-bearing "
                "evidence; labels are retained only for sensitivity analysis."
            ),
        },
    }
    combined_metrics = {
        "automatic_perturbations": automatic_metrics,
        "semantic_challenge": {
            "dataset_is_separate_from_automatic_track": True,
            "not_official_test_evaluation": True,
            "dataset_summary": challenge_info,
            "models": semantic_metrics,
        },
    }
    configuration = {
        "seed": seed,
        "validation_seed": SEED,
        "validation_fraction": VALIDATION_SIZE,
        "sample_limit": sample_limit,
        "max_variants_per_review": max_variants_per_review,
        "inference_batch_size": batch_size,
        "device": str(selected_device),
        "label_preserving_transformation_types": list(TRANSFORMATION_TYPES),
        "length_sensitivity_types": sorted(
            length_variants.transformation_type.unique().tolist()
        ),
        "semantic_categories": list(CHALLENGE_CATEGORIES),
    }
    (output / "automatic_perturbations" / "metrics.json").write_text(
        json.dumps(automatic_metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output / "semantic_challenge" / "metrics.json").write_text(
        json.dumps(combined_metrics["semantic_challenge"], indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    (output / "metrics.json").write_text(
        json.dumps(combined_metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output / "benchmark_config.json").write_text(
        json.dumps(configuration, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    metadata = {
        "phase": 15,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "output_directory": str(output),
        "validation_only_for_automatic_source": True,
        "official_test_dataset_loaded": False,
        "phase13_test_predictions_used": False,
        "model_training_or_tuning_performed": False,
        "preprocessing_refit": False,
        "checkpoint_files_modified": False,
        "source_artifact_hashes_before": hashes_before,
        "source_artifacts_unchanged": hashes_before == hashes_after,
        "source_phase12_comparison": str(phase12_path),
        "source_artifacts": {key: str(path) for key, path in sources.items()},
        "validation_split": {
            "source": "official labeled IMDb training split only",
            "rows": len(validation),
            "sampled_rows": len(originals),
            "review_fingerprint_sha256": _fingerprint(validation),
            "sampled_review_ids_sha256": hashlib.sha256(
                "\n".join(originals.review_id).encode()
            ).hexdigest(),
        },
        "automatic_track": {
            "sampled_reviews": len(originals),
            "accepted_variants": len(variants),
            "rejected_transformations": len(rejected),
            "missing_types": missing_types,
            "category_counts": variants.category.value_counts().to_dict(),
            "type_counts": variants.transformation_type.value_counts().to_dict(),
        },
        "semantic_challenge": {
            **challenge_info,
            "provenance": (
                "manually curated author-created examples; not independently "
                "human-validated"
            ),
            "exact_normalized_training_overlap": 0,
            "external_dataset_downloaded": False,
            "external_dataset_decision": (
                "Not needed: the deterministic IMDb validation perturbation track "
                "and independent balanced semantic set cover the two distinct "
                "robustness questions without combining external domains."
            ),
        },
        "software_versions": {
            "python": __import__("platform").python_version(),
            "numpy": np.__version__,
            "pandas": pd.__version__,
            "scikit_learn": sklearn.__version__,
            "torch": torch.__version__,
        },
        "limitations": [
            (
                "Automatic text edits are screened by a small sentiment/negation "
                "lexicon and are not guaranteed to preserve labels."
            ),
            (
                "Truncation and sentence ablation are reported separately because "
                "they can remove sentiment-bearing context."
            ),
            (
                "The semantic challenge set is author-created, balanced and "
                "independently unreviewed; its scores are descriptive and do not "
                "replace the official test evaluation."
            ),
            "Single-run descriptive comparisons do not establish significance.",
        ],
        "plot_files": plots,
    }
    (output / "metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output / "comparison.json").write_text(
        json.dumps(
            {
                "models": list(MODEL_KEYS),
                "automatic_perturbations": overall_metrics,
                "semantic_challenge": semantic_metrics,
                "tracks_combined_into_single_metric": False,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "output_directory": str(output),
        "metadata": metadata,
        "metrics": combined_metrics,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-directory", default="datasets/aclImdb")
    parser.add_argument(
        "--challenge-path", default="data/robustness/semantic_challenge.csv"
    )
    parser.add_argument("--output-root", default="outputs/robustness")
    parser.add_argument("--sample-limit", type=int, default=250)
    parser.add_argument(
        "--max-variants-per-review", type=int, default=len(TRANSFORMATION_TYPES)
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    args = parser.parse_args()
    if args.device == "cpu":
        torch.set_num_threads(1)
    result = run_robustness_evaluation(
        dataset_directory=args.dataset_directory,
        challenge_path=args.challenge_path,
        output_root=args.output_root,
        sample_limit=args.sample_limit,
        max_variants_per_review=args.max_variants_per_review,
        seed=args.seed,
        batch_size=args.batch_size,
        device=args.device,
    )
    print(
        json.dumps(
            {
                "output_directory": result["output_directory"],
                "automatic_perturbations": result["metadata"]["automatic_track"],
                "semantic_challenge": result["metadata"]["semantic_challenge"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
