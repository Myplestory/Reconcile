# NLI Classification Methodology

For research hypotheses, validation plan, and threats to validity, see
[`docs/hypothesis.md`](hypothesis.md).

## 1. Problem Statement

Collaboration health metrics (Gini coefficient, Shannon entropy, bus factor, churn
decomposition) are meaningful only when segmented by work type. A team with 0.8 Gini on
*feature* commits has a concentration problem; 0.8 Gini on *bugfix* commits may be
natural (one person maintains a legacy module). Per Mockus & Votta (2000) "Identifying
Reasons for Software Changes Using Historic Databases" (ICSM), change purpose must be
classified before metrics are computed.

The prior approach — regex keyword matching on commit messages (`\bfix\b` → bugfix,
`refactor` → refactor) — misses approximately 40% of commits in the target repository.
Messages like "reconciled with shuning map changes", "map", and "line 28 same fix"
contain no classification keyword. Our observation of ~40% keyword miss rate on the
target repository aligns with Herzig & Zeller's (2013, ICSE) broader finding that
textual artifact classification is unreliable when based on surface signals alone.
Their study of 7,401 issue reports across five open-source projects found 33–39% of
human-applied labels were incorrect (e.g., reports labeled "bug" that were actually
features or documentation). If even deliberate human labeling is unreliable at those
rates, automated keyword matching on informal student commit messages is expected to
perform worse. Misclassification at these rates propagates to bias all downstream
models — in their case defect prediction; in ours, collaboration health assessment.

## 2. Approach: Zero-Shot NLI via Hypothesis Entailment

### 2.1 Theoretical Foundation

Yin et al. (2019) "Benchmarking Zero-shot Text Classification: Datasets, Evaluation and
Entailment Approach" (EMNLP) established that natural language inference (NLI) enables
text classification without labeled training data. The method:

1. Treat the input text as a **premise**
2. Treat each candidate category as a **hypothesis** (natural language sentence)
3. Score `P(entailment | premise, hypothesis)` for every hypothesis
4. The category whose hypothesis receives the highest entailment score wins

This is a **discriminative** task. The model outputs a conditional probability distribution
over {entailment, not_entailment} — not generated text. Given identical inputs,
the output is deterministic and reproducible. There is no hallucination risk because the
model does not generate language; it classifies a relationship between two texts.

To our knowledge, this is a novel application of Yin et al.'s NLI methodology to
software engineering commit classification. Prior work on commit classification (Gharbi
et al. 2019, EMSE; Levin & Yehudai 2017, JSS) uses supervised approaches requiring
labeled training data — a significant barrier in educational contexts where each semester
produces a new repository with different conventions. The zero-shot NLI approach
eliminates this dependency.

### 2.2 Distinction from Generative AI

The model used (DeBERTa-v3-base, He et al. 2021, ICLR) is a **sequence classification**
model, not an autoregressive language model. It does not perform multi-step logical
reasoning, iterative inference, or chain-of-thought processing. It executes a single
fixed-depth forward pass of attention-weighted token interactions — a learned similarity
function, not a reasoning process. It computes this learned similarity between two text
inputs (premise and hypothesis), then maps the resulting vector to two output logits
via a linear classification head. The softmax of those logits produces
`P(entailment)` and `P(not_entailment)`. (The `zeroshot-v2.0` checkpoint collapses
neutral and contradiction into a single `not_entailment` class — this is standard for
zero-shot classification where only entailment strength matters per Yin et al. 2019.)
That is the entire computation — there is no chain of thought, no intermediate reasoning,
no semantic understanding in the human sense. The model returns the probability that one
text is classified as entailing another, nothing more.

The critical differences:

