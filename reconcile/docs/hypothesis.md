# Zero-Shot NLI for Collaboration Health Classification in Educational Software Engineering

## Abstract

Collaboration health monitoring in educational software engineering relies on classifying
the purpose of work artifacts (commits, board cards) to compute meaningful metrics. Current
approaches use keyword matching, which misses approximately 40% of inputs in student
repositories. We propose a deterministic-baseline-plus-NLI-enhancement architecture that
(1) ensures every artifact is classified via auditable heuristics regardless of model
availability, and (2) adds zero-shot natural language inference as an optional
probabilistic refinement layer. We formalize three testable hypotheses about the
educational benefit of improved classification accuracy and describe a validation plan
grounded in the target domain.

## 1. Introduction

### 1.1 Context

In project-based software engineering courses, instructors must assess individual
contributions within student teams. Standard approaches use survey-based peer evaluation
(Hertz, "gruepr," ASEE) supplemented by repository analytics. Git metrics -- commit
frequency, code churn, contribution distribution -- are widely used to quantify
participation (Breuker et al. 2021, CLEI; Perera et al. 2023, ITiCSE).

However, raw contribution counts conflate work of different kinds. A student with 50
commits may have 40 bugfixes and 10 features, while another has 30 features and 20
infrastructure tasks. The Gini coefficient over raw commit counts treats these as equal,
but the pedagogical interpretation is different: feature concentration may indicate a lead
developer pattern, while bugfix concentration may indicate a testing/QA role. Per Mockus &
Votta (2000, ICSM), change purpose must be classified before distribution metrics are
meaningful.

### 1.2 The Classification Problem

Commit messages in student repositories are informal, terse, and inconsistent. Unlike
open-source projects that enforce Conventional Commits or detailed messages, student
commits include:

- Keyword-matchable: "fix login bug", "refactor authentication" (~60%)
- Ambiguous: "reconciled with shuning map changes", "line 28 same fix" (~25%)
- Degenerate: "map", "update", "." (~15%)

Keyword matching (regex on commit messages) correctly classifies only the first category.
The remaining 40% are either misclassified or left as "unknown," introducing systematic
noise into all downstream metrics.

This is not a minor concern. Herzig & Zeller (2013, ICSE) demonstrated that
misclassification of software artifacts at rates of 33-39% propagates to bias all
downstream models -- in their case, defect prediction; in ours, collaboration health
assessment. Their study examined human-applied labels in issue trackers, finding that
even deliberate classification by developers is unreliable. Automated keyword matching
on informal student commit messages is expected to perform worse.

### 1.3 The Deterministic Baseline

The foundation of the classification system is a fully deterministic pipeline:

1. **Conventional Commits prefix parsing**: `feat:`, `fix:`, `refactor:` (authoritative
   when present, developer-declared intent)
2. **Diff file category analysis**: `categorize_file()` maps changed files to functional
   roles (backend:api, frontend:page, devops:infra) via file extension, directory
   conventions, and content sniffing -- three layers of auditable heuristics
3. **Keyword matching**: regex patterns on commit message text (`\bfix\b` -> bugfix,
   `refactor` -> refactor)
4. **Diff size heuristic**: small changes more likely fixes, large changes more likely
   features (weak signal)

This baseline always runs. It always produces a classification. Every rule is a regex
pattern or lookup table -- fully inspectable, deterministic, reproducible. No model, no
weights, no learned parameters. The baseline exists independent of any ML component and
serves as the auditable foundation that can be verified by any stakeholder.

The baseline's limitation is the 40% of inputs where no keyword matches and no
Conventional Commits prefix exists. For these inputs, the classification depends entirely
on diff file categories and diff size -- coarse signals that cannot distinguish a feature
commit from a bugfix commit that modifies the same files.

### 1.4 The NLI Enhancement Hypothesis

We hypothesize that adding zero-shot natural language inference (NLI) as a probabilistic
signal improves classification accuracy on the 40% of inputs where the deterministic
baseline is weakest, and that this improvement translates to measurably more accurate
collaboration health metrics for instructors.

**Critical framing**: NLI does not replace the deterministic baseline. It supplements it.
When NLI is unavailable (model not installed, inference fails, circuit breaker open), the
deterministic baseline runs unchanged. When NLI is available, it provides one additional
signal that is fused with the deterministic signals via weighted combination. The system
is never dependent on NLI.

## 2. Hypotheses

### H1: Classification Accuracy

