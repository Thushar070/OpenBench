# OpenBench

OpenBench is an educational IMDb sentiment-classification project. It includes
a TF-IDF + Logistic Regression baseline and four PyTorch neural models:
TextCNN, vanilla RNN, LSTM, and GRU. All five saved models have validation
comparisons and a unified final evaluation on the 25,000-review official test
split. The Phase 8 Streamlit dashboard is preserved but further UI work is
paused.

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
ruff check src\deeptext tests notebooks\phase_6_model_comparison.ipynb notebooks\phase_7_final_evaluation.ipynb notebooks\phase_13_final_test_evaluation.ipynb
ruff format --check src\deeptext tests notebooks\phase_6_model_comparison.ipynb notebooks\phase_7_final_evaluation.ipynb notebooks\phase_13_final_test_evaluation.ipynb
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
preprocessing and the PyTorch data pipeline are implemented in Phase 4.
Phase 5 adds the TextCNN training and inference path.

## Text preprocessing and PyTorch data pipeline

Phase 4 provides the small, consistent text pipeline used by the TextCNN:

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

## Phase 6 model comparison

Compare the saved Phase 3 Logistic Regression and Phase 5 TextCNN artifacts
on the same reconstructed validation split. The comparison reads only the
labeled IMDb training partition; it does not load the official test or
unlabeled reviews, retrain either model, or overwrite the original runs.

```powershell
python -m deeptext.experiments.comparison `
  --dataset-dir .\datasets\aclImdb `
  --baseline-run .\outputs\baseline\imdb-baseline-20261030T110055.976336Z `
  --textcnn-run .\outputs\textcnn\imdb-textcnn-20261030T122328.015737Z `
  --output-dir .\outputs\comparison
```

Each run saves JSON metrics and audit details, row-aligned validation
predictions, error-analysis examples, confusion matrices, metric and
performance plots, artifact sizes, and the saved TextCNN learning curves in a
new timestamped directory. Run `notebooks\phase_6_model_comparison.ipynb` for
the reviewable walkthrough. It loads existing artifacts and results; it does
not train models.

On the recorded 5,000-review validation split, Logistic Regression achieved
87.72% accuracy and 0.8790 F1; TextCNN achieved 84.92% accuracy and 0.8559 F1.
TextCNN recall was higher (89.60% vs. 89.20%) and it had fewer false negatives
(260 vs. 270), alongside more false positives. Exact duplicate review text
appears in both training and validation data (26 unique reviews across 28
validation rows), so validation scores may be optimistic. This is a single
validation comparison, not a final test evaluation. Historical training-time
measurements had different timing boundaries and are not directly comparable.

Use the commands in **Checks** to run the current test suite and Ruff checks.

## Phase 7 final held-out evaluation

Phase 7 evaluated the exact saved Phase 3 Logistic Regression pipeline and
Phase 5 TextCNN checkpoint on all 25,000 labeled reviews in the official IMDb
test split (12,500 negative and 12,500 positive). Neither model was retrained,
retuned, or selected using test results. The saved TF-IDF vectorizer and
TextCNN training vocabulary were reused. Both prediction arrays align with
the same test IDs in the loader's original order; original model artifact
hashes matched before and after evaluation.

To repeat the final evaluation into a new timestamped directory:

```powershell
python -m deeptext.experiments.final_evaluation `
  --dataset-dir .\datasets\aclImdb `
  --output-root .\outputs\final_evaluation
```

| Metric | TF-IDF + Logistic Regression | TextCNN |
|---|---:|---:|
| Accuracy | 88.064% | 84.536% |
| Precision | 87.423% | 81.687% |
| Recall | 88.920% | 89.032% |
| F1 | 88.165% | 85.201% |
| False positives | 1,599 | 2,495 |
| False negatives | 1,385 | 1,371 |

Validation minus test differences were -0.344, -0.788, +0.280, and -0.266
percentage points for Logistic Regression accuracy, precision, recall, and F1;
for TextCNN they were +0.384, +0.245, +0.568, and +0.393 points. These are
observed differences for these runs, not a complete measure of generalization
or a statistical significance test. The reconstructed validation split had
26 unique exact-text duplicates shared with training across 28 validation
rows; this may affect validation scores, but does not establish the cause of
any test difference.

The models both classified 19,660 test reviews correctly and both
misclassified 1,510 reviews. Logistic Regression alone was correct on 2,356
reviews; TextCNN alone was correct on 1,474. They predicted different classes
on 3,830 reviews. The result directory
`outputs\final_evaluation\imdb-final-evaluation-20261031T075001.786903Z`
contains full aligned predictions without review text, a classification
report, selected error examples with original test IDs and text snippets
truncated to 400 characters, metadata, and seven plots. The notebook
`notebooks\phase_7_final_evaluation.ipynb` verifies the saved results and
spot-checks both model artifacts without training.

The official test split has been used only for final reporting in Phases 7 and
13. Do not reuse it for model training, tuning, checkpoint selection,
preprocessing fitting, or additional model selection.

## Recurrent architecture experiments

