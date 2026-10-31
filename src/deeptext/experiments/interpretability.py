"""Validation-only explanations for the frozen OpenBench model artifacts.

This module never fits a model.  It reads the audited train/validation split,
loads the frozen Phase 3/5/9-11 checkpoints, and writes a separate Phase 14
artifact directory.  It intentionally has no code path that loads the test set.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import torch
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from sklearn.pipeline import Pipeline
from torch import Tensor
from torch.nn import functional as F

from deeptext.data.dataset import load_imdb_split, split_training_data
from deeptext.inference.predictor import (
    load_recurrent_predictor,
    load_textcnn_predictor,
)
from deeptext.models.baseline import load_baseline_artifacts
from deeptext.preprocessing.encoding import encode_text
from deeptext.preprocessing.tokenizer import tokenize_text

SEED = 42
VALIDATION_SIZE = 0.2
CELL_VALIDATION_ATOL = 1e-4
CELL_VALIDATION_RTOL = 5e-5
MODEL_KEYS = ("logistic_regression", "textcnn", "rnn", "lstm", "gru")
RUN_KEYS = {
    "logistic_regression": "baseline",
    "textcnn": "textcnn",
    "rnn": "rnn",
    "lstm": "lstm",
    "gru": "gru",
}
COLORS = {
    "green": "#18a558",
    "orange": "#f28e2b",
    "red": "#e45756",
    "teal": "#39a99b",
    "yellow": "#edc949",
}


def _fingerprint(frame: pd.DataFrame) -> str:
    digest = hashlib.sha256()
    for review_id, label, text in frame[["review_id", "label", "text"]].itertuples(
        index=False, name=None
    ):
        digest.update(
            f"{review_id}\0{int(label)}\0{hashlib.sha256(text.encode('utf-8')).hexdigest()}\n".encode()
        )
    return digest.hexdigest()


def _file_hashes(directory: Path) -> dict[str, str]:
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _select_examples(
    validation: pd.DataFrame,
    predictions: dict[str, tuple[np.ndarray, np.ndarray]],
    maximum: int = 8,
) -> list[dict[str, Any]]:
    """Pick deterministic validation examples with interpretable coverage."""
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
        raise ValueError("maximum must be a positive integer.")
    labels = validation.label.to_numpy(dtype=np.int64)
    baseline = predictions["logistic_regression"][0]
    tags: dict[int, list[str]] = {}

    def add(index: int | None, tag: str) -> None:
        if index is not None:
            tags.setdefault(index, []).append(tag)

    add(
        next((i for i, y in enumerate(labels) if y == 1 and baseline[i] == y), None),
        "correct_positive",
    )
    add(
        next((i for i, y in enumerate(labels) if y == 0 and baseline[i] == y), None),
        "correct_negative",
    )
    add(
        next((i for i, y in enumerate(labels) if y == 0 and baseline[i] == 1), None),
        "false_positive",
    )
    add(
        next((i for i, y in enumerate(labels) if y == 1 and baseline[i] == 0), None),
        "false_negative",
    )
    add(
        next(
            (
                i
                for i, row in enumerate(validation.text)
                if any(
                    t in {"but", "although", "however", "yet"}
                    for t in tokenize_text(row)
                )
            ),
            None,
        ),
        "mixed_or_contrasting_phrase",
    )
    add(
        next(
            (
                i
                for i in range(len(labels))
                if any(predictions[k][0][i] != labels[i] for k in MODEL_KEYS)
            ),
            None,
        ),
        "model_error",
    )
    add(
        next(
            (
                i
                for i in range(len(labels))
                if len({int(predictions[k][0][i]) for k in MODEL_KEYS}) > 1
            ),
            None,
        ),
        "model_disagreement",
    )
    for i in range(len(labels)):
        if len(tags) >= maximum:
            break
        if i not in tags:
            tags[i] = ["deterministic_validation_example"]
    return [
        {
            "row_index": i,
            "review_id": str(validation.iloc[i].review_id),
            "true_label": int(labels[i]),
            "selection_tags": sorted(set(reasons)),
        }
        for i, reasons in list(tags.items())[:maximum]
    ]


def _read_phase6_predictions(
    path: Path, validation: pd.DataFrame
) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    saved = pd.read_csv(path)
    if saved.review_id.tolist() != validation.review_id.tolist() or not np.array_equal(
        saved.true_label.to_numpy(dtype=np.int64),
        validation.label.to_numpy(dtype=np.int64),
    ):
        raise ValueError(
            "Phase 6 prediction rows do not match the deterministic validation split."
        )
    result = {
        "logistic_regression": (
            saved.baseline_prediction.to_numpy(dtype=np.int64),
            saved.baseline_positive_probability.to_numpy(dtype=np.float64),
        ),
        "textcnn": (
            saved.textcnn_prediction.to_numpy(dtype=np.int64),
            saved.textcnn_positive_probability.to_numpy(dtype=np.float64),
        ),
    }
    for key, (labels, probabilities) in result.items():
        if not np.isin(labels, (0, 1)).all():
            raise ValueError(f"Phase 6 {key} predictions contain an invalid label.")
        if (
            not np.isfinite(probabilities).all()
            or ((probabilities < 0) | (probabilities > 1)).any()
        ):
            raise ValueError(f"Phase 6 {key} probabilities are invalid.")
    return result


def _check_run_metadata(
    path: Path,
    key: str,
    train_fingerprint: str,
    validation_fingerprint: str,
    train_count: int,
    validation_count: int,
) -> dict[str, Any]:
    metadata = json.loads((path / "metadata.json").read_text(encoding="utf-8"))
    split = metadata.get("split", {})
    if (
        split.get("source") != "official labeled training split only"
        or split.get("random_state", metadata.get("seed")) != SEED
        or split.get("validation_fraction") != VALIDATION_SIZE
        or split.get("training_rows") != train_count
        or split.get("validation_rows") != validation_count
        or metadata.get("official_test_evaluated") is not False
    ):
        raise ValueError(
            f"{key} source run does not document the audited validation-only split."
        )
    if split.get(
        "training_partition_sha256", metadata.get("training_review_fingerprint_sha256")
    ) not in (None, train_fingerprint) or split.get(
        "validation_partition_sha256",
        metadata.get("validation_review_fingerprint_sha256"),
    ) not in (None, validation_fingerprint):
        raise ValueError(
            f"{key} metadata fingerprints do not match the reconstructed split."
        )
    return metadata


def _safe_name(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in value
    )


def _save_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _lr_explanations(
    pipeline: Pipeline,
    validation: pd.DataFrame,
    examples: list[dict[str, Any]],
    destination: Path,
) -> dict[str, Any]:
    vectorizer = pipeline.named_steps["tfidf"]
    classifier = pipeline.named_steps["logistic_regression"]
    terms = vectorizer.get_feature_names_out()
    weights = classifier.coef_[0]
    top_positive = np.argsort(weights)[-20:][::-1]
    top_negative = np.argsort(weights)[:20]
    pd.DataFrame(
        {
            "term": terms[top_positive],
            "coefficient": weights[top_positive],
            "absolute_coefficient": np.abs(weights[top_positive]),
        }
    ).to_csv(destination / "global_top_positive.csv", index=False)
    pd.DataFrame(
        {
            "term": terms[top_negative],
            "coefficient": weights[top_negative],
            "absolute_coefficient": np.abs(weights[top_negative]),
        }
    ).to_csv(destination / "global_top_negative.csv", index=False)
    records = []
    for example in examples:
        row = validation.iloc[example["row_index"]]
        sparse_row = vectorizer.transform([row.text])
        indices = sparse_row.indices
        values = sparse_row.data
        contribution = values * weights[indices]
        reconstructed = float(classifier.intercept_[0] + contribution.sum())
        actual = float(pipeline.decision_function([row.text])[0])
        if not math.isclose(reconstructed, actual, abs_tol=2e-6, rel_tol=1e-6):
            raise RuntimeError(
                "TF-IDF feature contributions failed to reconstruct "
                "Logistic Regression decision_function."
            )
        ranked = np.argsort(contribution)
        for rank, position in enumerate(
            list(ranked[:10]) + list(ranked[-10:][::-1]), start=1
        ):
            records.append(
                {
                    "review_id": example["review_id"],
                    "true_label": example["true_label"],
                    "intercept": float(classifier.intercept_[0]),
                    "decision_function": actual,
                    "term": terms[indices[position]],
                    "tfidf_value": float(values[position]),
                    "coefficient": float(weights[indices[position]]),
                    "contribution": float(contribution[position]),
                    "rank_in_negative_or_positive_tail": rank,
                }
            )
    pd.DataFrame(records).to_csv(
        destination / "review_feature_contributions.csv", index=False
    )
    return {
        "method": (
            "exact TF-IDF value × Logistic Regression coefficient; intercept "
            "plus all feature contributions reconstructs decision_function"
        ),
        "global_feature_count": int(len(terms)),
        "top_k": 20,
        "examples_reconstructed": len(examples),
        "all_reconstructions_validated": True,
    }


def _encode(predictor: Any, text: str) -> tuple[Tensor, Tensor, list[str]]:
    ids, length = encode_text(
        text, predictor.vocabulary, predictor.model.config.max_sequence_length
    )
    return (
        torch.tensor([ids], dtype=torch.long, device=predictor.device),
        torch.tensor([length], dtype=torch.long, device=predictor.device),
        tokenize_text(text)[:length],
    )


def _neural_probability(
    model: torch.nn.Module, inputs: Tensor, lengths: Tensor
) -> Tensor:
    return torch.softmax(model(inputs, lengths), dim=1)[:, 1]


def _occlusion(
    predictor: Any,
    examples: list[dict[str, Any]],
    validation: pd.DataFrame,
    destination: Path,
    *,
    max_windows: int = 100,
    batch_size: int = 128,
) -> list[dict[str, Any]]:
    if (
        isinstance(max_windows, bool)
        or not isinstance(max_windows, int)
        or max_windows < 1
    ):
        raise ValueError("max_windows must be a positive integer.")
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or batch_size < 1
    ):
        raise ValueError("batch_size must be a positive integer.")
    model, vocab = predictor.model, predictor.vocabulary
    records: list[dict[str, Any]] = []
    model.eval()
    with torch.inference_mode():
        for example in examples:
            row = validation.iloc[example["row_index"]]
            input_ids, lengths, tokens = _encode(predictor, row.text)
            length = int(lengths.item())
            original = float(_neural_probability(model, input_ids, lengths)[0].item())
            candidates: list[tuple[int, int]] = []
            for width in (1, 2, 3):
                starts = list(range(max(0, length - width + 1)))
                if len(starts) > max_windows:
                    starts = sorted(
                        set(
                            np.linspace(
                                0, len(starts) - 1, max_windows, dtype=int
                            ).tolist()
                        )
                    )
                candidates.extend((width, start) for start in starts)
            for batch_start in range(0, len(candidates), batch_size):
                part = candidates[batch_start : batch_start + batch_size]
                masked = input_ids.repeat(len(part), 1)
                for j, (width, start) in enumerate(part):
                    masked[j, start : start + width] = vocab.unk_id
                repeated_lengths = lengths.repeat(len(part))
                probabilities = (
                    _neural_probability(model, masked, repeated_lengths).cpu().tolist()
                )
                for (width, start), masked_probability in zip(
                    part, probabilities, strict=True
                ):
                    end = start + width
                    records.append(
                        {
                            "review_id": example["review_id"],
                            "true_label": example["true_label"],
                            "window_start": start,
                            "window_end_exclusive": end,
                            "window_size": width,
                            "tokens": " ".join(tokens[start:end]),
                            "original_positive_probability": original,
                            "occluded_positive_probability": float(masked_probability),
                            "positive_probability_drop": original
                            - float(masked_probability),
                            "mask_token": "<unk>",
                            "sequence_length_preserved": True,
                        }
                    )
    frame = pd.DataFrame(records)
    frame.to_csv(destination / "token_window_occlusion.csv", index=False)
    return records


def _cnn_activations(
    predictor: Any,
    examples: list[dict[str, Any]],
    validation: pd.DataFrame,
    destination: Path,
) -> None:
    model = predictor.model
    rows = []
    with torch.inference_mode():
        for example in examples:
            text = validation.iloc[example["row_index"]].text
            ids, lengths, tokens = _encode(predictor, text)
            embedded = model.embedding(ids).transpose(1, 2)
            pooled_branches = []
            for width, convolution in zip(
                model.config.filter_sizes, model.convolutions, strict=True
            ):
                values = F.relu(convolution(embedded))[0]
                valid_count = max(0, int(lengths[0]) - width + 1)
                if valid_count:
                    max_per_filter = values[:, :valid_count].max(dim=1).values
                    pooled_branches.append(max_per_filter)
                else:
                    pooled_branches.append(values.new_zeros(values.shape[0]))
                    continue
                selected_filters = torch.topk(
                    max_per_filter, min(10, max_per_filter.numel())
                ).indices
                for filter_index_tensor in selected_filters:
                    filter_index = int(filter_index_tensor.item())
                    position = int(values[filter_index, :valid_count].argmax().item())
                    rows.append(
                        {
                            "review_id": example["review_id"],
                            "filter_size": width,
                            "filter_index": filter_index,
                            "activation": float(values[filter_index, position].item()),
                            "window_start": position,
                            "window_tokens": " ".join(
                                tokens[position : position + width]
                            ),
                        }
                    )
            reconstructed = model.classifier(model.dropout(torch.cat(pooled_branches)))[
                None, :
            ]
            native = model(ids, lengths)
            if not torch.allclose(reconstructed, native, atol=2e-6, rtol=1e-6):
                raise RuntimeError(
                    "TextCNN max-pooling trace failed to reproduce native logits."
                )
    pd.DataFrame(rows).to_csv(destination / "max_filter_activations.csv", index=False)


def _write_pairwise_explanations(output: Path, roster: list[dict[str, Any]]) -> None:
    """Pair prediction differences with each model's local evidence summary."""
    contributions = pd.read_csv(
        output / "logistic_regression" / "review_feature_contributions.csv"
    )
    neural_evidence: dict[str, dict[str, tuple[str, str]]] = {}
    for key in ("textcnn", "rnn", "lstm", "gru"):
        frame = pd.read_csv(output / key / "token_window_occlusion.csv")
        evidence: dict[str, tuple[str, str]] = {}
        for review_id, group in frame.groupby("review_id"):
            supportive = group.loc[group.positive_probability_drop.idxmax()]
            suppressive = group.loc[group.positive_probability_drop.idxmin()]
            evidence[str(review_id)] = (
                f"supports positive: {supportive.tokens} "
                f"({supportive.positive_probability_drop:+.4f})",
                f"suppresses positive: {suppressive.tokens} "
                f"({suppressive.positive_probability_drop:+.4f})",
            )
        neural_evidence[key] = evidence

    rows = []
    for item in roster:
        review_id = item["review_id"]
        review_terms = contributions[contributions.review_id == review_id]
        positive_terms = (
            review_terms[review_terms.contribution > 0]
            .nlargest(3, "contribution")
            .term.tolist()
        )
        negative_terms = (
            review_terms[review_terms.contribution < 0]
            .nsmallest(3, "contribution")
            .term.tolist()
        )
        evidence = {
            "logistic_regression": (
                f"positive terms: {', '.join(positive_terms)}; "
                f"negative terms: {', '.join(negative_terms)}"
            )
        }
        evidence.update(
            {
                key: "; ".join(neural_evidence[key].get(review_id, ()))
                for key in neural_evidence
            }
        )
        for left_index, left in enumerate(MODEL_KEYS):
            for right in MODEL_KEYS[left_index + 1 :]:
                rows.append(
                    {
                        "review_id": review_id,
                        "true_label": item["true_label"],
                        "left_model": left,
                        "left_prediction": item[f"{left}_prediction"],
                        "left_positive_probability": item[
                            f"{left}_positive_probability"
                        ],
                        "left_local_evidence": evidence[left],
                        "right_model": right,
                        "right_prediction": item[f"{right}_prediction"],
                        "right_positive_probability": item[
                            f"{right}_positive_probability"
                        ],
                        "right_local_evidence": evidence[right],
                        "predictions_agree": item[f"{left}_prediction"]
                        == item[f"{right}_prediction"],
                    }
                )
    pd.DataFrame(rows).to_csv(
        output / "cross_model" / "pairwise_explanation_comparison.csv",
        index=False,
    )


