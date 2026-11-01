export const API_ROOT = process.env.NEXT_PUBLIC_OPENBENCH_API_URL ?? "http://127.0.0.1:8000";

export async function apiGet(path) {
  const response = await fetch(`${API_ROOT}${path}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`OpenBench API returned ${response.status}`);
  return response.json();
}

export async function predictReview(text, modelIds) {
  const response = await fetch(`${API_ROOT}/api/predict`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, ...(modelIds ? { model_ids: modelIds } : {}) }),
  });
  if (!response.ok) {
    const result = await response.json().catch(() => ({}));
    throw new Error(result.detail ?? `Prediction failed (${response.status})`);
  }
  return response.json();
}

export function validateReview(text) {
  if (typeof text !== "string" || text.trim().length === 0) return "Enter a review before requesting a prediction.";
  if (text.length > 10_000) return "Reviews must be 10,000 characters or fewer.";
  return "";
}