| Property | Generative LLM (GPT, Claude) | Discriminative NLI (DeBERTa) |
|---|---|---|
| Output | Generated text (tokens) | Probability distribution over 2 classes (entailment, not_entailment) |
| Reasoning | Autoregressive chain-of-thought, can fabricate logic | None — single-pass learned similarity function, no iterative inference |
| Determinism | Temperature-dependent, stochastic | Deterministic (softmax of fixed logits) |
| Hallucination | Can fabricate plausible-sounding text | Cannot — outputs only {entailment, not_entailment} per hypothesis |
| Gibberish input | May "reason" confidently about nonsense | Expected: low P(entailment) across all hypotheses, triggering deterministic fallback (verified in §8, Test 5) |
| Interpretability | Requires interpretation of generated text | Score table: 8 hypotheses × 2 probabilities = 16 auditable numbers |
| Mechanism | Predicts next token, recursively | Single forward pass, no recursion, no generation |

The concern that "you can pass in random gibberish and it would still reason it like it
makes sense" describes autoregressive generation, not discriminative classification.
A DeBERTa NLI model receiving gibberish produces low `P(entailment)` across all
hypotheses (high `P(not_entailment)`), which the confidence threshold detects and routes
to deterministic fallback. Empirically verified on Team 1470: "reconciled with shuning
map changes" scored <0.04 entailment across all 8 hypotheses — correctly routed to
deterministic. The model does not "reason" that the gibberish makes sense — it computes
low similarity scores across all hypotheses, which is the mathematically correct output
for unrelated text pairs.

### 2.3 Multi-Signal Fusion

Single-signal classification is fragile. Levin & Yehudai (2017) "Boosting Automatic
Commit Classification Into Maintenance Activities by Utilizing Source Code Changes"
(JSS) demonstrated that combining commit message analysis with code change metrics
significantly outperforms either signal alone.

The classification pipeline fuses five signals with weighted combination:

| Signal | Weight | Source | Reliability |
|---|---|---|---|
| NLI entailment | 0.40 | Model inference | High when confident (entailment ≥ 0.40, margin ≥ 0.15) |
| Conventional Commits prefix | 0.25 | Regex parse of `feat:`, `fix:`, etc. | Authoritative when present (developer-declared intent) |
| Diff file categories | 0.20 | `categorize_file()` on changed files | Deterministic, always available |
| Keyword match | 0.10 | Existing regex matcher | Low — 40% miss rate, but free |
| Diff size | 0.05 | Lines added + deleted | Weak heuristic (small → fix, large → feature) |

Fusion weights are initial heuristic values adapted from an operationally validated
pipeline (PolyEdge cross-venue semantic matcher). The relative ordering reflects signal
reliability: NLI provides semantic understanding, CC prefix is developer-declared intent
(authoritative when present), diff categories are deterministic, keywords are low-recall,
and diff size is a weak proxy. Specific values are subject to empirical calibration
via the NLI-vs-deterministic agreement report (§5.5) and ablation study
([`hypothesis.md`](hypothesis.md) §7.4).

Importantly, the deterministic signals (diff categories, keywords, diff size)
always produce a classification independent of NLI. NLI refines the deterministic
baseline; it does not replace it.

**Weight redistribution** when signals are absent:

- No Conventional Commits prefix (most student repos): NLI weight redistributed to 0.52
- NLI not confident (degenerate message): diff categories redistributed to 0.57
- Neither NLI nor prefix: pure deterministic (diff categories 0.57, keyword 0.29, size 0.14)

This ensures graceful degradation: the system always produces a classification.

## 3. Model Selection

### 3.1 Architecture: DeBERTa-v3-base (184M parameters)

He et al. (2021) "DeBERTa: Decoding-enhanced BERT with Disentangled Attention" (ICLR)
introduced disentangled attention, which separately encodes content and position. This
is particularly effective for NLI where the *relationship* between premise and hypothesis
matters more than absolute token positions.

**Why base, not large (304M parameters):**