def _recurrent_trace(
    model: torch.nn.Module, inputs: Tensor, length: int, kind: str
) -> dict[str, Tensor]:
    """Run transparent single-layer cell equations with PyTorch parameter layout."""
    cell = model.recurrent
    if cell.num_layers != 1 or cell.bidirectional:
        raise ValueError(
            "Analysis equations support only the saved single-layer "
            "unidirectional models."
        )
    x = model.embedding(inputs)[0, :length]
    hidden_dim = cell.hidden_size
    h = x.new_zeros(hidden_dim)
    c = x.new_zeros(hidden_dim)
    hidden, gate_rows, cell_states = [], [], []
    for step in range(length):
        input_gates = F.linear(x[step], cell.weight_ih_l0, cell.bias_ih_l0)
        recurrent_gates = F.linear(h, cell.weight_hh_l0, cell.bias_hh_l0)
        if kind == "rnn":
            h = torch.tanh(input_gates + recurrent_gates)
        elif kind == "lstm":
            i, f, g, o = (input_gates + recurrent_gates).chunk(4)
            i, f, o, g = i.sigmoid(), f.sigmoid(), o.sigmoid(), g.tanh()
            c = f * c + i * g
            h = o * c.tanh()
            cell_states.append(c)
            gate_rows.append(torch.stack((i.mean(), f.mean(), g.mean(), o.mean())))
        elif kind == "gru":
            ir, iz, inn = input_gates.chunk(3)
            hr, hz, hn = recurrent_gates.chunk(3)
            reset = (ir + hr).sigmoid()
            update = (iz + hz).sigmoid()
            candidate = (inn + reset * hn).tanh()
            h = (1 - update) * candidate + update * h
            gate_rows.append(
                torch.stack((reset.mean(), update.mean(), candidate.mean()))
            )
        else:
            raise ValueError(f"Unsupported recurrent cell: {kind}")
        hidden.append(h)
    result = {"hidden": torch.stack(hidden) if hidden else x.new_zeros((0, hidden_dim))}
    if gate_rows:
        result["gates"] = torch.stack(gate_rows)
    if cell_states:
        result["cell_states"] = torch.stack(cell_states)
    return result


