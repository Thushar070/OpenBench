# OpenBench semantic challenge set

`semantic_challenge.csv` contains 200 original, author-created IMDb-style review snippets for Phase 15. It is separate from the automatic validation perturbations and contains 100 positive and 100 negative examples, with ten examples of each label in each of ten linguistic challenge categories.

Labels use `0` for negative and `1` for positive. Each row has a stable challenge ID, intended label, category, short rationale, and linguistic phenomenon. The texts were authored for this challenge set rather than copied from or produced by editing IMDb training or validation reviews. Phase 15 checks for exact normalized-text overlap against the available IMDb training corpus before evaluation.

The labels and rationales are the author’s intended interpretations, not independently adjudicated ground truth. Borderline examples are deliberately weak in sentiment and should be reviewed by independent annotators before high-stakes use. Results on this small constructed set are descriptive and are not comparable to the held-out IMDb test evaluation.