Commit messages average 5–20 words. Card titles average 10–40 words. At max_length=256
tokens, both inputs are well under capacity. The large model's additional parameters
improve accuracy on long documents (PolyEdge uses large for 512-token financial contracts)
but provide diminishing returns on short text. Empirically, base achieves 90.6% accuracy on MNLI-matched (Williams et al. 2018).
This accuracy is measured on the MNLI benchmark distribution (news, fiction, telephone
transcripts), not on software engineering text. The confidence threshold and
deterministic fallback mitigate domain gap — when the model is uncertain on
domain-specific input, the deterministic baseline takes over.

### 3.2 Checkpoint: `MoritzLaurer/deberta-v3-base-zeroshot-v2.0`

Three DeBERTa-v3-base NLI checkpoints were evaluated:

| Checkpoint | Training data | Zero-shot design | Selection rationale |
|---|---|---|---|
| `cross-encoder/nli-deberta-v3-base` | SNLI + MNLI | No — general NLI | Baseline — fewest training distributions |
| `MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli` | MNLI + FEVER + ANLI + LingNLI | No — broader NLI | Better robustness via adversarial training |
| **`MoritzLaurer/deberta-v3-base-zeroshot-v2.0`** | Multi-dataset, zero-shot tuned | **Yes — explicitly** | **Purpose-built for our exact methodology** |

Empirical comparison of all three checkpoints on the target repository's 96 commits
is part of the validation plan ([`hypothesis.md`](hypothesis.md) §7.1). The selection
above is based on training data analysis; empirical results may indicate a different
optimal checkpoint.

All three are identical in parameter count (184M), model size (~440MB), and inference
speed. The difference is training data and fine-tuning objective.

**Selection rationale:**

1. **ANLI adversarial examples** (Nie et al. 2020): ANLI was constructed by human
   annotators who wrote examples that fooled existing NLI models. Training on ANLI
   improves robustness on edge cases — critical for ambiguous commit messages like
   "Prevent XSS and SQL Injection Attacks" (feature? bugfix? security task?).

2. **FEVER fact verification** (Thorne et al. 2018): FEVER pairs claims with evidence
   sentences and classifies as SUPPORTED/REFUTED/NOT ENOUGH INFO. This is structurally
   similar to our task: "does this commit message support the hypothesis that this change
   adds new functionality?"

3. **Zero-shot tuning**: The `zeroshot-v2.0` checkpoint was specifically fine-tuned for
   Yin et al. (2019) style hypothesis-template classification — our exact methodology.
   General NLI checkpoints work but are not optimized for this use pattern.

### 3.3 Precision: FP32 (No Quantization)

**Decision: no INT8 quantization.**

PolyEdge uses INT8 quantization (`bitsandbytes` BitsAndBytesConfig) to fit two models
simultaneously on 8GB GPUs (4B embedding model + 880MB cross-encoder ≈ 2.5GB combined).
Quantization reduces cross-encoder VRAM from 1.7GB to 0.5GB, making co-residency possible.

The commit classifier loads a **single** 440MB model. Memory analysis:

| Hardware | Model | Peak activations (batch=64) | Total peak | Available | Headroom |
|---|---|---|---|---|---|
| CUDA 8GB | 440MB | ~360MB | ~800MB | 8,192MB | 90% free |
| MPS Mac 8GB | 440MB | ~360MB | ~800MB | ~6,000MB | 87% free |
| CPU 16GB | 440MB | ~360MB | ~800MB | 16,384MB | 95% free |

No VRAM pressure exists at any scale. The GPU semaphore ensures one forward pass at a
time — adding more teams does not increase peak memory.

**Accuracy risk of quantization:**

The classification confidence threshold requires entailment ≥ 0.40 with margin ≥ 0.15
between the winner and runner-up. INT8 quantization on MNLI benchmarks typically degrades
accuracy by 0.5–1.5% (Zafrir et al. 2019, "Q8BERT: Quantized 8Bit BERT"). On short
sequences (35 tokens), the degradation is proportionally larger because fewer tokens
contribute to the final representation — quantization noise has outsized effect on the
softmax distribution.

