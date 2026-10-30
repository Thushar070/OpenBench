# OpenBench

OpenBench is an educational IMDb sentiment-classification project. It includes
a TF-IDF and Logistic Regression baseline, reusable text preprocessing and
PyTorch data pipelines, and a trainable TextCNN with validation-based
checkpointing and raw-text inference. Recurrent models and the Streamlit
interface remain future work.

## Environment

Use Python 3.15.x and PowerShell from the project root. Reuse an existing
`.venv`; do not recreate it. For a new environment, select the installed
Python 3.15 interpreter explicitly. With the per-user Python installer, the
default path is:

```powershell
$python315 = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python315\python.exe'
if (-not (Test-Path $python315)) { throw 'Set $python315 to your Python 3.15 executable.' }
& $python315 -m venv .venv
```

Activate the environment and install the project with development tools:

```powershell
.\.venv\Scripts\Activate.ps1
python --version
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

PyTorch 2.15 or later is required for this Python 3.15 Windows setup. For an
NVIDIA GPU, install a compatible CUDA-enabled PyTorch wheel using the official
PyTorch instructions before installing the project. CPU-only PyTorch is also
supported. Do not install the full CUDA Toolkit unless a later project
requirement calls for it.

## Checks

```powershell
python -m pytest
ruff check .
python -m pip check
```

## IMDB dataset

The dataset is the Large Movie Review Dataset (aclImdb v1.0), originally
published by Stanford researchers with their ACL 2011 paper. The local archive
was obtained from a mirror. The original source and paper are documented at
[Stanford NLP](https://nlp.stanford.edu/~amaas/data/sentiment/) and the
[ACL Anthology](https://aclanthology.org/P11-1015/).

Place the archive at `datasets\aclImdb_v1.tar.gz`. From the project root, with
the existing `.venv` activated, safely extract it and validate the resulting
structure and review files:

```powershell
python -m deeptext.data.dataset --extract `
  --archive .\datasets\aclImdb_v1.tar.gz `
  --dataset-dir .\datasets\aclImdb

python -m deeptext.data.dataset --dataset-dir .\datasets\aclImdb
```

The loader maps negative reviews to `0` and positive reviews to `1`, loads
training and test data separately, and excludes unlabeled reviews. The
deterministic stratified validation split is taken only from training data;
the official test split remains separate. The dataset contents are ignored
and are not part of the project source. Validation reports exact duplicate
labeled review texts and keeps the source files unchanged.

## Traditional baseline

Run the reproducible TF-IDF and Logistic Regression comparison from the
project root:

```powershell
python -m deeptext.experiments.baseline `
  --dataset-dir .\datasets\aclImdb `
  --output-dir .\outputs\baseline `
  --seed 42
```

The runner creates a stratified 80/20 split from labeled training reviews,
compares word unigrams and unigrams plus bigrams at two regularization
settings, and selects by validation F1. It fits TF-IDF on training text only;
the official test set and unlabeled reviews are not used. Each run saves one
reloadable model pipeline and its JSON metadata under `outputs\baseline`.
Labels are `0` for negative and `1` for positive; confusion matrices use
`[[true_negative, false_positive], [false_negative, true_positive]]` order.

Phase 3 implements the traditional baseline and shared metrics. Text
preprocessing and the PyTorch data pipeline are implemented in Phase 4. No
neural model training has been started.

## Text preprocessing and PyTorch data pipeline

Phase 4 provides a small, consistent pipeline for future neural experiments:

- `normalize_text` removes HTML markup, decodes entities, lowercases text,
  normalizes Unicode, and collapses whitespace. Punctuation and negation are
  preserved; `tokenize_text` keeps punctuation and contractions as tokens.
- `Vocabulary.build` counts tokens from the supplied **training reviews only**.
  `<pad>` is ID `0`, `<unk>` is ID `1`; remaining tokens are ordered by
  descending frequency and then alphabetically. The maximum size includes the
  two special tokens. Vocabularies can be saved and loaded as JSON.
- `encode_text` maps unseen tokens to `<unk>`, truncates to the configured
  maximum length, and pads to a fixed size. It returns the padded IDs and the
  non-padding length after truncation. An empty review becomes all padding with
  length zero.
- `TextReviewDataset` pre-encodes each labeled review and returns a dictionary
  containing `input_ids`, scalar `label`, and scalar `length` tensors. Labels
  remain `0` for negative and `1` for positive.