def _recurrent_explanations(
    key: str,
    predictor: Any,
    examples: list[dict[str, Any]],
    validation: pd.DataFrame,
    destination: Path,
) -> bool | None:
    model = predictor.model
    state_rows, gate_rows, dimension_rows = [], [], []
    gate_valid: bool | None = None
    gate_name = {
        "lstm": ["input", "forget", "candidate", "output"],
        "gru": ["reset", "update", "candidate"],
    }.get(key, [])
    for example in examples:
        ids, lengths, tokens = _encode(
            predictor, validation.iloc[example["row_index"]].text
        )
        length = int(lengths.item())
        if length == 0:
            continue
        trace = _recurrent_trace(model, ids, length, key)
        logits = model(ids, lengths)[0]
        reproduced = model.classifier(trace["hidden"][-1])
        native_output, native_state = model.recurrent(model.embedding(ids[:, :length]))
        valid = torch.allclose(
            trace["hidden"],
            native_output[0],
            atol=CELL_VALIDATION_ATOL,
            rtol=CELL_VALIDATION_RTOL,
        ) and torch.allclose(
            reproduced,
            logits,
            atol=CELL_VALIDATION_ATOL,
            rtol=CELL_VALIDATION_RTOL,
        )
        if key == "lstm" and isinstance(native_state, tuple):
            valid = valid and torch.allclose(
                trace["cell_states"][-1],
                native_state[1][-1],
                atol=CELL_VALIDATION_ATOL,
                rtol=CELL_VALIDATION_RTOL,
            )
        if key in {"lstm", "gru"}:
            gate_valid = valid if gate_valid is None else gate_valid and valid
        if key == "rnn" and not valid:
            raise RuntimeError(
                "Vanilla RNN hidden-state trace does not reproduce saved model logits."
            )
        selected_dimensions = torch.topk(
            trace["hidden"].var(dim=0, unbiased=False),
            min(12, model.config.hidden_dim),
        ).indices
        cell_states = trace.get("cell_states")
        for position, token in enumerate(tokens):
            hidden = trace["hidden"][position]
            state_record = {
                "review_id": example["review_id"],
                "position": position,
                "token": token,
                "hidden_l2_norm": float(hidden.norm().item()),
                "hidden_mean": float(hidden.mean().item()),
                "hidden_mean_absolute": float(hidden.abs().mean().item()),
            }
            if cell_states is not None:
                state_record["cell_state_l2_norm"] = float(
                    cell_states[position].norm().item()
                )
                state_record["cell_state_mean"] = float(
                    cell_states[position].mean().item()
                )
            state_rows.append(state_record)
            for dimension in selected_dimensions.tolist():
                dimension_rows.append(
                    {
                        "review_id": example["review_id"],
                        "position": position,
                        "token": token,
                        "hidden_dimension": dimension,
                        "activation": float(hidden[dimension].item()),
                    }
                )
            if "gates" in trace:
                for gate, value in zip(
                    gate_name, trace["gates"][position].tolist(), strict=True
                ):
                    gate_rows.append(
                        {
                            "review_id": example["review_id"],
                            "position": position,
                            "token": token,
                            "gate": gate,
                            "mean_activation": value,
                            "validated_against_native_model": bool(valid),
                        }
                    )
    pd.DataFrame(state_rows).to_csv(
        destination / "hidden_state_evolution.csv", index=False
    )
    pd.DataFrame(dimension_rows).to_csv(
        destination / "hidden_dimension_activations.csv", index=False
    )
    if gate_rows and gate_valid:
        pd.DataFrame(gate_rows).to_csv(
            destination / "gate_activations.csv", index=False
        )
    elif key in {"lstm", "gru"}:
        _save_json(
            destination / "gate_extraction_status.json",
            {
                "validated": False,
                "reason": (
                    "Analysis-only cell equations did not reproduce native "
                    "PyTorch logits; gate activations omitted."
                ),
            },
        )
    return gate_valid