A 1% shift in entailment probability can flip a 0.15-margin decision. With no memory
pressure to justify the risk, FP32 is the correct choice.

**Speed optimization without accuracy loss:**

`torch.compile(mode="reduce-overhead")` (Ansel et al. 2024, PyTorch 2.0) provides
1.5–2x inference speedup on CUDA by fusing operations and eliminating Python overhead.
Zero accuracy impact — the computation is identical, just faster. Disabled on MPS
(Metal shader compilation errors) and Windows (requires MSVC cl.exe).

## 4. Hypothesis Design

### 4.1 Commit Hypotheses (Hot Path)

8 hypotheses, each describing a change purpose. The model scores every commit message
against all 8 simultaneously.

```
feature:                 "This change adds new functionality or implements a new user-facing capability."
maintenance:bugfix:      "This change fixes a bug, corrects an error, or resolves broken behavior."
maintenance:refactor:    "This change restructures or reorganizes existing code without changing its behavior."
maintenance:documentation: "This change updates documentation, README files, or process guidelines."
maintenance:dependency:  "This change adds, removes, or updates a project dependency or library."
test:                    "This change adds, modifies, or fixes automated tests or test infrastructure."
devops:infra:            "This change modifies deployment scripts, CI/CD pipelines, or server infrastructure."
devops:config:           "This change updates build configuration, linting rules, or project settings."
```

**Design principles:**
- Each hypothesis is a single declarative sentence (Yin et al. 2019 recommendation)
- Hypotheses use "This change" framing because the premise is a commit message (an action)
- Categories align with the existing `categorize_file()` taxonomy for cross-validation
- `test` was added as a distinct category (not collapsed into maintenance) because writing
  tests is a distinct and valued contribution in a course context

### 4.2 Card Hypotheses (Cold Path)

Card titles describe tasks (intent), not changes (action). Different framing is required:

```
feature:    "This task requires implementing new functionality or a user-facing capability."
bugfix:     "This task requires fixing a bug, correcting broken behavior, or resolving an error."
refactor:   "This task requires restructuring or reorganizing existing code."
test:       "This task requires writing or updating automated tests."
docs:       "This task requires updating documentation or process guidelines."
devops:     "This task requires deployment, infrastructure, or CI/CD changes."
config:     "This task requires updating build configuration or project settings."
research:   "This task requires investigation, prototyping, or feasibility analysis."
```

**`research` is card-only.** Student projects include cards like "research of map
feasibility" and "Design a Figma wireframe for the main dashboard map" — tasks that
involve investigation or design, not code changes. These have no commit-level equivalent
because research produces knowledge, not commits (or produces commits classified under
other categories).

### 4.3 Hypothesis Sensitivity

Hypothesis template wording directly affects entailment scores. Small phrasing changes
("adds new functionality" vs "implements a new feature") may shift classification
boundaries. This is a known property of the Yin et al. approach. Mitigation:
(1) hypotheses are written as clear, unambiguous declarative sentences;
(2) the confidence margin threshold catches borderline cases;
(3) the deterministic baseline provides a stable classification independent of
hypothesis wording. Sensitivity testing is part of the validation plan
([`hypothesis.md`](hypothesis.md) §7.6).

### 4.4 Fitness Against Real Data

Evaluated against 88 card titles from the Team 1470 PMTool board:

