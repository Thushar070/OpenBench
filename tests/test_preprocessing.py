from pathlib import Path

import pytest

from deeptext.preprocessing.encoding import encode_text
from deeptext.preprocessing.tokenizer import normalize_text, tokenize_text
from deeptext.preprocessing.vocabulary import Vocabulary


def test_normalization_handles_case_html_whitespace_and_negation() -> None:
    text = "  THIS\tfilm<br /><br>is <b>NOT</b> bad &amp; never boring!  "

    normalized = normalize_text(text)

    assert normalized == "this film is not bad & never boring!"
    assert "not" in tokenize_text(text)
    assert "never" in tokenize_text(text)
    assert "!" in tokenize_text(text)


@pytest.mark.parametrize("text", ["", "   \r\n\t", "<br><b></b>"])
def test_empty_or_markup_only_text_has_no_tokens(text: str) -> None:
    assert normalize_text(text) == ""
    assert tokenize_text(text) == []


def test_tokenizer_removes_other_markup_and_preserves_contractions() -> None:
    assert normalize_text("<p>It isn't <i>great</i>.</p>") == "it isn't great ."
    assert tokenize_text("It isn't great.") == ["it", "isn't", "great", "."]


def test_preprocessing_rejects_non_string_input() -> None:
    with pytest.raises(TypeError, match="must be a string"):
        normalize_text(None)  # type: ignore[arg-type]


def test_vocabulary_has_stable_special_tokens_and_training_only_content() -> None:
    training_texts = ["good good excellent", "bad terrible"]
    validation_only_token = "validationonlyword"

    vocabulary = Vocabulary.build(training_texts, max_size=10)

    assert vocabulary.pad_id == 0
    assert vocabulary.unk_id == 1
    assert vocabulary.token_for_id(0) == "<pad>"
    assert vocabulary.token_for_id(1) == "<unk>"
    assert vocabulary.id_for_token("good") == 2
    assert vocabulary.id_for_token(validation_only_token) == vocabulary.unk_id
    assert validation_only_token not in vocabulary.token_to_id
    assert vocabulary.size <= 10


def test_vocabulary_applies_frequency_and_size_limits_deterministically() -> None:
    training_texts = ["zebra apple banana good good good", "apple banana"]

    limited = Vocabulary.build(training_texts, max_size=5, min_frequency=1)
    frequent_only = Vocabulary.build(training_texts, max_size=20, min_frequency=2)
    repeated = Vocabulary.build(training_texts, max_size=5, min_frequency=1)

    assert limited.tokens == ("<pad>", "<unk>", "good", "apple", "banana")
    assert frequent_only.tokens == ("<pad>", "<unk>", "good", "apple", "banana")
    assert "zebra" not in frequent_only.token_to_id
    assert limited.tokens == repeated.tokens
    assert limited.max_size == 5
    assert frequent_only.min_frequency == 2


def test_vocabulary_rejects_empty_input_and_uninitialized_use() -> None:
    uninitialized = Vocabulary()
    with pytest.raises(RuntimeError, match="has not been built"):
        uninitialized.id_for_token("word")
    with pytest.raises(ValueError, match="at least one review"):
        Vocabulary.build([])
    with pytest.raises(ValueError, match="did not contain any tokens"):
        Vocabulary.build(["  ", "<br>"])


def test_vocabulary_round_trip_preserves_ids_and_configuration(tmp_path: Path) -> None:
    vocabulary = Vocabulary.build(
        ["great movie", "not bad"], max_size=20, min_frequency=1
    )
    path = vocabulary.save(tmp_path / "state" / "vocabulary.json")

    restored = Vocabulary.load(path)

    assert restored.tokens == vocabulary.tokens
    assert restored.token_to_id == vocabulary.token_to_id
    assert restored.max_size == 20
    assert restored.min_frequency == 1
    assert restored.id_for_token("unseen") == 1


def test_encoding_maps_unknown_words_pads_and_preserves_true_length() -> None:
    vocabulary = Vocabulary.build(["a wonderful film", "not a bad movie"])

    encoded, length = encode_text("wonderful unseen", vocabulary, max_length=5)

    assert encoded == [vocabulary.id_for_token("wonderful"), 1, 0, 0, 0]
    assert length == 2


def test_encoding_truncates_and_defines_empty_review_behavior() -> None:
    vocabulary = Vocabulary.build(["one two three four"])

    truncated, truncated_length = encode_text("one two three four", vocabulary, 3)
    empty, empty_length = encode_text("  ", vocabulary, 3)

    assert truncated == [
        vocabulary.id_for_token("one"),
        vocabulary.id_for_token("two"),
        vocabulary.id_for_token("three"),
    ]
    assert truncated_length == 3
    assert empty == [0, 0, 0]
    assert empty_length == 0


def test_encoding_rejects_invalid_length_and_uninitialized_vocabulary() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        encode_text("review", Vocabulary.build(["review"]), 0)
    with pytest.raises(RuntimeError, match="has not been built"):
        encode_text("", Vocabulary())
