"""Simple IMDb text normalization and deterministic tokenization."""

from __future__ import annotations

import re
import unicodedata
from html.parser import HTMLParser

_TOKEN_PATTERN = re.compile(r"\w+(?:['’]\w+)*|[^\w\s]", flags=re.UNICODE)


class _HTMLTextExtractor(HTMLParser):
    """Collect readable text and separate it at markup boundaries."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        self.parts.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.parts.append(" ")


def normalize_text(text: str) -> str:
    """Remove HTML markup, decode entities, lowercase, and collapse whitespace.

    Punctuation and negation words are preserved as useful sentiment signals.
    Empty and whitespace-only input normalizes to an empty string.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string.")

    parser = _HTMLTextExtractor()
    parser.feed(text)
    parser.close()
    plain_text = unicodedata.normalize("NFKC", " ".join(parser.parts)).lower()
    return " ".join(plain_text.split())


def tokenize_text(text: str) -> list[str]:
    """Normalize text and retain words, contractions, and punctuation tokens."""
    normalized = normalize_text(text)
    return _TOKEN_PATTERN.findall(normalized)
