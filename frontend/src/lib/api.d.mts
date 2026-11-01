export const API_ROOT: string;
export function apiGet<T>(path: string): Promise<T>;
export type ModelId = "logistic_regression" | "textcnn" | "rnn" | "lstm" | "gru";
export function predictReview(text: string, modelIds?: ModelId[]): Promise<{
  models: Array<{ id: string; name: string; status: string; sentiment: string | null; negative_probability: number | null; positive_probability: number | null; latency_ms: number; device: string | null }>;
  agreement: boolean; available_model_count: number; label_distribution: Record<string, number>;
  scores_are_calibrated_probabilities: boolean;
}>;
export function validateReview(text: string): string;