| Input type | % of cards | NLI difficulty | Handling |
|---|---|---|---|
| User stories ("As a delivery manager I want...") | 28% | Easy — natural English, clear NLI separation | NLI classifies directly |
| Labeled implementations ("Login Page Implementation") | 23% | Easy — "implementation" is strong lexical signal | NLI + keyword fusion |
| Frontend/Backend split ("...on the Management page(backend)") | 17% | Easy — suffix enriches diff-category signal | Diff category + NLI fusion |
| Short technical ("Map Routing", "ETA Handling") | 17% | Degenerate — <4 words | Skip NLI → deterministic |
| Ambiguous ("Prevent XSS and SQL Injection Attacks") | 9% | Medium — thin margins | Confidence threshold → deterministic |
| Research/design ("research of map feasibility") | 3% | Medium — needs research hypothesis | Card-specific hypothesis handles it |
| Garbage ("Shihao Liu -User story 1") | 2% | Degenerate — generic pattern | Pattern detection → skip |

**~72% of card titles are classifiable by NLI** (easy + medium categories).
**~28% route to deterministic fallback** (degenerate + garbage).
This is expected — the NLI path is an enhancement, not a replacement. The deterministic
fallback is always present as the floor.

## 5. Two-Path Cross-Reference

### 5.1 Motivation

Aranda & Venolia (2009) "The Secret Life of Bugs: Going Past the Errors and Omissions
in Software Repositories" (CSCW) found that issue tracker descriptions frequently diverge
from actual code changes. The planned work (card title) and the executed work (commit
messages) are independently observable signals that should be cross-referenced:

- **Agreement**: card says "feature", commit aggregate says "feature" → confident
  classification, team executed what was planned
- **Divergence**: card says "feature", commits say "bugfix" → scope creep, discovered
  bugs during implementation, or poor planning
- **Missing link**: commit has no card reference → informal work, not tracked on board

Per Cataldo et al. (2006) "Identification of Coordination Requirements" (CSCW), alignment
between planned and actual work is a coordination effectiveness metric. Sustained divergence
under PM oversight indicates coordination failure.

### 5.2 Linkage

Commits link to cards via two patterns:

1. **Branch name**: `#NNN` or `NNN_` prefix (existing `extract_card_number()`)
2. **Commit message**: `#NNN` reference (new regex scan)

Empirical linkage rate in Team 1470: ~70% of branches have extractable card numbers.
For 50 diverse teams, expected range: 50–80%. Unlinked commits retain hot-path
classification; cross-reference enriches where linkage exists.

### 5.3 Aggregation

One card may have multiple commits of different types (3 feature, 1 bugfix, 1 test).
Comparison is at the **card aggregate level**, not per-commit:

1. Collect all commit classifications linked to card `#N`
2. Majority vote → card's **effective execution type**
3. Compare to card's **intent type** (cold path NLI result)
4. Report agreement rate per team per sprint

Majority vote is a simplification. The full per-commit distribution is preserved
in the audit trail. Future work may explore weighted multi-label aggregation (by lines
changed) or distribution comparison (KL divergence between card intent and commit
distribution). For the current system, majority vote provides an actionable
single-label comparison while the audit trail retains the full signal.

### 5.4 Metrics Produced

| Metric | Definition | Value |
|---|---|---|
| Agreement rate | % of cards where intent matches execution | Planning effectiveness |
| Card degenerate rate | % of cards too vague for NLI (<4 words, generic) | Card quality / PM effectiveness |
| Divergence flags | Cards where intent ≠ execution with high confidence | Scope creep, discovery, or misplanning |
| Linkage rate | % of commits traceable to a card | Process discipline |

These are collaboration health metrics — they describe team process, not individual blame.

## 6. Confidence and Fallback

### 6.1 Confidence Thresholds

A classification is **confident** if:
- Winner's entailment score ≥ 0.40 (non-trivial evidence of entailment)
- Margin between winner and runner-up ≥ 0.15 (clear separation)

Both thresholds must be met. If either fails, the NLI result is discarded and the
deterministic fusion path (diff categories + keyword + diff size) produces the final
classification. Yin et al. (2019) Section 4.2 discusses confidence-based filtering
for NLI classification. The specific threshold values (entailment ≥ 0.40, margin ≥ 0.15)
are initial heuristic values. The entailment floor (0.40) ensures non-trivial evidence —
below this, the model's opinion is barely better than chance across 8 hypotheses (uniform
would be 0.125). The margin (0.15) ensures clear separation between winner and runner-up.
Both values are subject to calibration against the deterministic baseline.

