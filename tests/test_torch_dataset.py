import pytest
import torch
from torch.utils.data import RandomSampler, SequentialSampler

from deeptext.data.torch_dataset import TextReviewDataset, create_data_loaders
from deeptext.preprocessing.vocabulary import Vocabulary


@pytest.fixture
def vocabulary() -> Vocabulary:
    return Vocabulary.build(
        ["excellent wonderful movie", "awful boring movie", "not very good"]
    )


def test_text_review_dataset_returns_expected_tensors_and_lengths(
    vocabulary: Vocabulary,
) -> None:
    dataset = TextReviewDataset(
        ["excellent movie", "awful unknownword"],
        [1, 0],
        vocabulary,
        max_length=5,
    )

    positive_sample = dataset[0]
    negative_sample = dataset[1]
    assert len(dataset) == 2
    assert positive_sample["input_ids"].shape == (5,)
    assert positive_sample["input_ids"].dtype == torch.long
    assert positive_sample["label"].shape == torch.Size([])
    assert positive_sample["label"].item() == 1
    assert negative_sample["label"].item() == 0
    assert negative_sample["length"].item() == 2
    assert positive_sample["length"].item() == 2
    assert negative_sample["input_ids"][1].item() == vocabulary.unk_id


def test_text_review_dataset_supports_empty_dataset(vocabulary: Vocabulary) -> None:
    dataset = TextReviewDataset([], [], vocabulary, max_length=4)

    assert len(dataset) == 0
    assert dataset.max_length == 4


def test_text_review_dataset_rejects_mismatched_or_invalid_inputs(
    vocabulary: Vocabulary,
) -> None:
    with pytest.raises(ValueError, match="equal lengths"):
        TextReviewDataset(["review"], [], vocabulary)
    with pytest.raises(ValueError, match="binary integers"):
        TextReviewDataset(["review"], [2], vocabulary)
    with pytest.raises(ValueError, match="one-dimensional"):
        TextReviewDataset(["review", "another"], [[0], [1]], vocabulary)  # type: ignore[list-item]
    with pytest.raises(ValueError, match="positive integer"):
        TextReviewDataset(["review"], [1], vocabulary, max_length=0)


def test_dataloaders_batch_shapes_and_ordering(vocabulary: Vocabulary) -> None:
    reviews = ["excellent movie", "awful movie", "not good", "wonderful story"]
    labels = [1, 0, 0, 1]
    training_dataset = TextReviewDataset(reviews, labels, vocabulary, max_length=6)
    validation_dataset = TextReviewDataset(reviews, labels, vocabulary, max_length=6)

    training_loader, validation_loader = create_data_loaders(
        training_dataset,
        validation_dataset,
        batch_size=3,
        device="cpu",
    )

    first_training_batch = next(iter(training_loader))
    assert isinstance(training_loader.sampler, RandomSampler)
    assert isinstance(validation_loader.sampler, SequentialSampler)
    assert training_loader.num_workers == 0
    assert training_loader.pin_memory is False
    assert first_training_batch["input_ids"].shape == (3, 6)
    assert first_training_batch["label"].shape == (3,)
    assert first_training_batch["length"].shape == (3,)
    assert first_training_batch["input_ids"].dtype == torch.long
    assert first_training_batch["label"].dtype == torch.long

    validation_batches = list(validation_loader)
    observed_labels = torch.cat([batch["label"] for batch in validation_batches])
    assert torch.equal(observed_labels, torch.tensor(labels, dtype=torch.long))
    assert validation_batches[-1]["input_ids"].shape == (1, 6)


def test_training_shuffle_is_reproducible_and_validation_is_not_shuffled(
    vocabulary: Vocabulary,
) -> None:
    reviews = ["excellent movie", "awful movie", "not good", "wonderful story"]
    labels = [1, 0, 0, 1]
    first_dataset = TextReviewDataset(reviews, labels, vocabulary, max_length=6)
    second_dataset = TextReviewDataset(reviews, labels, vocabulary, max_length=6)

    first_train_loader, first_validation_loader = create_data_loaders(
        first_dataset, first_dataset, batch_size=2, seed=29
    )
    second_train_loader, second_validation_loader = create_data_loaders(
        second_dataset, second_dataset, batch_size=2, seed=29
    )

    assert list(first_train_loader.sampler) == list(second_train_loader.sampler)
    assert list(first_validation_loader.sampler) == [0, 1, 2, 3]
    assert list(second_validation_loader.sampler) == [0, 1, 2, 3]


def test_dataloader_rejects_empty_datasets_and_invalid_settings(
    vocabulary: Vocabulary,
) -> None:
    empty_dataset = TextReviewDataset([], [], vocabulary)
    nonempty_dataset = TextReviewDataset(["good"], [1], vocabulary)

    with pytest.raises(ValueError, match="both be non-empty"):
        create_data_loaders(empty_dataset, nonempty_dataset)
    with pytest.raises(ValueError, match="batch_size"):
        create_data_loaders(nonempty_dataset, nonempty_dataset, batch_size=0)
    with pytest.raises(ValueError, match="num_workers"):
        create_data_loaders(nonempty_dataset, nonempty_dataset, num_workers=-1)


def test_cuda_batch_transfer_when_available(vocabulary: Vocabulary) -> None:
    if not torch.cuda.is_available():
        pytest.skip("CUDA is unavailable in the installed PyTorch build.")

    dataset = TextReviewDataset(["excellent movie"], [1], vocabulary, max_length=4)
    training_loader, _ = create_data_loaders(
        dataset,
        dataset,
        batch_size=1,
        device="cuda",
    )
    batch = next(iter(training_loader))
    moved_batch = {name: tensor.to("cuda") for name, tensor in batch.items()}

    assert moved_batch["input_ids"].device.type == "cuda"
    assert moved_batch["label"].item() == 1
