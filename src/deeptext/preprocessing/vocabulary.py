"""A deterministic token vocabulary with stable padding and unknown IDs."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Iterable, Mapping

from deeptext.preprocessing.tokenizer import tokenize_text

PAD_TOKEN = "<pad>"
UNK_TOKEN = "<unk>"
PREPROCESSING_VERSION = "html-lowercase-whitespace-punctuation-v1"
DEFAULT_MAX_VOCAB_SIZE = 30_000
DEFAULT_MIN_TOKEN_FREQUENCY = 1


class Vocabulary:
    """Map tokens to IDs, reserving 0 for padding and 1 for unknown tokens."""

    def __init__(self) -> None:
        self._tokens = [PAD_TOKEN, UNK_TOKEN]
        self._token_to_id = {PAD_TOKEN: 0, UNK_TOKEN: 1}
        self._is_fitted = False
        self._max_size: int | None = None
        self._min_frequency: int | None = None

    @classmethod
    def build(
        cls,
        training_texts: Iterable[str],
        max_size: int = DEFAULT_MAX_VOCAB_SIZE,
        min_frequency: int = DEFAULT_MIN_TOKEN_FREQUENCY,
    ) -> Vocabulary:
        """Build from training reviews only, ordered by frequency then token.

        ``max_size`` includes the two reserved special tokens. Tied token
        frequencies are resolved alphabetically for deterministic IDs.
        """
        if isinstance(training_texts, str):
            raise TypeError("training_texts must be an iterable of review strings.")
        if isinstance(max_size, bool) or not isinstance(max_size, int) or max_size < 2:
            raise ValueError("max_size must be an integer of at least 2.")
        if (
            isinstance(min_frequency, bool)
            or not isinstance(min_frequency, int)
            or min_frequency < 1
        ):
            raise ValueError("min_frequency must be a positive integer.")

        token_counts: Counter[str] = Counter()
        review_count = 0
        for review in training_texts:
            if not isinstance(review, str):
                raise TypeError("training_texts must contain only strings.")
            review_count += 1
            token_counts.update(tokenize_text(review))

        if review_count == 0:
            raise ValueError("training_texts must contain at least one review.")
        if not token_counts:
            raise ValueError("training_texts did not contain any tokens.")

        candidates = [
            (token, frequency)
            for token, frequency in token_counts.items()
            if frequency >= min_frequency
        ]
        candidates.sort(key=lambda item: (-item[1], item[0]))
        selected_tokens = [token for token, _ in candidates[: max_size - 2]]

        vocabulary = cls()
        vocabulary._set_tokens(
            [PAD_TOKEN, UNK_TOKEN, *selected_tokens], max_size, min_frequency
        )
        return vocabulary

    def _set_tokens(self, tokens: list[str], max_size: int, min_frequency: int) -> None:
        self._tokens = tokens
        self._token_to_id = {token: index for index, token in enumerate(tokens)}
        self._is_fitted = True
        self._max_size = max_size
        self._min_frequency = min_frequency

    def _require_fitted(self) -> None:
        if not self._is_fitted:
            raise RuntimeError("Vocabulary has not been built or loaded yet.")

    @property
    def size(self) -> int:
        """Number of tokens, including ``<pad>`` and ``<unk>``."""
        self._require_fitted()
        return len(self._tokens)

    @property
    def max_size(self) -> int:
        self._require_fitted()
        assert self._max_size is not None
        return self._max_size

    @property
    def min_frequency(self) -> int:
        self._require_fitted()
        assert self._min_frequency is not None
        return self._min_frequency

    @property
    def pad_id(self) -> int:
        return 0

    @property
    def unk_id(self) -> int:
        return 1

    @property
    def tokens(self) -> tuple[str, ...]:
        self._require_fitted()
        return tuple(self._tokens)

    @property
    def token_to_id(self) -> Mapping[str, int]:
        """Return an independent copy of the token-to-ID mapping."""
        self._require_fitted()
        return dict(self._token_to_id)

    def id_for_token(self, token: str) -> int:
        """Look up a token, mapping tokens outside the vocabulary to ``<unk>``."""
        self._require_fitted()
        if not isinstance(token, str):
            raise TypeError("token must be a string.")
        return self._token_to_id.get(token, self.unk_id)

    def token_for_id(self, token_id: int) -> str:
        """Return the token for an integer ID or raise for an invalid ID."""
        self._require_fitted()
        if isinstance(token_id, bool) or not isinstance(token_id, int):
            raise TypeError("token_id must be an integer.")
        if token_id < 0 or token_id >= len(self._tokens):
            raise IndexError(f"token_id is outside the vocabulary: {token_id}")
        return self._tokens[token_id]

    def to_dict(self) -> dict[str, object]:
        """Return a transparent, JSON-compatible vocabulary state."""
        self._require_fitted()
        return {
            "format_version": 1,
            "preprocessing_version": PREPROCESSING_VERSION,
            "special_tokens": {"pad": PAD_TOKEN, "unk": UNK_TOKEN},
            "max_size": self.max_size,
            "min_frequency": self.min_frequency,
            "tokens": self._tokens.copy(),
        }

    def save(self, path: str | Path) -> Path:
        """Save vocabulary state as readable UTF-8 JSON."""
        destination = Path(path).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return destination

    @classmethod
    def load(cls, path: str | Path) -> Vocabulary:
        """Load a vocabulary JSON file and validate its token mapping."""
        source = Path(path).expanduser()
        state = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("Vocabulary state must be a JSON object.")
        if state.get("format_version") != 1:
            raise ValueError("Unsupported vocabulary format version.")
        if state.get("preprocessing_version") != PREPROCESSING_VERSION:
            raise ValueError("Vocabulary preprocessing version does not match.")
        if state.get("special_tokens") != {"pad": PAD_TOKEN, "unk": UNK_TOKEN}:
            raise ValueError("Vocabulary special-token definitions do not match.")

        tokens = state.get("tokens")
        max_size = state.get("max_size")
        min_frequency = state.get("min_frequency")
        if (
            not isinstance(tokens, list)
            or len(tokens) < 2
            or tokens[:2] != [PAD_TOKEN, UNK_TOKEN]
            or not all(isinstance(token, str) for token in tokens)
            or len(set(tokens)) != len(tokens)
        ):
            raise ValueError(
                "Vocabulary tokens must be unique and start with <pad>, <unk>."
            )
        if (
            isinstance(max_size, bool)
            or not isinstance(max_size, int)
            or max_size < len(tokens)
        ):
            raise ValueError("Vocabulary max_size is invalid for its token list.")
        if (
            isinstance(min_frequency, bool)
            or not isinstance(min_frequency, int)
            or min_frequency < 1
        ):
            raise ValueError("Vocabulary min_frequency must be a positive integer.")

        vocabulary = cls()
        vocabulary._set_tokens(tokens, max_size, min_frequency)
        return vocabulary