### 6.2 Degenerate Input Detection

Inputs are flagged degenerate before NLI scoring if:
- Fewer than 4 words after stripping file references
- Match GitHub web UI default patterns: "Add files via upload", "Update X", "Create X"
- Card titles matching generic patterns: "{Name} - User story {N}"

Degenerate inputs skip NLI entirely (0ms) and route to deterministic classification.
The degenerate rate is itself a metric (see §5.4).

### 6.3 Audit Trail

Every classification includes:

```python
{
    "sha": "abc1234",                          # commit or card ID
    "classification": "feature",               # final result
    "confidence": 0.73,                        # NLI confidence (None if deterministic)
    "source": "nli",                           # "nli", "deterministic", "degenerate"
    "scores": {                                # full entailment scores (8 hypotheses)
        "feature": {"entailment": 0.73, "not_entailment": 0.27},
        "maintenance:bugfix": {"entailment": 0.12, "not_entailment": 0.88},
        ...
    },
    "signals": {                               # all fusion inputs
        "nli_winner": "feature",
        "cc_prefix": null,
        "diff_categories": ["frontend:page", "backend:api"],
        "keyword_match": null,
        "diff_lines": 45,
    },
    "margin": 0.61,                            # winner - runner-up
    "circuit_state": "closed",                 # circuit breaker state at scoring time
}
```

This is fully auditable. Any classification can be verified by re-running the model
on the same input — the output is deterministic.

## 7. Limitations and Constraints

### 7.1 Known Limitations

1. **Card descriptions unavailable**: The PMTool event stream carries card titles
   (`card_name`) but not card body text. The `newdesc` event type contains only
   "Changed Description" without the actual content. Cold path operates on titles and
   comments only.

2. **Training data distribution**: DeBERTa-v3 was pre-trained on general English text
   (Wikipedia, BookCorpus). Software engineering jargon ("endpoint", "polling", "XSS")
   may be underrepresented. The confidence threshold mitigates this — unfamiliar terms
   produce low entailment scores, routing to deterministic fallback.

3. **Linkage coverage**: 20–50% of commits may not link to any card. Cross-reference
   metrics are computed over linked commits only. Linkage rate itself is reported as a
   process discipline metric.

4. **Temporal bias**: Card titles are written at creation time. The task may evolve
   during implementation. Cross-reference divergence captures this but cannot distinguish
   "plan changed" from "plan was wrong."

### 7.2 What This System Does NOT Do

- **Does not read or analyze code content.** Classification uses commit messages, card
  titles, file paths, and diff statistics. No source code is passed to the NLI model.
- **Does not generate explanations.** The model outputs probability scores, not text.
  All interpretive language in the dashboard is pre-written by humans, not generated.
- **Does not make grading decisions.** Classification produces metrics that inform
  instructor judgment. The system observes and measures; it does not conclude or accuse.

**Individual classification error does not propagate to grading.**
All metrics are computed over aggregated classifications (per-team, per-sprint).
A single misclassification shifts the team-level distribution by <2% (1 commit
out of ~50 per sprint). The audit trail preserves every individual classification
with its source (NLI, deterministic, degenerate), enabling instructor review of
any flagged result. The deterministic baseline is always available as a
cross-check — no individual classification rests solely on NLI.

## 8. Validation Plan

For the full validation plan with testable hypotheses and threats to validity, see
[`hypothesis.md`](hypothesis.md). Summary of key tests:

### 8.1 Human Baseline
Manually classify 50 commits from Team 1470 repository (stratified sample:
high-confidence NLI, low-confidence NLI, degenerate, keyword-only). Compare
human labels to pipeline output. Report agreement rate (Cohen's kappa if
second rater available).

