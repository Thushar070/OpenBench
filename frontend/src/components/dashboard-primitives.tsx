import type { ReactNode } from "react";

export type Data = Record<string, unknown>;

export function asObject(value: unknown): Data {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Data)
    : {};
}

export function asRows(value: unknown): Data[] {
  return Array.isArray(value) ? value.map(asObject) : [];
}

export function asText(value: unknown, fallback = "—"): string {
  return typeof value === "string" && value.length ? value : fallback;
}

export function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function keyLabel(key: string): string {
  return key.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function formatValue(key: string, value: unknown): string {
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "object" && value !== null) return JSON.stringify(value);
  const number = asNumber(value);
  if (number === null) return String(value ?? "Unavailable");
  if (/(accuracy|precision|recall|f1|probability|degradation|flip_rate|consistency)/i.test(key)) {
    return `${(number * 100).toFixed(2)}%`;
  }
  return number.toLocaleString(undefined, { maximumFractionDigits: 3 });
}

export function Metric({
  label,
  value,
  note,
}: {
  label: string;
  value: ReactNode;
  note?: string;
}) {
  return (
    <article className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
      {note && <small>{note}</small>}
    </article>
  );
}

export function Panel({
  title,
  note,
  children,
  className = "",
}: {
  title: string;
  note?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`.trim()}>
      <div className="panel-heading">
        <div>
          <h2>{title}</h2>
          {note && <p>{note}</p>}
        </div>
      </div>
      {children}
    </section>
  );
}

export function DataTable({
  rows,
  columns,
}: {
  rows: Data[];
  columns?: string[];
}) {
  if (rows.length === 0) {
    return <p className="empty-state">No saved values are available for this view.</p>;
  }
  const keys = columns ?? Object.keys(rows[0]).filter((key) => !["text", "review", "review_text", "tokens"].includes(key)).slice(0, 8);
  return (
    <div className="table-scroll" role="region" aria-label="Saved result table" tabIndex={0}>
      <table>
        <thead><tr>{keys.map((key) => <th key={key}>{keyLabel(key)}</th>)}</tr></thead>
        <tbody>
          {rows.slice(0, 100).map((row, index) => (
            <tr key={`${String(row.id ?? row.model_id ?? row.name ?? "row")}-${index}`}>
              {keys.map((key) => <td key={key}>{formatValue(key, row[key])}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Bars({
  rows,
  valueKey,
  labelKey = "name",
  unit,
}: {
  rows: Data[];
  valueKey: string;
  labelKey?: string;
  unit?: string;
}) {
  const values = rows.map((row) => asNumber(row[valueKey]));
  const available = rows.flatMap((row, index) => values[index] === null ? [] : [{ row, value: values[index] as number }]);
  if (!available.length) return <p className="empty-state">No measured values are available for this chart.</p>;
  const maximum = Math.max(...available.map(({ value }) => value), 0.000001);
  return (
    <div className="bars" role="list" aria-label={`${keyLabel(valueKey)} comparison`}>
      {available.map(({ row, value }, index) => (
        <div className="bar-row" role="listitem" key={`${String(row.id ?? row[labelKey] ?? "item")}-${index}`}>
          <span>{asText(row[labelKey], asText(row.model_name, `Item ${index + 1}`))}</span>
          <div className="bar-track"><i style={{ width: `${Math.max(1.5, Math.max(value, 0) / maximum * 100)}%` }} /></div>
          <b>{formatValue(valueKey, value)}{unit ? ` ${unit}` : ""}</b>
        </div>
      ))}
    </div>
  );
}

export function ConfusionMatrix({ model }: { model: Data }) {
  const metrics = asObject(model.metrics);
  const matrixValue = model.confusion_matrix ?? metrics.confusion_matrix;
  const matrix = Array.isArray(matrixValue) ? matrixValue : asObject(matrixValue).matrix;
  if (!Array.isArray(matrix) || matrix.length < 2) return null;
  const cells = matrix.slice(0, 2).flatMap((row) => Array.isArray(row) ? row.slice(0, 2) : []);
  if (cells.length !== 4) return null;
  return (
    <article className="matrix-card">
      <b>{asText(model.name)}</b>
      <div className="matrix-grid" aria-label="Confusion matrix, actual rows and predicted columns">
        {cells.map((value, index) => <span key={index}>{formatValue("count", value)}</span>)}
      </div>
      <small>Actual rows · predicted columns</small>
    </article>
  );
}

export function Records({ value }: { value: unknown }) {
  const rows = asRows(value);
  return <DataTable rows={rows} />;
}