**H1_0 (null)**: Zero-shot NLI does not improve commit classification accuracy over the
deterministic baseline on ambiguous inputs (those lacking keyword match or CC prefix).

**H1_1 (alternative)**: Zero-shot NLI improves classification accuracy by >= 10 percentage
points over the deterministic baseline on the ambiguous input subset.

**Measurement**: Accuracy against human ground truth, computed on the **ambiguous subset
only** (commits where keyword matching produces no classification and no CC prefix is
present). This isolates the population where NLI is hypothesized to contribute.

Human labeling protocol: the labeler classifies each commit from the message and diff
metadata alone, **blind to pipeline output**. Labels are assigned before any pipeline
comparison. If a second rater is available, inter-rater reliability is reported via
Cohen's kappa between raters; pipeline accuracy is then measured against majority label.
If only one rater is available, accuracy is measured against that rater's labels,
acknowledged as single-annotator ground truth with the caveat that the instructor's
judgment constitutes pedagogical authority in this domain.

The 10pp threshold is a minimum practical significance level: below this, the
computational overhead of model inference is not justified. It is not a derived value.

**Domain**: Ambiguous commits from Team 1470 repository (~42 of 108 commits). Extended
to 5+ teams if H1_1 is supported.

### H2: Metric Sensitivity

**H2_0 (null)**: NLI-enhanced classification does not change collaboration health metrics
(Gini, entropy, bus factor) relative to deterministic-only classification when both are
computed per work-type.

**H2_1 (alternative)**: NLI-enhanced per-category metrics differ from deterministic-only
per-category metrics by >= 0.05 (absolute) on at least one key metric.

**Measurement**: Compute Gini, entropy, and bus factor under **three conditions**:
1. **Unsegmented**: all commits treated equally (no classification)
2. **Deterministic-segmented**: per-category using deterministic-only classification
3. **NLI-segmented**: per-category using full pipeline (deterministic + NLI)

H2_1 tests whether NLI-segmented differs from deterministic-segmented -- isolating NLI's
marginal contribution from the effect of segmentation itself.

With 108 commits across 5 members, statistical power is limited. The 0.05 absolute
threshold is a practical significance level, not a statistically derived one. Multi-team
aggregation (50 teams) provides the sample size for statistical significance; the
single-team evaluation is a pilot.

### H3: Actionability (Pilot Evaluation)

**H3_0 (null)**: Category-segmented reports do not surface actionable patterns beyond what
aggregate reports show.

**H3_1 (alternative)**: Category-segmented reports enable the instructor to identify at
least one additional actionable pattern (work concentration within a specific category,
role specialization, planning-execution misalignment) not visible in aggregate reports.

**Measurement**: Pilot case study (N=1 instructor, acknowledged as non-generalizable).
Present two reports for the same team: (a) aggregate summary, (b) NLI-segmented
per-category breakdown. Ask: "What specific interventions would you consider based on
each report?" Count actionable observations unique to each.

This is not a blinded study -- the instructor knows which report is segmented. We
acknowledge this as a limitation. The evaluation assesses practical value, not
statistical validity. Full evaluation would require multiple instructors across courses,
which is outside the scope of this initial deployment.

**Motivation**: The ultimate value proposition is not classification accuracy but
instructor decision quality. A perfectly accurate classifier that doesn't change what
the instructor does has zero educational benefit.

## 3. Theoretical Foundation

### 3.1 Zero-Shot NLI Classification

Yin et al. (2019, EMNLP) established that natural language inference enables text
classification without labeled training data by scoring whether an input text entails
a hypothesis describing each category. The model outputs P(entailment | premise,
hypothesis) -- a conditional probability, not generated text.

**Novel application**: To our knowledge, no prior published work applies Yin et al.'s
NLI methodology to commit message classification. Existing commit classification
approaches (Gharbi et al. 2019, EMSE; Levin & Yehudai 2017, JSS) require labeled
training data -- a significant barrier in educational contexts where each semester
produces a new repository with different conventions. Zero-shot NLI eliminates this
dependency: the hypothesis templates are universal ("This change adds new functionality")
and require no per-repository training.

**Why zero-shot over few-shot or supervised transfer**: Few-shot fine-tuning requires
labeled examples per-repository, per-semester. With 50 teams and ~5 sprints per semester,
this amounts to ~250 labeling sessions per year -- infeasible for a single instructor.
Supervised transfer learning (train on one team, apply to others) assumes consistent
commit conventions across teams, which does not hold in student repositories where
conventions vary from Conventional Commits to single-word messages within the same
cohort. Zero-shot requires only hypothesis templates that generalize universally.

