"""Convert normalized review text into fixed-length token ID sequences."""

from __future__ import annotations

from deeptext.preprocessing.tokenizer import tokenize_text
from deeptext.preprocessing.vocabulary import Vocabulary

DEFAULT_MAX_SEQUENCE_LENGTH = 200


def encode_text(
    text: str,
    vocabulary: Vocabulary,
    max_length: int = DEFAULT_MAX_SEQUENCE_LENGTH,
) -> tuple[list[int], int]:
    """Encode one review as padded IDs and its non-padding length.

    Long reviews are truncated before padding. Empty reviews return an all-pad
    sequence with length zero. The returned length is capped at ``max_length``.
    """
    if (
        isinstance(max_length, bool)
        or not isinstance(max_length, int)
        or max_length < 1
    ):
        raise ValueError("max_length must be a positive integer.")
    if not isinstance(vocabulary, Vocabulary):
        raise TypeError("vocabulary must be a built Vocabulary instance.")
    _ = vocabulary.size
    tokens = tokenize_text(text)
    token_ids = [vocabulary.id_for_token(token) for token in tokens[:max_length]]
    length = len(token_ids)
    padded_ids = token_ids + [vocabulary.pad_id] * (max_length - length)
    return padded_ids, length