### 8.2 Checkpoint Comparison
Run all three checkpoint candidates on the 96 Team 1470 commits. Compare
classification distributions and confidence margins. Select checkpoint with highest
human-agreement rate.

### 8.3 Ablation Study
Remove each fusion signal in isolation and measure accuracy degradation against
human baseline:
- NLI only (weight=1.0): measures NLI standalone accuracy
- Deterministic only (no NLI): measures baseline accuracy
- Full fusion: measures combined accuracy
Report NLI's marginal contribution over deterministic baseline.

### 8.4 Calibration Report
Run `calibration_report()` on full dataset:
- Agreement rate: % where NLI and deterministic agree
- Rescue rate: % where deterministic said "other/unknown", NLI provided specific classification
- Override rate: % where both had opinions but disagreed (NLI reclassified to more precise category)

Initial Team 1470 pilot (108 commits): agreement 57%, rescue 0%, override 43%.
The 0% rescue rate reflects that the diff-category baseline never returns "other" —
it always has a coarse classification. NLI's contribution is reclassification precision
(43% of commits reclassified from coarse diff-category labels like `frontend` to
semantic labels like `maintenance:bugfix` or `devops:infra`).
This is the ongoing validation mechanism across semesters.

### 8.5 Edge Case Verification
- Gibberish input ("asdf qwerty jkl"): expect high P(neutral), deterministic fallback
- Single-word commit ("map"): expect degenerate detection, skip NLI
- Ambiguous commit ("line 28 same fix"): expect low margin, deterministic fallback
- Mixed-intent ("fix login and add dashboard"): expect thin margins, report both scores

## References

- Ansel, J. et al. (2024). "PyTorch 2: Faster Machine Learning Through Dynamic Python Bytecode Transformation and Graph Compilation." *ASPLOS*.
- Aranda, J. & Venolia, G. (2009). "The Secret Life of Bugs: Going Past the Errors and Omissions in Software Repositories." *CSCW*, ACM.
- Cataldo, M. et al. (2006). "Identification of Coordination Requirements: Implications for the Design of Collaboration and Awareness Tools." *CSCW*, ACM.
- Gharbi, S. et al. (2019). "On the Classification of Software Change Messages Using Multi-label Active Learning." *EMSE*, Springer.
- He, P. et al. (2021). "DeBERTa: Decoding-enhanced BERT with Disentangled Attention." *ICLR*.
- Herzig, K. & Zeller, A. (2013). "It's Not a Bug, It's a Feature: How Misclassification Impacts Bug Prediction." *ICSE*, IEEE.
- Hönel, S. et al. (2019). "Using Process Metrics to Predict Software Defects." *IST*, Elsevier.
- Levin, S. & Yehudai, A. (2017). "Boosting Automatic Commit Classification Into Maintenance Activities by Utilizing Source Code Changes." *JSS*, Elsevier.
- Mockus, A. & Votta, L. (2000). "Identifying Reasons for Software Changes Using Historic Databases." *ICSM*, IEEE.
- Nie, Y. et al. (2020). "Adversarial NLI: A New Benchmark for Natural Language Understanding." *ACL*.
- Nygard, M. (2007). *Release It! Design and Deploy Production-Ready Software.* Pragmatic Bookshelf.
- Thorne, J. et al. (2018). "FEVER: A Large-scale Dataset for Fact Extraction and VERification." *NAACL-HLT*.
- Williams, A. et al. (2018). "A Broad-Coverage Challenge Corpus for Sentence Understanding through Inference." *NAACL-HLT*.
- Yin, W. et al. (2019). "Benchmarking Zero-shot Text Classification: Datasets, Evaluation and Entailment Approach." *EMNLP*.
- Zafrir, O. et al. (2019). "Q8BERT: Quantized 8Bit BERT." *NeurIPS EMC² Workshop*.