- `create_data_loaders` shuffles training batches reproducibly and keeps
  validation order fixed. Defaults are batch size `32`, sequence length `200`,
  zero workers (simple for Windows), seed `42`, and pinned memory only when a
  CUDA device is requested and available. CPU use is supported.

Build the vocabulary after splitting the original labeled training partition.
Do not pass validation or official test text to `Vocabulary.build`.

```python
from pathlib import Path

from deeptext.data.dataset import load_imdb_split, split_training_data
from deeptext.data.torch_dataset import TextReviewDataset, create_data_loaders
from deeptext.preprocessing import Vocabulary

records = load_imdb_split(Path("datasets/aclImdb"), "train")
training_records, validation_records = split_training_data(
    records, validation_size=0.2, seed=42
)
vocabulary = Vocabulary.build(
    training_records["text"].tolist(), max_size=30_000, min_frequency=1
)
training_dataset = TextReviewDataset(
    training_records["text"].tolist(),
    training_records["label"].tolist(),
    vocabulary,
    max_length=200,
)
validation_dataset = TextReviewDataset(
    validation_records["text"].tolist(),
    validation_records["label"].tolist(),
    vocabulary,
    max_length=200,
)
training_loader, validation_loader = create_data_loaders(
    training_dataset, validation_dataset, batch_size=32, seed=42
)
batch = next(iter(training_loader))
print(batch["input_ids"].shape)  # (32, 200)
```

## TextCNN training and inference

Phase 5 adds a Kim-style TextCNN: a 100-dimensional embedding, parallel
1D convolution branches with filter widths 3, 4, and 5, max pooling over
windows that contain only real review tokens, dropout, and a two-logit output.
The model masks convolution windows overlapping padding; branches shorter than
their filter width contribute a zero vector, so empty and short reviews remain
valid inputs.

The reusable trainer uses Adam (`learning_rate=0.001`) and cross-entropy,
records loss, accuracy, positive-class precision/recall/F1 for training and
validation, and selects checkpoints by strict validation-F1 improvement.
Early stopping uses a configurable minimum F1 delta (default `0.001`) and
patience (default 3); checkpoint selection still retains the highest F1 even
when a smaller gain does not reset patience. `auto` selects CUDA when
available and otherwise CPU. Experiments use seed 42 and the same stratified
80/20 split of the labeled official training split as the baseline. Vocabulary
fitting uses only the training partition; the official test and unlabeled
reviews are not used.

Run the full experiment from PowerShell with the existing `.venv` activated:

```powershell
python -m deeptext.experiments.textcnn `
  --dataset-dir .\datasets\aclImdb `
  --output-dir .\outputs\textcnn `
  --device auto
```

Each run gets a unique output directory containing `best_model.pt` (a
weights-only state dictionary plus model configuration), `vocabulary.json`,
`history.json`, and `metadata.json`. The loader uses PyTorch's restricted
`weights_only=True` loading and is intended for trusted local artifacts.
The walkthrough in `notebooks/phase_5_textcnn.ipynb` loads the completed run,
plots its saved history, and demonstrates CPU inference without starting
training.
Load a checkpoint and predict from raw review text with:

```python
from deeptext.inference import load_textcnn_predictor

predictor = load_textcnn_predictor(
    "outputs/textcnn/<experiment-id>", device="cpu"
)
print(predictor.predict("A thoughtful and moving film."))
```

The full-data run on Python 3.15.2, PyTorch 2.15.0+cu132, and an NVIDIA RTX 3050 Laptop GPU
used 20,000 training and 5,000 validation reviews. It stopped after 5 epochs;
the best checkpoint was epoch 2 (validation accuracy 84.92%, precision 81.93%,
recall 89.60%, F1 85.59%). Training time was 39.00 seconds. The checkpoint
was reloaded on CPU and matched probabilities and predictions on 16 validation
reviews. These are validation results from one run, not official test results
or a claim of generalization. Artifacts are under
`outputs\textcnn\imdb-textcnn-20261030T122328.015737Z`.

Run the regression suite and Ruff checks with:

```powershell
New-Item -ItemType Directory -Path .tmp -Force | Out-Null
python -m pytest --basetemp .tmp\pytest-phase5
ruff check src\deeptext tests
ruff format --check src\deeptext tests
```