def _plot_outputs(output: Path, model_data: dict[str, dict[str, Any]]) -> None:
    lr_dir = output / "logistic_regression"
    positive = pd.read_csv(lr_dir / "global_top_positive.csv").head(15).iloc[::-1]
    negative = pd.read_csv(lr_dir / "global_top_negative.csv").head(15).iloc[::-1]
    fig = Figure(figsize=(12, 7), constrained_layout=True)
    FigureCanvasAgg(fig)
    axes = fig.subplots(1, 2)
    axes[0].barh(positive.term, positive.coefficient, color=COLORS["green"])
    axes[1].barh(negative.term, negative.coefficient, color=COLORS["red"])
    axes[0].set_title("Top positive Logistic Regression features")
    axes[1].set_title("Top negative Logistic Regression features")
    fig.savefig(output / "plots" / "logistic_global_features.png", dpi=150)

    contributions = pd.read_csv(lr_dir / "review_feature_contributions.csv")
    for review_id, group in contributions.groupby("review_id"):
        positive_terms = group[group.contribution > 0].nlargest(8, "contribution")
        negative_terms = group[group.contribution < 0].nsmallest(8, "contribution")
        shown = pd.concat([negative_terms, positive_terms]).sort_values("contribution")
        if shown.empty:
            continue
        fig = Figure(figsize=(10, 6), constrained_layout=True)
        FigureCanvasAgg(fig)
        ax = fig.subplots()
        ax.barh(
            shown.term,
            shown.contribution,
            color=[
                COLORS["green"] if value > 0 else COLORS["red"]
                for value in shown.contribution
            ],
        )
        ax.set(
            title=f"Logistic Regression feature contributions — {review_id}",
            xlabel="TF-IDF value × coefficient",
            ylabel="Feature",
        )
        fig.savefig(
            output / "plots" / f"logistic_review_{_safe_name(review_id)}.png",
            dpi=150,
        )

    for key, info in model_data.items():
        if key == "textcnn":
            frame = pd.read_csv(output / key / "max_filter_activations.csv")
            if not frame.empty:
                shown = frame.sort_values("activation").tail(25)
                fig = Figure(figsize=(11, 6), constrained_layout=True)
                FigureCanvasAgg(fig)
                ax = fig.subplots()
                ax.barh(shown.window_tokens, shown.activation, color=COLORS["orange"])
                ax.set_title("TextCNN strongest validation-window filter activations")
                fig.savefig(output / "plots" / "textcnn_filter_windows.png", dpi=150)

        occlusion = pd.read_csv(output / key / "token_window_occlusion.csv")
        if not occlusion.empty:
            first_id = occlusion.review_id.iloc[0]
            review_windows = occlusion[occlusion.review_id == first_id]
            supportive = review_windows.nlargest(8, "positive_probability_drop")
            suppressive = review_windows.nsmallest(8, "positive_probability_drop")
            shown = pd.concat([suppressive, supportive]).sort_values(
                "positive_probability_drop"
            )
            labels = [f"{row.tokens} ({row.window_size})" for row in shown.itertuples()]
            fig = Figure(figsize=(11, 7), constrained_layout=True)
            FigureCanvasAgg(fig)
            ax = fig.subplots()
            ax.barh(
                labels,
                shown.positive_probability_drop,
                color=[
                    COLORS["green"] if value > 0 else COLORS["red"]
                    for value in shown.positive_probability_drop
                ],
            )
            ax.axvline(0, color="#555555", linewidth=0.8)
            ax.set(
                title=f"{key.upper()} <unk> window occlusion — {first_id}",
                xlabel="Original − masked positive-class probability",
                ylabel="Occluded token window (width)",
            )
            fig.savefig(output / "plots" / f"{key}_occlusion_rankings.png", dpi=150)

        if key in {"rnn", "lstm", "gru"}:
            frame = pd.read_csv(output / key / "hidden_state_evolution.csv")
            if not frame.empty:
                first_id = frame.review_id.iloc[0]
                shown = frame[frame.review_id == first_id]
                fig = Figure(figsize=(11, 4), constrained_layout=True)
                FigureCanvasAgg(fig)
                ax = fig.subplots()
                ax.plot(shown.position, shown.hidden_l2_norm, color=COLORS["teal"])
                ax.set(
                    title=f"{key.upper()} hidden-state magnitude by token",
                    xlabel="Token position",
                    ylabel="L2 norm",
                )
                fig.savefig(output / "plots" / f"{key}_hidden_state.png", dpi=150)

                if key == "lstm":
                    ax.plot(
                        shown.position,
                        shown.cell_state_l2_norm,
                        color=COLORS["orange"],
                        label="Cell state L2 norm",
                    )
                    ax.lines[0].set_label("Hidden state L2 norm")
                    ax.legend()
                    fig.savefig(
                        output / "plots" / "lstm_hidden_cell_state.png", dpi=150
                    )

            dimensions_path = output / key / "hidden_dimension_activations.csv"
            if dimensions_path.exists():
                dimensions = pd.read_csv(dimensions_path)
                if not dimensions.empty:
                    first_id = dimensions.review_id.iloc[0]
                    shown_dimensions = dimensions[dimensions.review_id == first_id]
                    matrix = shown_dimensions.pivot(
                        index="hidden_dimension",
                        columns="position",
                        values="activation",
                    )
                    fig = Figure(figsize=(12, 5), constrained_layout=True)
                    FigureCanvasAgg(fig)
                    ax = fig.subplots()
                    image = ax.imshow(matrix, aspect="auto", cmap="RdYlGn")
                    fig.colorbar(image, ax=ax, label="Hidden activation")
                    ax.set(
                        title=f"{key.upper()} selected hidden dimensions — {first_id}",
                        xlabel="Token position",
                        ylabel="Selected hidden dimension",
                        yticks=np.arange(len(matrix.index)),
                        yticklabels=matrix.index,
                    )
                    fig.savefig(
                        output / "plots" / f"{key}_hidden_state_heatmap.png",
                        dpi=150,
                    )

            gate_path = output / key / "gate_activations.csv"
            if gate_path.exists():
                gates = pd.read_csv(gate_path)
                if not gates.empty:
                    first_id = gates.review_id.iloc[0]
                    shown_gates = gates[gates.review_id == first_id]
                    fig = Figure(figsize=(10, 5), constrained_layout=True)
                    FigureCanvasAgg(fig)
                    ax = fig.subplots()
                    palette = [
                        COLORS["green"],
                        COLORS["orange"],
                        COLORS["red"],
                        COLORS["teal"],
                    ]
                    for color, (gate, values) in zip(
                        palette,
                        shown_gates.groupby("gate", sort=False),
                        strict=False,
                    ):
                        ax.plot(
                            values.position,
                            values.mean_activation,
                            color=color,
                            label=gate,
                        )
                    ax.set(
                        title=f"{key.upper()} validated gate activations — {first_id}",
                        xlabel="Token position",
                        ylabel="Mean gate activation",
                    )
                    ax.legend()
                    fig.savefig(
                        output / "plots" / f"{key}_gate_activations.png", dpi=150
                    )

    roster = json.loads(
        (output / "cross_model" / "validation_examples_and_predictions.json").read_text(
            encoding="utf-8"
        )
    )
    if roster:
        labels = [
            f"{item['review_id'].split('/')[-1]}\n{','.join(item['selection_tags'])}"
            for item in roster
        ]
        x = np.arange(len(roster))
        width = 0.15
        palette = [
            COLORS["green"],
            COLORS["orange"],
            COLORS["red"],
            COLORS["teal"],
            COLORS["yellow"],
        ]
        fig = Figure(figsize=(15, 6), constrained_layout=True)
        FigureCanvasAgg(fig)
        ax = fig.subplots()
        for index, (key, color) in enumerate(zip(MODEL_KEYS, palette, strict=True)):
            ax.bar(
                x + (index - 2) * width,
                [item[f"{key}_positive_probability"] for item in roster],
                width,
                label=key,
                color=color,
            )
        ax.set(
            title="Positive-class probabilities for selected validation examples",
            ylabel="Model-reported P(positive)",
            xticks=x,
            xticklabels=labels,
        )
        ax.set_ylim(0, 1)
        ax.legend(ncol=3)
        fig.savefig(
            output / "plots" / "cross_model_probability_comparison.png", dpi=150
        )


