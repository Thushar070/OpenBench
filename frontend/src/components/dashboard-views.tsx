"use client";

import { useMemo, useState, type FormEvent } from "react";
import Link from "next/link";
import Image from "next/image";
import { API_ROOT, predictReview, validateReview, type ModelId } from "@/lib/api.mjs";
import {
  asNumber,
  asObject,
  asRows,
  asText,
  Bars,
  ConfusionMatrix,
  DataTable,
  keyLabel,
  Metric,
  Panel,
  type Data,
} from "./dashboard-primitives";

const MODEL_NAMES: Record<string, string> = {
  logistic_regression: "TF-IDF + Logistic Regression",
  textcnn: "TextCNN",
  rnn: "Vanilla RNN",
  lstm: "LSTM",
  gru: "GRU",
};

function trackRows(track: Data): Data[] {
  const metrics = asObject(track.metrics_by_model);
  return Object.entries(MODEL_NAMES).map(([id, name]) => ({
    id,
    name,
    ...asObject(metrics[id]),
  }));
}

export function WorkflowIllustration() {
  return (
    <div className="workflow" aria-label="OpenBench review analysis pipeline">
      <div className="workflow-review"><span>Review text</span><b>“A story worth remembering.”</b></div>
      <div className="workflow-arrow" aria-hidden="true">→</div>
      <div className="workflow-stage"><span>01</span><b>Preprocessing</b><small>Saved tokenizer</small></div>
      <div className="workflow-arrow" aria-hidden="true">→</div>
      <div className="workflow-models"><b>Five frozen models</b><div><span>TF-IDF</span><span>TextCNN</span><span>RNN · LSTM · GRU</span></div></div>
      <div className="workflow-arrow" aria-hidden="true">→</div>
      <div className="workflow-result"><span>Model output</span><b>Positive / Negative</b><small>Uncalibrated scores</small></div>
    </div>
  );
}

export function OverviewView({ data }: { data: Data }) {
  const models = asRows(data.models);
  const dataset = asObject(data.dataset);
  const distribution = asObject(dataset.class_distribution);
  const accuracyRows = models.map((model) => ({ name: model.name, accuracy: model.test_accuracy }));
  const metrics = asObject(data.quality_summary);
  return (
    <>
      <section className="overview-hero">
        <div className="hero-copy">
          <span className="phase-badge">{asText(data.active_generation_id, "Active generation unavailable")}</span>
          <h2>From movie reviews<br />to <em>measurable insight.</em></h2>
          <p>Explore the saved evaluation, robustness and efficiency results for five frozen sentiment models.</p>
          <div className="hero-links">
            <Link className="button primary" href="/comparison">Explore model results <span>→</span></Link>
            <Link className="button" href="/playground">Try a review <span>↗</span></Link>
          </div>
        </div>
        <WorkflowIllustration />
      </section>
      <section className="metrics-grid">
        <Metric label="Frozen models" value={asNumber(data.model_count) ?? "—"} note="Resolved from active manifest" />
        <Metric label="Saved experiments" value={asNumber(data.experiment_count) ?? "—"} note="Discovered from local outputs" />
        <Metric label="Best recorded IMDb test accuracy" value={formatPercent(data.best_test_accuracy)} note="Saved prior evaluation · not pristine" />
        <Metric label="Historical test reviews" value={asNumber(dataset.official_test_review_count)?.toLocaleString() ?? "—"} note="Aggregate only · prior project use" />
      </section>
      <section className="section-grid">
        <Panel title="Saved active-generation test accuracy" note="Historical aggregate metrics; this test set is not pristine." className="panel-wide">
          <Bars rows={accuracyRows} valueKey="accuracy" />
        </Panel>
        <Panel title="IMDb test set" note={asText(data.test_source, "Saved aggregate reports only")}>
          <p className="dataset-name">{asText(dataset.name)}</p>
          <p>{asText(dataset.task)}</p>
          <div className="distribution-row"><span>Negative</span><b>{asNumber(distribution.negative)?.toLocaleString() ?? "Unavailable"}</b></div>
          <div className="distribution-row"><span>Positive</span><b>{asNumber(distribution.positive)?.toLocaleString() ?? "Unavailable"}</b></div>
          <p className="muted">The official IMDb test set has earlier project use. Saved values are descriptive and were not used for selection in the latest audit.</p>
        </Panel>
      </section>
      <Panel title="Five frozen models" note={`Verified active generation · ${asText(data.active_generation_id)}`}>
        <DataTable rows={models} columns={["name", "architecture", "test_accuracy", "test_f1", "artifact_size_bytes", "parameter_count"]} />
      </Panel>
      <p className="muted">{asText(metrics.ranking_method, "Track-specific evaluation results are available in Model comparison.")}</p>
    </>
  );
}

