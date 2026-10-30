# OpenBench

OpenBench is an educational text-classification project for comparing a
TF-IDF and Logistic Regression baseline with a Text CNN, vanilla RNN, LSTM,
and GRU. IMDB movie reviews are the primary dataset. Dataset loading,
validation, and a deterministic validation split are implemented; no models
have been trained and the Streamlit interface is still planned.

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

For an NVIDIA GPU, install a compatible CUDA-enabled PyTorch wheel using the
official PyTorch instructions before installing the project. CPU-only PyTorch
is also supported. Do not install the full CUDA Toolkit unless a later project
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
preprocessing and the PyTorch data pipeline are Phase 4; neural models and
the Streamlit interface remain future work.