Phases 9–11 completed vanilla RNN, LSTM, and GRU validation experiments. The
Phase 12 comparison covers Logistic Regression, TextCNN, vanilla RNN, LSTM,
and GRU on one shared validation split. Its runner consumes saved predictions,
checks validation row IDs against the deterministic split, and reports
validation metrics, pairwise error categories, artifact sizes, and optional
inference benchmarks. The recurrent training runner uses the labeled official
training partition only, reconstructs the seeded 20,000/5,000 split, fits
vocabulary on training text, selects checkpoints by validation F1, and writes a
new run directory. It does not load the official test split.

To run one architecture from the project root with the existing `.venv`:

```powershell
python -m deeptext.experiments.recurrent --architecture rnn --device auto
python -m deeptext.experiments.recurrent --architecture lstm --device auto
python -m deeptext.experiments.recurrent --architecture gru --device auto
```

Each run saves its model, training vocabulary, history, reproducibility
metadata, and ordered validation predictions under `outputs\recurrent\`.
The initial configuration uses 100-dimensional embeddings, 100 hidden units,
one unidirectional recurrent layer, dropout 0.5, sequence length 200, batch
size 32, seed 42, and validation-F1 early stopping. Run architectures in the
roadmap order; each full run can take several minutes on Windows.

Run the saved five-model validation comparison after the three recurrent runs
and the Phase 6 artifacts are present:

```powershell
python -m deeptext.experiments.neural_comparison
```

The comparison writes a timestamped JSON report under
`outputs\neural_comparison\`; pass `--no-benchmark` to skip inference timing.
The reviewable summary notebook is `notebooks\phase_12_neural_comparison.ipynb`.

## Phase 13 final test evaluation

Evaluate all five saved models on the official test split without retraining or
fitting preprocessing:

```powershell
python -m deeptext.experiments.phase_13_final_evaluation
```

This checks all 25,000 test IDs and labels, writes per-model predictions,
metrics, classification reports, confusion matrices, selected text examples,
pairwise agreement, model/artifact details, and CPU/CUDA inference benchmarks
to a new directory under `outputs\final_evaluation\`. Source model hashes are
checked before and after the run. The notebook
`notebooks\phase_13_final_test_evaluation.ipynb` reviews those saved outputs;
its optional full-evaluation cell is disabled by default to avoid repeating a
completed test run.

| Model | Test accuracy | Test F1 |
|---|---:|---:|
| TF-IDF + Logistic Regression | 88.064% | 88.165% |
| TextCNN | 84.536% | 85.201% |
| Vanilla RNN | 65.632% | 66.783% |
| LSTM | 81.644% | 82.225% |
| GRU | 84.800% | 84.672% |

Phase 13 reproduced Phase 7's Logistic Regression and TextCNN metrics within
1e-12 and all their test prediction labels exactly. Phase 13 is the first
official test evaluation for the three recurrent models. These single-run
results do not establish statistical significance or general superiority.

## Phase 14 model interpretability

Generate explanations for the five frozen models from the deterministic
validation partition only:

```powershell
python -m deeptext.experiments.interpretability
```

Analysis uses CPU by default. When CUDA is available, pass `--device cuda` to
run neural explanations on the GPU.

The command checks the Phase 12 training/validation fingerprints and saved
validation predictions, then writes a new timestamped directory under
`outputs\interpretability\`. Outputs include exact Logistic Regression
coefficient contributions, TextCNN filter windows, recurrent hidden-state
traces, validated LSTM/GRU gate values, batched `<unk>` token-window occlusion,
and cross-model pairwise explanations. The analysis does not train models,
modify checkpoints, or load the official test split. The notebook
`notebooks\phase_14_model_interpretability.ipynb` reviews the latest saved run
without rerunning the analysis by default. Occlusion scores are sensitivity
analyses and are not causal explanations.

## Phase 15 robustness evaluation

Run the robustness analysis on the five frozen models:

```powershell
python -m deeptext.experiments.robustness --device auto
```

The automatic perturbation and author-created semantic challenge tracks are
reported separately under `outputs\robustness\`. Automatic variants use a
balanced sample from Phase 12's deterministic validation partition; a separate
length-sensitivity track reports truncation and sentence ablation because
these may remove sentiment evidence. The semantic challenge set contains 200
balanced examples across ten categories and is documented in
`data\robustness\README.md`. Its labels are author-intended and not
independently adjudicated. No model is trained or tuned and the official test
split is not loaded. The saved-output notebook is
`notebooks\phase_15_robustness_testing.ipynb`; it does not rerun the evaluation
unless its explicit run flag is enabled.

## Local dashboard

With the existing `.venv` activated, start the Streamlit app from the project
root. Node.js/npm is needed only if you need to rebuild the bundled Three.js
scene asset:

```powershell
python -m streamlit run src\deeptext\dashboard\app.py
```

To rebuild the local scene bundle:

```powershell
npm ci --prefix frontend
npm run --prefix frontend build
```

The app reads saved experiment summaries under `outputs\` and does not load
the review dataset, load model checkpoints, or start training on startup. The
dashboard currently includes the verified Phase 7 test summary, comparison
chart, confusion matrices, saved experiment table, and interactive 3D pipeline
scene. Models, Experiments, Model Comparison, Analysis, Sentiment Playground,
Dataset, and About remain clearly identified as incremental placeholders.