function formatPercent(value: unknown): string {
  const number = asNumber(value);
  return number === null ? "Unavailable" : `${(number * 100).toFixed(2)}%`;
}

export function ComparisonView({ data }: { data: Data }) {
  const tracks = asObject(data.tracks);
  const trackIds = Object.keys(tracks);
  const preferred = "imdb_validation_recovery_selection";
  const [trackId, setTrackId] = useState(trackIds.includes(preferred) ? preferred : trackIds[0] ?? "");
  const track = asObject(tracks[trackId]);
  const rows = useMemo(() => trackRows(track), [track]);
  const limitations = Array.isArray(track.limitations) ? track.limitations.map(String) : [];
  const sampleCount = asNumber(track.sample_count);
  return (
    <>
      <div className="toolbar-row">
        <div><b>{keyLabel(trackId)}</b><span>{sampleCount?.toLocaleString() ?? "Unavailable"} evaluated examples · {asText(track.split)}</span></div>
        <label className="field-label">Evaluation track<select value={trackId} onChange={(event) => setTrackId(event.target.value)}>{trackIds.map((id) => <option key={id} value={id}>{keyLabel(id)}</option>)}</select></label>
      </div>
      {limitations.length > 0 && <aside className="notice"><b>Track limitations</b><ul>{limitations.map((text) => <li key={text}>{text}</li>)}</ul></aside>}
      {trackId.includes("semantic_challenge") && <aside className="notice">Challenge labels are author-intended and have not been independently adjudicated.</aside>}
      <section className="chart-grid">
        <Panel title="Accuracy by model"><Bars rows={rows} valueKey="accuracy" /></Panel>
        <Panel title="F1 by model"><Bars rows={rows} valueKey="f1" /></Panel>
      </section>
      <Panel title="Track metrics" note="Missing measurements remain unavailable; metrics from different tracks are not combined.">
        <DataTable rows={rows} columns={["name", "available", "accuracy", "precision", "recall", "f1", "evaluated_examples", "prediction_consistency", "error_count"]} />
      </Panel>
      <Panel title="Confusion matrices" note="Available only for evaluation tracks that saved them.">
        <div className="matrix-gallery">{rows.map((model) => <ConfusionMatrix key={String(model.id)} model={{ ...model, confusion_matrix: asObject(model).confusion_matrix }} />)}</div>
      </Panel>
      <p className="muted">Audit {asText(data.run_id)} · active generation {asText(data.active_generation_id)}. No cross-dataset score is computed.</p>
    </>
  );
}

export function ExperimentsView({ data }: { data: Data }) {
  const experiments = asRows(data.experiments);
  return (
    <Panel title="Saved experiment history" note="Runs are discovered from local saved output directories; source artifacts are not modified.">
      <DataTable rows={experiments} columns={["phase", "name", "model", "dataset_split", "timestamp", "integrity_verified"]} />
    </Panel>
  );
}

export function FinalEvaluationView({ data }: { data: Data }) {
  const models = asRows(data.models);
  const metricRows = models.map((model) => ({ name: model.name, ...asObject(model.metrics) }));
  const classRows = models.flatMap((model) => Object.entries(asObject(model.classification_report))
    .filter(([label]) => label === "negative" || label === "positive")
    .map(([label, metrics]) => ({ model: model.name, class: label, ...asObject(metrics) })));
  return (
    <>
      <aside className="notice">Historical official test evaluation · {asNumber(data.sample_count)?.toLocaleString() ?? "Unavailable"} examples · integrity {String(data.integrity_verified)}. This test set has prior project use and is not a pristine independent test.</aside>
      <Panel title="Classification metrics"><DataTable rows={metricRows} columns={["name", "accuracy", "precision", "recall", "f1", "false_positive", "false_negative"]} /></Panel>
      <Panel title="Per-class results"><DataTable rows={classRows} /></Panel>
      <Panel title="Confusion matrices"><div className="matrix-gallery">{models.map((model) => <ConfusionMatrix key={String(model.id)} model={model} />)}</div></Panel>
      <Panel title="Selected saved examples" note="Capped examples from the historical report; full raw prediction tables are not exposed."><DataTable rows={asRows(data.selected_error_examples)} /></Panel>
    </>
  );
}