### 3.2 Discriminative vs Generative Models

The NLI model (DeBERTa-v3-base, He et al. 2021, ICLR) is a sequence classification
model. It does not perform multi-step logical reasoning, iterative inference, or
chain-of-thought processing. It executes a single fixed-depth forward pass of
attention-weighted token interactions -- a learned similarity function, not a reasoning
process. The output is a probability distribution over {entailment, neutral,
contradiction}. Given identical inputs, the output is deterministic and reproducible.

This distinction matters because it addresses the documented concern that generative AI
"can pass in random gibberish and it would still reason it like it makes sense." That
concern describes autoregressive language models (GPT, Claude), not discriminative
classifiers. On out-of-distribution input, the NLI model produces low P(entailment)
across all hypotheses (high P(not_entailment)) -- the mathematically expected output
when premise and hypothesis are unrelated -- which the confidence threshold detects and
routes to the deterministic baseline. Empirically verified: "reconciled with shuning
map changes" scored <0.04 entailment across all 8 hypotheses. Further edge case
verification is Test 5 in the validation plan (S6).

### 3.3 Multi-Signal Fusion

Single-signal classification is fragile. Levin & Yehudai (2017, JSS) demonstrated that
combining commit message analysis with code change metrics outperforms either signal
alone. Our pipeline fuses five signals:

| Signal | Source | Always available? | What it captures |
|---|---|---|---|
| Diff file categories | `categorize_file()` on changed files | **Yes** | What stack layers were touched |
| Keyword match | Regex on commit message | **Yes** | Developer vocabulary when present |
| Diff size | Lines added + deleted | **Yes** | Change magnitude |
| CC prefix | Regex for `feat:`, `fix:`, etc. | When used | Developer-declared intent |
| NLI entailment | Model inference | When model available | Semantic classification |

The first three signals constitute the **deterministic baseline** -- always available,
fully auditable, no model dependency. CC prefix adds developer intent when the
Conventional Commits convention is followed. NLI adds semantic understanding when the
model is loaded and confident.

Fusion weights are initial heuristic values informed by an operationally validated
pipeline (PolyEdge cross-venue semantic matcher). The relative ordering reflects signal
reliability: deterministic signals always contribute; NLI contributes only when confident.
Specific weight values are subject to empirical calibration via the ablation study (S6.4).

**The deterministic baseline is always computed, regardless of NLI availability.** The
system produces identical classifications with or without NLI for any input where the
deterministic signals are sufficient. NLI's contribution is strictly additive -- it can
only change the classification when deterministic signals are ambiguous or absent. When
NLI and deterministic agree, confidence is higher. When they disagree, the calibration
report (S6.7) tracks the disagreement for analysis. This architecture ensures the system
is never dependent on NLI and that the NLI hypothesis is empirically testable.

### 3.4 Educational Context

Collaboration health metrics in educational SE serve a specific purpose: enabling
instructors to identify dysfunctional teams early enough to intervene (Hsiung 2014,
BJET; free-rider detection literature). The accuracy of these metrics directly affects
intervention quality:

- **False equality** (classification noise masks concentration): Instructor sees healthy
  Gini (0.3), doesn't intervene. Reality: one student does all features, another does
  only formatting. Per-category Gini would show 0.8 on features.
