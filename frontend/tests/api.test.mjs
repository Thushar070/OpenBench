import test from "node:test";
import assert from "node:assert/strict";
import { apiGet, predictReview, validateReview } from "../src/lib/api.mjs";

test("review validation covers blank and oversized input", () => {
  assert.match(validateReview("  \n"), /Enter a review/);
  assert.match(validateReview("x".repeat(10_001)), /10,000/);
  assert.equal(validateReview("A review"), "");
});

test("API client returns JSON and surfaces server errors", async context => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async () => ({ ok: true, json: async () => ({ models: [1, 2, 3, 4, 5] }) });
  assert.equal((await apiGet("/api/models")).models.length, 5);
  let requestBody;
  globalThis.fetch = async (_url, options) => {
    requestBody = JSON.parse(options.body);
    return { ok: true, json: async () => ({ models: [{ id: "textcnn" }] }) };
  };
  assert.equal((await predictReview("A review", ["textcnn"])).models[0].id, "textcnn");
  assert.deepEqual(requestBody, { text: "A review", model_ids: ["textcnn"] });
  globalThis.fetch = async () => ({ ok: false, status: 422, json: async () => ({ detail: "Review too long" }) });
  await assert.rejects(predictReview("bad"), /Review too long/);
});