export function InterpretabilityView({ data }: { data: Data }) {
  const models = asObject(data.models);
  return (
    <>
      <aside className="notice">Validation-only saved analysis. Occlusion measures sensitivity to the masking choice; hidden-state magnitudes and activations are not causal explanations.</aside>
      {Object.entries(models).map(([modelId, collections]) => (
        <Panel key={modelId} title={MODEL_NAMES[modelId] ?? keyLabel(modelId)}>
          {Object.entries(asObject(collections)).map(([kind, rows]) => (
            <details className="saved-details" key={kind}>
              <summary>{keyLabel(kind)} <span>{asRows(rows).length} saved rows</span></summary>
              <DataTable rows={asRows(rows)} />
            </details>
          ))}
        </Panel>
      ))}
    </>
  );
}

export function RobustnessView({ data }: { data: Data }) {
  const transformationRows = asRows(data.transformation_summary);
  const transformationModels = Array.from(
    new Set(transformationRows.map((row) => asText(row.model)).filter((model) => model !== "—")),
  );
  const [transformationModel, setTransformationModel] = useState(
    transformationModels.includes("logistic_regression") ? "logistic_regression" : transformationModels[0] ?? "",
  );
  const selectedTransformations = transformationRows.filter((row) => row.model === transformationModel);
  return (
    <>
      <aside className="notice">Automatic perturbations, information-loss tests and the author-curated semantic challenge are separate tracks. Challenge labels are not independently verified; any displayed model scores are uncalibrated.</aside>
      <Panel title="Automatic perturbations" note={asText(data.dataset_split)}>
        <h3>Review-level results</h3><DataTable rows={asRows(data.automatic_overall_by_model)} />
        <h3>Variant accuracy by transformation</h3>
        <label className="field-label">Model<select value={transformationModel} onChange={(event) => setTransformationModel(event.target.value)}>{transformationModels.map((model) => <option key={model} value={model}>{MODEL_NAMES[model] ?? keyLabel(model)}</option>)}</select></label>
        <p className="muted">Descriptive variant accuracy by transformation type for the selected model.</p>
        <Bars rows={selectedTransformations} valueKey="variant_accuracy_descriptive" labelKey="group" />
        <DataTable rows={selectedTransformations} columns={["group", "model", "variant_count", "variant_accuracy_descriptive", "variant_f1_descriptive", "prediction_consistency", "prediction_flip_rate"]} />
        <details className="saved-details"><summary>Review-level summary</summary><DataTable rows={asRows(data.review_level_summary)} /></details>
        <details className="saved-details"><summary>Length sensitivity · information-loss track</summary><DataTable rows={asRows(data.length_sensitivity_summary)} /></details>
      </Panel>
      <Panel title="Semantic challenge" note={asText(data.curated_challenge_note)}>
        <h3>Overall challenge metrics</h3><DataTable rows={asRows(data.semantic_metrics_by_model)} />
        <h3>Category performance</h3><DataTable rows={asRows(data.semantic_category_summary)} />
        <details className="saved-details"><summary>Examples where every model failed</summary><DataTable rows={asRows(data.all_models_wrong)} /></details>
        <details className="saved-details"><summary>Model disagreements</summary><DataTable rows={asRows(data.prediction_disagreements)} /></details>
      </Panel>
    </>
  );
}