- **False concentration** (misclassification inflates one person's share): Instructor
  intervenes unnecessarily. The "concentrated" contributor was doing bugfixes and
  infrastructure, not dominating feature work.

Improved classification accuracy reduces both error types, enabling more precise
interventions. Breuker et al. (2021, CLEI) specifically studied git-based inequality
indexes for student contribution assessment and found them effective for identifying
unequal distribution -- but their approach did not segment by work type, treating all
commits as equal. Our work extends this by adding type-aware segmentation.

### 3.5 Cross-Reference: Planned vs Actual Work

Aranda & Venolia (2009, CSCW) found that issue tracker descriptions frequently diverge
from actual code changes. In educational contexts, this divergence measures planning
effectiveness: do teams execute what they planned?

The two-path architecture classifies both board cards (planned intent, cold path) and
commits (executed action, hot path), then cross-references via card-commit linkage.
Agreement rate, divergence flags, and linkage rate are collaboration health metrics that
inform sprint retrospective discussions -- directly actionable by instructors.

## 4. Model Selection

**Checkpoint**: `MoritzLaurer/deberta-v3-base-zeroshot-v2.0` (184M parameters, ~440MB)

Selected by training data analysis (empirical comparison is Validation Test 1):
- Purpose-built for Yin et al. zero-shot classification (our exact methodology)
- Trained on MNLI + FEVER + ANLI + LingNLI + synthetic zero-shot data
- Identical size and speed to alternative checkpoints
- ANLI adversarial examples improve robustness on ambiguous student commit messages

**Precision**: FP32 (no quantization). Single 440MB model has no memory pressure.
Quantization risks accuracy on thin confidence margins (0.15) with zero memory benefit.

**Speed**: `torch.compile()` on CUDA for 1.5-2x speedup without accuracy loss.

See [`methodology.md`](methodology.md) S3 for full model selection rationale.

## 5. Hypothesis Templates

### 5.1 Design Sensitivity

Hypothesis wording directly affects entailment scores. "This change adds new
functionality" and "This change implements a new feature" may produce different
classifications. This is a known property of zero-shot NLI (Yin et al. 2019).

**Mitigation**:
1. Hypotheses are single declarative sentences (Yin et al. format)
2. Confidence margin threshold (0.15) catches borderline cases -> deterministic fallback
3. Deterministic baseline provides stable classification independent of hypothesis wording
4. Validation Test 6 (S6) systematically tests hypothesis sensitivity

### 5.2 Commit Hypotheses (8)

```
feature:                   "This change adds new functionality or implements a new user-facing capability."
maintenance:bugfix:        "This change fixes a bug, corrects an error, or resolves broken behavior."
maintenance:refactor:      "This change restructures or reorganizes existing code without changing its behavior."
maintenance:documentation: "This change updates documentation, README files, or process guidelines."
maintenance:dependency:    "This change adds, removes, or updates a project dependency or library."
test:                      "This change adds, modifies, or fixes automated tests or test infrastructure."
devops:infra:              "This change modifies deployment scripts, CI/CD pipelines, or server infrastructure."
devops:config:             "This change updates build configuration, linting rules, or project settings."
```

### 5.3 Card Hypotheses (8, different framing -- task intent, not change action)

```
feature:   "This task requires implementing new functionality or a user-facing capability."
bugfix:    "This task requires fixing a bug, correcting broken behavior, or resolving an error."
refactor:  "This task requires restructuring or reorganizing existing code."
test:      "This task requires writing or updating automated tests."
docs:      "This task requires updating documentation or process guidelines."
devops:    "This task requires deployment, infrastructure, or CI/CD changes."
config:    "This task requires updating build configuration or project settings."
research:  "This task requires investigation, prototyping, or feasibility analysis."
```

## 6. Related Work

### 6.1 Commit Message Classification

Mockus & Votta (2000, ICSM) established change purpose classification from commit
metadata as foundational to software process analysis. Subsequent work applied supervised
learning: Gharbi et al. (2019, EMSE) used multi-label active learning, requiring labeled
training data per-project. Levin & Yehudai (2017, JSS) combined message analysis with
source code change metrics, demonstrating multi-signal fusion outperforms single-signal.
Hindle et al. (2009, MSR) analyzed large commits and found commit size correlates with
change purpose. All supervised approaches require labeled corpora that do not exist for
student repositories and would need recreation each semester.

### 6.2 Zero-Shot NLI Classification

Yin et al. (2019, EMNLP) formalized NLI-based zero-shot classification and evaluated on
topic, emotion, and situation datasets. The approach has been applied to sentiment
analysis, intent detection, and document classification, but -- to our knowledge -- not to
software engineering commit messages. Our work is a novel domain application of an
established methodology.

### 6.3 Git Metrics in Education

Breuker et al. (2021, CLEI) used git metrics including inequality indexes and inter-decile
ratios to measure student contribution distribution across 150 students. Perera et al.
(2023, ITiCSE) correlated GitHub metrics with class performance. Both treat all commits
as equal -- no work-type segmentation. The TRACE system (2025, arXiv) applied AI-assisted
assessment to collaborative projects but uses generative models, which introduces the
reasoning and hallucination concerns our discriminative approach avoids.

### 6.4 Team Dysfunction Detection

Hsiung (2014, BJET) used Mahalanobis distance to identify dysfunctional teams and
troubled individuals from cooperative learning data. The free-rider detection literature
(Hall & Buzwell 2012) examines behavioral antecedents. Hertz's gruepr (ASEE) optimizes
team formation but does not monitor ongoing collaboration. Our system complements these
approaches by providing continuous, metric-based monitoring throughout the project
lifecycle.

## 7. Validation Plan

### 7.1 Checkpoint Comparison

Run all three candidate checkpoints on the 108 Team 1470 commits. Compare classification
distributions and confidence margins against human labels. Select checkpoint with highest
accuracy on the ambiguous subset.

### 7.2 Human Baseline (H1 evaluation)

Manually classify the ~38 ambiguous commits (no keyword match, no CC prefix) from Team
1470. Human labeling is performed **blind to pipeline output** -- the labeler sees only the
commit message and diff metadata. Labels are assigned before any pipeline comparison.

Compute accuracy for (a) full pipeline and (b) deterministic-only on this subset.
If accuracy improves by >= 10pp, H1_1 is supported.

### 7.3 Metric Sensitivity (H2 evaluation)

Compute Gini, entropy, and bus factor under three conditions:
1. Unsegmented (no classification)
2. Deterministic-segmented (per-category, deterministic-only)
3. NLI-segmented (per-category, full pipeline)

H2_1 is tested by comparing conditions 2 and 3. The comparison between 1 and 2 quantifies
the value of segmentation itself (independent of NLI).

### 7.4 Ablation Study

Remove each signal in isolation and measure accuracy on the human-labeled subset:
- NLI-only: all weight to NLI
- Deterministic-only: no NLI
- Full fusion: all signals
- Extended: keyword-only, diff-category-only, CC-prefix-only

Report each signal's marginal contribution. This validates or adjusts fusion weights.

### 7.5 Edge Case Verification

- Gibberish ("asdf qwerty jkl"): expect low P(entailment) across all hypotheses, deterministic fallback
- Single word ("map"): expect degenerate detection, skip NLI
- Ambiguous ("line 28 same fix"): expect low margin, deterministic fallback
- Mixed-intent ("fix login and add dashboard"): expect thin margins, report both scores

### 7.6 Hypothesis Sensitivity

Rephrase each hypothesis (synonym substitution: "adds new functionality" -> "implements a
new feature") and re-run classification on 20 commits. Report classification stability
(% unchanged). With 8 categories, random classification would produce ~12.5% stability;
our threshold of 80% stability is well above chance, indicating the model is responding
to semantic content, not surface word choice.

### 7.7 Calibration Report

Run `calibration_report()` on full dataset:
- Agreement rate: NLI agrees with deterministic
- Rescue rate: deterministic said "other/unknown", NLI provided specific classification
- Override rate: both had opinions, disagreed — NLI reclassified to a more precise category

Initial Team 1470 results (108 commits): agreement 57%, rescue 0% (deterministic
baseline never returns "other" due to diff-category coverage), override 43%. The
high override rate indicates NLI's primary contribution is **reclassification
precision** — refining coarse diff-category labels (e.g., `frontend` → `maintenance:bugfix`)
rather than rescuing unclassified commits. This is the ongoing cross-semester validation
mechanism.

## 8. Threats to Validity

### 8.1 Internal Validity

- **Single annotator**: Human ground truth from one labeler (the instructor or researcher).
  Classification of ambiguous commits is subjective -- "line 28 same fix" could be bugfix
  or maintenance. Mitigation: labeling protocol defines categories with examples; if a
  second rater is available, inter-rater kappa is reported.
- **Label definitions**: The 8-category taxonomy is a design choice. Different granularity
  (fewer/more categories) could change results. Categories were chosen to align with the
  existing `categorize_file()` taxonomy for cross-validation.
- **Ordering effects**: Commits are labeled in chronological order. Labeler fatigue or
  anchoring could bias later labels. Mitigation: randomize labeling order.

### 8.2 External Validity

- **Single team**: Initial validation on Team 1470 (5 members, 108 commits, PHP/React web
  app). Generalization to other teams, project types (mobile, CLI, data science), and
  languages is untested. Multi-team validation (S7.3) is required before claims generalize.
- **Single course**: CSE 442 at one university. Commit conventions, team size, and
  project scope vary across institutions. The zero-shot approach mitigates this (no
  per-course training), but hypothesis templates may need adjustment for non-web domains.
- **Semester-specific**: Student behavior varies by cohort. Results from Spring 2026 may
  not replicate in future semesters.

### 8.3 Construct Validity

- **Accuracy != usefulness**: H1 measures classification accuracy, but accuracy on the
  ambiguous subset may not translate to metric-level improvement (tested by H2) or
  instructor-level actionability (tested by H3). The three hypotheses form a causal chain
  that must be validated end-to-end.
- **Gini as proxy for collaboration health**: Gini measures inequality, not dysfunction.
  A Gini of 0.8 on features may reflect legitimate role specialization, not free-riding.
  The metrics inform instructor judgment; they do not determine it.

### 8.4 Conclusion Validity

- **Small sample**: 108 commits, 5 members. Statistical power for detecting small effects
  is limited. The 0.05 absolute threshold for H2 is a practical significance level.
  Statistical significance requires multi-team aggregation (50 teams x ~100 commits =
  ~5,000 commits).
- **Multiple comparisons**: Testing 3 metrics (Gini, entropy, bus factor) x 8 categories
  inflates Type I error. Bonferroni correction or FDR control should be applied at the
  multi-team stage.

## 9. Limitations

1. **Novel application**: No prior published work applies NLI to commit classification.
   Results cannot be compared to an established baseline.
2. **Single-repository initial validation**: H1 and H2 are initially tested on one team.
   Generalization requires multi-team validation.
3. **Hypothesis template sensitivity**: Classification depends on hypothesis wording
   (S5.1). Mitigation via confidence thresholds and deterministic baseline.
4. **Card description data gap**: PMTool event stream carries card titles but not body
   text. Cold path operates on titles and comments only.
5. **Individual classification error**: Any single NLI classification may be wrong.
   All metrics are aggregated (per-team, per-sprint), diluting individual error. The
   deterministic baseline is always available as a cross-check. Full audit trail preserves
   every classification with its source for instructor review.

## 10. Educational Benefit

If the hypotheses are supported:
- **H1**: Better classification -> fewer "unknown" commits -> more complete contributor profiles
- **H2**: Per-category metrics -> finer-grained view of work distribution -> role visibility
- **H3**: Actionable insights -> targeted interventions -> earlier identification of
  dysfunctional teams (Hsiung 2014), free-rider patterns, and scope creep

If the hypotheses are NOT supported:
- The deterministic baseline continues to function unchanged
- The NLI component can be removed with zero impact on the system
- The validation data (human labels, ablation results) still contributes to understanding
  classification challenges in educational SE contexts

## References

- Aranda, J. & Venolia, G. (2009). "The Secret Life of Bugs: Going Past the Errors and Omissions in Software Repositories." *CSCW*, ACM.
- Breuker, D. et al. (2021). "Using Git Metrics to Measure Students' and Teams' Code Contributions in Software Development Projects." *CLEI Electronic Journal*, 24(2).
- Cataldo, M. et al. (2006). "Identification of Coordination Requirements: Implications for the Design of Collaboration and Awareness Tools." *CSCW*, ACM.
- Gharbi, S. et al. (2019). "On the Classification of Software Change Messages Using Multi-label Active Learning." *EMSE*, Springer.
- Hall, D. & Buzwell, S. (2012). "The Problem of Free-Riding in Group Projects: Looking Beyond Social Loafing as Reason for Non-Contribution." *Active Learning in Higher Education*, 14(1).
- He, P. et al. (2021). "DeBERTa: Decoding-enhanced BERT with Disentangled Attention." *ICLR*.
- Hertz, J. "gruepr: An Open Source Tool for Creating Optimal Student Teams." *ASEE PEER*.
- Herzig, K. & Zeller, A. (2013). "It's Not a Bug, It's a Feature: How Misclassification Impacts Bug Prediction." *ICSE*, IEEE.
- Hindle, A. et al. (2009). "What Do Large Commits Tell Us? A Taxonomical Study of Large Commits." *MSR*, IEEE.
- Hsiung, C. (2014). "Identification of Dysfunctional Cooperative Learning Teams and Troubled Individuals." *BJET*, Wiley.
- Levin, S. & Yehudai, A. (2017). "Boosting Automatic Commit Classification Into Maintenance Activities by Utilizing Source Code Changes." *JSS*, Elsevier.
- Mockus, A. & Votta, L. (2000). "Identifying Reasons for Software Changes Using Historic Databases." *ICSM*, IEEE.
- Nie, Y. et al. (2020). "Adversarial NLI: A New Benchmark for Natural Language Understanding." *ACL*.
- Perera, P. et al. (2023). "Correlating Students' Class Performance Based on GitHub Metrics." *ITiCSE*, ACM.
- Yin, W. et al. (2019). "Benchmarking Zero-shot Text Classification: Datasets, Evaluation and Entailment Approach." *EMNLP*.