def run_interpretability(
    *,
    dataset_directory: str | Path = "datasets/aclImdb",
    comparison_directory: str | Path | None = None,
    output_root: str | Path = "outputs/interpretability",
    max_examples: int = 8,
    max_occlusion_windows_per_size: int = 100,
    device: str | torch.device = "cpu",
) -> dict[str, Any]:
    """Generate Phase 14 explanations from audited validation records only."""
    if (
        isinstance(max_examples, bool)
        or not isinstance(max_examples, int)
        or max_examples < 1
    ):
        raise ValueError("max_examples must be a positive integer.")
    if (
        isinstance(max_occlusion_windows_per_size, bool)
        or not isinstance(max_occlusion_windows_per_size, int)
        or max_occlusion_windows_per_size < 1
    ):
        raise ValueError("max_occlusion_windows_per_size must be a positive integer.")
    analysis_device = torch.device(device)
    if analysis_device.type not in {"cpu", "cuda"}:
        raise ValueError("device must be either 'cpu' or 'cuda'.")
    if analysis_device.type == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested but is not available.")
    root = Path.cwd().resolve()
    if comparison_directory is None:
        candidates = sorted(
            (root / "outputs/neural_comparison").glob(
                "imdb-neural-comparison-*/comparison.json"
            )
        )
        if not candidates:
            raise FileNotFoundError("No Phase 12 comparison artifact exists.")
        comparison_path = candidates[-1]
    else:
        supplied = Path(comparison_directory)
        comparison_path = (
            supplied / "comparison.json" if supplied.is_dir() else supplied
        )
    comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
    if (
        comparison.get("validation_only") is not True
        or comparison.get("official_test_evaluated") is not False
    ):
        raise ValueError("Source comparison must be explicitly validation-only.")
    source = {
        ("logistic_regression" if key == "baseline" else key): Path(value).resolve()
        for key, value in comparison["source_artifacts"].items()
        if key != "phase6_comparison"
    }
    phase6_dir = Path(comparison["source_artifacts"]["phase6_comparison"]).resolve()
    phase6_meta = json.loads(
        (phase6_dir / "comparison.json").read_text(encoding="utf-8")
    )
    phase6_predictions = phase6_dir / phase6_meta["artifacts"]["validation_predictions"]

    # The only dataset split read here is "train"; validation is deterministically
    # carved from it and checked against the Phase 12 fingerprint.
    official_training = load_imdb_split(dataset_directory, "train")
    train, validation = split_training_data(
        official_training, validation_size=VALIDATION_SIZE, seed=SEED
    )
    train_fp, validation_fp = _fingerprint(train), _fingerprint(validation)
    split = comparison["split"]
    if (
        split.get("validation_review_fingerprint_sha256") != validation_fp
        or split.get("training_review_fingerprint_sha256") != train_fp
        or split.get("validation_rows") != len(validation)
    ):
        raise ValueError(
            "Reconstructed Phase 12 validation split does not match its "
            "saved fingerprints."
        )

    initial_hashes = {key: _file_hashes(path) for key, path in source.items()}
    for key, path in source.items():
        _check_run_metadata(
            path, key, train_fp, validation_fp, len(train), len(validation)
        )
    predictions = _read_phase6_predictions(phase6_predictions, validation)
    recurrent_keys = {"rnn": "vanilla_rnn", "lstm": "lstm", "gru": "gru"}
    for key, model_key in recurrent_keys.items():
        frame = pd.read_csv(source[key] / "validation_predictions.csv")
        if (
            frame.review_id.tolist() != validation.review_id.tolist()
            or not np.array_equal(
                frame.true_label.to_numpy(dtype=np.int64),
                validation.label.to_numpy(dtype=np.int64),
            )
        ):
            raise ValueError(
                f"{key} saved predictions do not match the common validation split."
            )
        labels = frame.predicted_label.to_numpy(dtype=np.int64)
        probabilities = frame.positive_probability.to_numpy(dtype=np.float64)
        if not np.isin(labels, (0, 1)).all() or (
            not np.isfinite(probabilities).all()
            or ((probabilities < 0) | (probabilities > 1)).any()
        ):
            raise ValueError(f"{key} saved predictions contain invalid values.")
        predictions[key] = (labels, probabilities)
    examples = _select_examples(validation, predictions, maximum=max_examples)
    out = (
        Path(output_root).resolve()
        / f"imdb-interpretability-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S.%fZ')}"
    )
    for folder in (*MODEL_KEYS, "cross_model", "plots", "metadata"):
        (out / folder).mkdir(parents=True, exist_ok=False)

    pipeline, _ = load_baseline_artifacts(source["logistic_regression"])
    lr_info = _lr_explanations(
        pipeline, validation, examples, out / "logistic_regression"
    )
    model_info: dict[str, dict[str, Any]] = {"logistic_regression": lr_info}
    # Checkpoint loaders are read-only; all neural explanations are inference-only.
    cnn = load_textcnn_predictor(source["textcnn"], analysis_device)
    _cnn_activations(cnn, examples, validation, out / "textcnn")
    cnn_occlusion = _occlusion(
        cnn,
        examples,
        validation,
        out / "textcnn",
        max_windows=max_occlusion_windows_per_size,
    )
    model_info["textcnn"] = {
        "method": (
            "convolution ReLU activations with exact max-pooling windows; "
            "batched <unk> occlusion"
        ),
        "occlusion_rows": len(cnn_occlusion),
        "max_pooling_reconstruction_validated": True,
        "representative_filters_per_size": 10,
    }
    for key in ("rnn", "lstm", "gru"):
        predictor = load_recurrent_predictor(source[key], analysis_device)
        gates_valid = _recurrent_explanations(
            key, predictor, examples, validation, out / key
        )
        occlusion = _occlusion(
            predictor,
            examples,
            validation,
            out / key,
            max_windows=max_occlusion_windows_per_size,
        )
        method = (
            "native tanh hidden-state trajectory; batched <unk> occlusion"
            if key == "rnn"
            else (
                "analysis-only PyTorch cell equations validated against native "
                "logits; hidden-state trajectory and batched <unk> occlusion"
            )
        )
        model_info[key] = {
            "method": method,
            "occlusion_rows": len(occlusion),
            "hidden_state_trace_validated": (
                True if key == "rnn" else bool(gates_valid)
            ),
            "cell_state_trace_validated": (
                bool(gates_valid) if key == "lstm" else None
            ),
            "gate_equations_validated": gates_valid,
            "gate_activations_written": bool(gates_valid and key in {"lstm", "gru"}),
        }

    roster = []
    for example in examples:
        row = validation.iloc[example["row_index"]]
        item = {
            "review_id": example["review_id"],
            "true_label": example["true_label"],
            "selection_tags": example["selection_tags"],
            "review_text": row.text,
        }
        for key in MODEL_KEYS:
            item[f"{key}_prediction"] = int(predictions[key][0][example["row_index"]])
            item[f"{key}_positive_probability"] = float(
                predictions[key][1][example["row_index"]]
            )
        roster.append(item)
    _save_json(out / "cross_model" / "validation_examples_and_predictions.json", roster)
    pd.DataFrame(
        [{k: v for k, v in item.items() if k != "review_text"} for item in roster]
    ).to_csv(out / "cross_model" / "prediction_comparison.csv", index=False)
    for key in ("textcnn", "rnn", "lstm", "gru"):
        occlusion = pd.read_csv(out / key / "token_window_occlusion.csv")
        if not occlusion.empty:
            top = (
                occlusion.sort_values("positive_probability_drop", ascending=False)
                .groupby("review_id", as_index=False)
                .head(10)
            )
            top.to_csv(
                out / "cross_model" / f"{key}_top_supportive_windows.csv", index=False
            )
    _write_pairwise_explanations(out, roster)
    _plot_outputs(out, {key: {} for key in ("textcnn", "rnn", "lstm", "gru")})
    final_hashes = {key: _file_hashes(path) for key, path in source.items()}
    if final_hashes != initial_hashes:
        raise RuntimeError(
            "At least one frozen source artifact changed during Phase 14."
        )
    metadata = {
        "phase": 14,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "validation_only": True,
        "analysis_device": str(analysis_device),
        "dataset_source": (
            "official labeled training split only; validation deterministically "
            "held out from train"
        ),
        "official_test_split_loaded": False,
        "official_test_evaluated": False,
        "model_training_performed": False,
        "checkpoint_files_modified": False,
        "source_comparison": str(comparison_path.resolve()),
        "source_artifacts": {key: str(path) for key, path in source.items()},
        "source_artifacts_unchanged_sha256": True,
        "source_artifact_file_sha256": initial_hashes,
        "split": {
            "seed": SEED,
            "validation_fraction": VALIDATION_SIZE,
            "training_rows": len(train),
            "validation_rows": len(validation),
            "training_fingerprint_sha256": train_fp,
            "validation_fingerprint_sha256": validation_fp,
        },
        "selected_example_count": len(examples),
        "analysis_configuration": {
            "max_examples": max_examples,
            "max_occlusion_windows_per_size": max_occlusion_windows_per_size,
            "occlusion_window_sizes": [1, 2, 3],
            "occlusion_batch_size": 128,
            "cnn_representative_filters_per_size": 10,
        },
        "models": model_info,
        "explanation_score_convention": (
            "positive_probability_drop = original P(positive) minus masked "
            "P(positive); positive values mean the window supports the positive "
            "class, negative values mean it suppresses it"
        ),
        "recurrent_equation_validation_tolerance": {
            "absolute": CELL_VALIDATION_ATOL,
            "relative": CELL_VALIDATION_RTOL,
        },
        "limitations": [
            "Occlusion depends on replacing tokens with <unk>; it is a "
            "sensitivity analysis, not a causal claim.",
            "Recurrent hidden-state norms summarize state magnitude, not "
            "token-level causal attribution.",
            "LSTM/GRU gates are reported only when analysis equations reproduce "
            "the native saved model logits.",
        ],
    }
    _save_json(out / "metadata" / "run.json", metadata)
    _save_json(out / "metadata" / "selected_examples.json", examples)
    return {"output_directory": str(out), **metadata}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-directory", default="datasets/aclImdb")
    parser.add_argument("--comparison-directory", default=None)
    parser.add_argument("--output-root", default="outputs/interpretability")
    parser.add_argument("--max-examples", type=int, default=8)
    parser.add_argument("--max-occlusion-windows-per-size", type=int, default=100)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    args = parser.parse_args()
    # These relatively small batches are faster and more predictable without
    # large BLAS/OpenMP thread pools on common desktop CPUs.
    torch.set_num_threads(1)
    result = run_interpretability(
        dataset_directory=args.dataset_directory,
        comparison_directory=args.comparison_directory,
        output_root=args.output_root,
        max_examples=args.max_examples,
        max_occlusion_windows_per_size=args.max_occlusion_windows_per_size,
        device=args.device,
    )
    print(
        json.dumps(
            {
                "output_directory": result["output_directory"],
                "validation_only": result["validation_only"],
                "selected_example_count": result["selected_example_count"],
                "models": result["models"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