export function EfficiencyView({ data }: { data: Data }) {
  const [batchSize, setBatchSize] = useState("128");
  const [trackId, setTrackId] = useState("imdb_validation_recovery_selection");
  const latency = asRows(data.latency_summary).filter((row) => row.available === true);
  const batchOptions = Array.isArray(asObject(data.configuration).batch_sizes) ? asObject(data.configuration).batch_sizes as number[] : [];
  const tracks = asObject(data.quality_tracks);
  const trackIds = Object.keys(tracks);
  const safeTrackId = trackIds.includes(trackId) ? trackId : trackIds[0] ?? "";
  const qualityRows = asRows(data.quality_efficiency_rows).filter((row) => row.quality_track_id === safeTrackId);
  const batchRows = asRows(data.batch_benchmark).filter((row) => row.available === true && String(row.batch_size) === batchSize)
    .map((row) => ({ ...row, model_device: `${asText(row.model_name)} · ${asText(row.device)}` }));
  const models = asObject(data.models);
  const awards = asObject(data.efficiency_awards);
  const hardware = asObject(data.hardware);
  const plots = Array.isArray(data.plot_files) ? data.plot_files.filter((file): file is string => typeof file === "string") : [];
  return (
    <>
      <aside className="notice">Run {asText(data.run_id)} · active generation {asText(data.active_generation_id)} · source hashes unchanged: {String(data.source_artifact_hashes_unchanged)} · no official test reviews loaded for this audit.</aside>
      <section className="metrics-grid">
        <Metric label="Fastest CPU single review" value={`${asNumber(asObject(awards.fastest_cpu_single_review).median_ms)?.toFixed(2) ?? "—"} ms`} note={asText(asObject(awards.fastest_cpu_single_review).model_name)} />
        <Metric label="Fastest supported CUDA" value={`${asNumber(asObject(awards.fastest_supported_cuda_single_review).median_ms)?.toFixed(2) ?? "—"} ms`} note={asText(asObject(awards.fastest_supported_cuda_single_review).model_name)} />
        <Metric label="Highest batch throughput" value={`${asNumber(asObject(awards.highest_batch_throughput).reviews_per_second)?.toLocaleString(undefined, { maximumFractionDigits: 0 }) ?? "—"} /s`} note={`${asText(asObject(awards.highest_batch_throughput).model_name)} · batch ${String(asObject(awards.highest_batch_throughput).batch_size ?? "—")}`} />
        <Metric label="Device" value={asText(hardware.cuda_device, "CPU only")} note={`${String(hardware.platform ?? "")}`} />
      </section>
      <section className="chart-grid">
        <Panel title="Single-review median latency" note="Milliseconds · CPU and CUDA shown as separate measurements.">
          <Bars rows={latency.map((row) => ({ ...row, display_name: `${asText(row.model_name)} · ${asText(row.device)}` }))} valueKey="single_review_median_ms" labelKey="display_name" unit="ms" />
        </Panel>
        <Panel title="Batch throughput" note="Reviews per second · only available runs are plotted.">
          <div className="field-inline"><label className="field-label">Batch size<select value={batchSize} onChange={(event) => setBatchSize(event.target.value)}>{batchOptions.map((size) => <option key={size} value={String(size)}>{size}</option>)}</select></label></div>
          <Bars rows={batchRows} valueKey="reviews_per_second" labelKey="model_device" unit="reviews/s" />
        </Panel>
      </section>
      <Panel title="Quality and latency by one selected track" note="Track values are shown independently. No cross-dataset ranking is calculated.">
        <label className="field-label">Quality track<select value={safeTrackId} onChange={(event) => setTrackId(event.target.value)}>{trackIds.map((id) => <option key={id} value={id}>{keyLabel(id)}</option>)}</select></label>
        <DataTable rows={qualityRows} columns={["model_name", "quality_dataset_id", "quality_split", "evaluated_examples", "accuracy", "precision", "recall", "f1", "latency_device", "single_review_median_ms", "artifact_size_bytes"]} />
      </Panel>
      <section className="chart-grid">
        <Panel title="Model loading and artifact size"><DataTable rows={asRows(data.loading_times)} columns={["model_name", "device", "available", "artifact_loading_seconds", "model_initialization_seconds", "first_prediction_cold_start_latency_ms", "artifact_size_bytes"]} /></Panel>
        <Panel title="Memory observations"><DataTable rows={asRows(data.memory_summary)} columns={["model_name", "device", "available", "observed_rss_increase_bytes", "peak_gpu_allocated_bytes", "peak_gpu_reserved_bytes"]} /></Panel>
      </section>
      <Panel title="Saved audit plots" note="Images are served from the selected local Phase 16 run.">
        <div className="plot-gallery">{plots.map((filename) => <figure key={filename}><Image src={`${API_ROOT}/api/efficiency/plots/${encodeURIComponent(filename)}`} alt={keyLabel(filename.replace(/\.png$/, ""))} width={600} height={400} unoptimized loading="lazy" /><figcaption>{keyLabel(filename.replace(/\.png$/, ""))}</figcaption></figure>)}</div>
      </Panel>
      <Panel title="Hardware and software">
        <DataTable rows={[...Object.entries(hardware).map(([key, value]) => ({ setting: keyLabel(key), value })), ...Object.entries(asObject(data.software_versions)).map(([key, value]) => ({ setting: keyLabel(key), value }))]} columns={["setting", "value"]} />
        <details className="saved-details"><summary>Measured model artifacts</summary><DataTable rows={Object.entries(models).map(([id, model]) => ({ id, ...asObject(model) }))} columns={["name", "generation_id", "architecture", "parameter_count", "trainable_parameter_count", "artifact_size_bytes"]} /></details>
      </Panel>
    </>
  );
}

const EXAMPLES = [
  { label: "Positive example", text: "A thoughtful, funny film with wonderful performances and a story that stayed with me." },
  { label: "Negative example", text: "The plot was dull, the dialogue felt forced, and I was disappointed by the ending." },
  { label: "Mixed example", text: "The acting was excellent, but the movie was far too long and the story lost its way." },
];

export function PlaygroundView({ models }: { models: Data[] }) {
  const [review, setReview] = useState("");
  const [modelId, setModelId] = useState("all");
  const [result, setResult] = useState<Data | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const predictions = asRows(result?.models);
  const distribution = asObject(result?.label_distribution);
  const positiveVotes = asNumber(distribution.positive) ?? 0;
  const negativeVotes = asNumber(distribution.negative) ?? 0;

  async function analyze(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setResult(null);
    const validationError = validateReview(review);
    if (validationError) { setError(validationError); return; }
    setPending(true);
    try {
      const selectedModels: ModelId[] | undefined = modelId === "all" ? undefined : [modelId as ModelId];
      setResult(await predictReview(review, selectedModels));
    } catch (issue) {
      setError(issue instanceof Error ? issue.message : "The prediction request failed.");
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="playground-grid">
      <Panel title="Review text" note="The selected frozen model or all five models analyze the same text.">
        <form className="review-form" onSubmit={(event) => void analyze(event)}>
          <label className="field-label" htmlFor="review-text">Movie review</label>
          <textarea id="review-text" value={review} maxLength={10_000} onChange={(event) => setReview(event.target.value)} placeholder="Paste a movie review to analyze…" />
          <div className="form-foot"><span>{review.length.toLocaleString()} / 10,000</span><button className="text-button" type="button" onClick={() => { setReview(""); setResult(null); setError(""); }}>Clear</button></div>
          <label className="field-label" htmlFor="model-select">Models</label>
          <select id="model-select" value={modelId} onChange={(event) => setModelId(event.target.value)}>
            <option value="all">All five models</option>
            {models.map((model) => <option key={String(model.id)} value={String(model.id)}>{asText(model.name)}</option>)}
          </select>
          {error && <p className="form-error" role="alert">{error}</p>}
          <button className="button primary submit" type="submit" disabled={pending}>{pending ? "Analyzing…" : "Analyze sentiment →"}</button>
        </form>
        <div className="example-list"><span>Try a sample review</span>{EXAMPLES.map((example) => <button type="button" className="example-button" key={example.label} onClick={() => { setReview(example.text); setResult(null); setError(""); }}>{example.label}</button>)}</div>
        <p className="muted">Scores are raw model outputs, not calibrated confidence. Short text can behave differently from full IMDb reviews.</p>
      </Panel>
      <Panel title="Prediction results" note={result ? `${asNumber(result.available_model_count) ?? 0} model(s) available for this request` : "Results from the local FastAPI inference service will appear here."}>
        {pending && <p className="loading-state" role="status">Loading and running the selected frozen model…</p>}
        {!pending && result && <>
          {predictions.length > 1 && <div className="agreement-card"><div><b>{result.agreement ? "Models agree" : "Models disagree"}</b><small>Majority vote: {positiveVotes === negativeVotes ? "No majority" : positiveVotes > negativeVotes ? "Positive" : "Negative"} · {Math.max(positiveVotes, negativeVotes)} of {predictions.length} votes</small></div><span>{predictions.length} selected</span></div>}
          <div className="prediction-list">{predictions.map((prediction) => <PredictionCard key={String(prediction.id)} prediction={prediction} />)}</div>
          <p className="muted">Raw model scores; not calibrated probabilities. Each result comes from the active saved model generation.</p>
        </>}
        {!pending && !result && <p className="empty-state">Enter a review and choose Analyze sentiment to request real predictions.</p>}
      </Panel>
    </div>
  );
}

function PredictionCard({ prediction }: { prediction: Data }) {
  const available = prediction.status === "ok";
  const positiveProbability = asNumber(prediction.positive_probability);
  const negativeProbability = asNumber(prediction.negative_probability);
  const sentiment = asText(prediction.sentiment, available ? "Unavailable" : "Unavailable");
  return (
    <article className="prediction-card">
      <div className="prediction-top"><div><b>{asText(prediction.name)}</b><small>{available ? `${asText(prediction.device)} · ${asNumber(prediction.latency_ms)?.toFixed(2) ?? "—"} ms` : "Model unavailable"}</small></div><strong className={available ? sentiment : "unavailable"}>{sentiment}</strong></div>
      {available && <>
        <div className="probability-labels"><span>Positive {formatPercent(positiveProbability)}</span><span>Negative {formatPercent(negativeProbability)}</span></div>
        <div className="probability-track" role="img" aria-label={`Positive score ${formatPercent(positiveProbability)}`}><i style={{ width: `${Math.min(100, Math.max(0, (positiveProbability ?? 0) * 100))}%` }} /></div>
      </>}
      {!available && <p className="muted">This model did not return a prediction for the request.</p>}
    </article>
  );
}
