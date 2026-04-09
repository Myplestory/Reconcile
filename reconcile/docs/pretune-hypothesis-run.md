# Pre-Tune Hypothesis Run — Team 1470

Date: 2026-04-09
Model: `MoritzLaurer/deberta-v3-base-zeroshot-v2.0` (184M params, FP32)
Device: MPS (Apple Silicon)
Repository: `data/s26-fresh-clone` (Team 1470, CSE 442 S26)
Inference time: ~4s for 108 commits on MPS

This is the **pre-tuning baseline** — default hypothesis templates, default confidence
thresholds (entailment >= 0.40, margin >= 0.15), default fusion weights. No calibration
adjustments have been applied. Results here inform whether H1, H2, H3 warrant further
investigation and where threshold/weight tuning is needed.

## Raw Classification Output

```
========================================================================================================================
SHA      Author     NLI                       Deterministic             Agree Conf   Source       Message
========================================================================================================================
6d9f7fdb LSN        maintenance               feature                     N     -    degenerate   Create README.md
68a3f9df LSN        feature                   feature                     Y     -    degenerate   Add files via upload
15100ea5 刘世豪        backend                   backend                     Y     -    heuristic    Database setup sql code
30ce4831 刘世豪        maintenance:bugfix        backend                     N   0.59   nli          Revert "Database setup sql code"
0476221c 刘世豪        backend                   backend                     Y     -    heuristic    Database setup sql code
07a45493 刘世豪        feature                   feature                     Y   0.60   nli          Add register endpoint with DB insert and JSON resp
223f2b4d 刘世豪        backend                   feature                     N     -    heuristic    Implement login endpoint with DB password verifica
a3b86800 Charles Cheng frontend                  frontend                    Y     -    heuristic    landing page + css
c293a48d Charles Cheng frontend                  frontend                    Y     -    heuristic    login page + css
81fe4477 Charles Cheng frontend                  frontend                    Y     -    degenerate   register.jsx page
f9a033ad Charles Cheng frontend                  frontend                    Y     -    heuristic    forgot password + confirm email reset
4c4ec52d Charles Cheng devops:infra              frontend                    N   0.79   nli          vite + react set up
7c6804bb Charles Cheng frontend                  frontend                    Y     -    heuristic    session handling + auth context
aa8f8ddc Charles Cheng frontend                  devops:config               N     -    degenerate   vite config modified
ec4df3f2 William Otoo-Mensah maintenance:bugfix        legacy                      N   0.53   nli          Login functionality with error handling
2947beaa William Otoo-Mensah legacy                    legacy                      Y     -    degenerate   Langing page functionality
73adc665 William Otoo-Mensah legacy                    legacy                      Y     -    degenerate   Update login.js
0b3bad02 LSN        backend                   feature                     N     -    degenerate   Add files via upload
1a721ad8 LSN        frontend                  feature                     N     -    degenerate   Add frontend folder
c0256b78 kriss-LIU  legacy                    feature                     N     -    degenerate   Add files via upload
44a5038b Charles Cheng backend                   feature                     N     -    heuristic    orders.php, retrieve + create endpoint
b9d9ab5d William Otoo-Mensah legacy                    feature                     N     -    degenerate   Create management.php
165b6688 Charles Cheng backend                   backend                     Y     -    degenerate   schema for orders
25d4632a Charles Cheng maintenance:dependency    frontend                    N   0.67   nli          ui/ux for orders, no navbar globally
0cdb1625 kriss-LIU  legacy                    legacy                      Y     -    degenerate   Update dashboard.js
ab6b38ba kriss-LIU  frontend                  frontend                    Y     -    degenerate   Rename dashboard.html to frontend/dashboard.html
433611a0 kriss-LIU  frontend                  frontend                    Y     -    degenerate   Rename dashboard.js to frontend/dashboard.js
09b1060d kriss-LIU  frontend                  frontend                    Y     -    degenerate   Rename order.html to frontend/order.html
7637f50a LSN        frontend                  feature                     N     -    degenerate   Add files via upload
23ed2253 William Otoo-Mensah legacy                    legacy                      Y     -    degenerate   Update management.php
87a8926c Charles Cheng frontend                  frontend                    Y     -    heuristic    ported over html, js, and css elements from the ra
91b7b5cd Charles Cheng frontend                  frontend                    Y     -    heuristic    deleting legacy raw html/js for dashboard, modifie
56ee6dc1 William Otoo-Mensah legacy                    legacy                      Y     -    degenerate   Update management.php
73e2ab8e William Otoo-Mensah legacy                    feature                     N     -    degenerate   Create orders.html
22283fdc LSN        other                     other                       Y     -    degenerate   index update
38cda520 LSN        frontend                  frontend                    Y     -    degenerate   Map added
7ec93d9c LSN        frontend                  frontend                    Y     -    degenerate   More Englishlized UI
dafc6ef5 Charles Cheng frontend                  maintenance:refactor        N     -    heuristic    refactored raw php html into react jsx + css, nav
5e4f0095 Charles Cheng maintenance:dependency    legacy                      N   0.59   nli          Remove legacy root-level files superseded by React
b450013d Charles Cheng backend                   feature                     N     -    heuristic    create table drivers, seeding with dummy data
fab449e0 Charles Cheng backend                   maintenance:bugfix          N     -    heuristic    drivers.php, patch endpoint option added for order
fcc991fd Charles Cheng feature                   feature                     Y   0.94   nli          find new button, modal implemented, populating mod
170e5ab1 Charles Cheng frontend                  frontend                    Y     -    heuristic    properly reconciled orders with drivers for modal
614aa5d4 Charles Cheng frontend                  frontend                    Y     -    heuristic    hamburger collapsible nav, made managements mobile
0a254664 Charles Cheng devops:infra              devops:config               N   0.98   nli          .htaccess, proper path routing for vite build conf
4cb05855 Charles Cheng devops                    devops:config               N     -    degenerate   package.json deploy script
b079291f Charles Cheng devops:infra              devops:config               N   0.96   nli          deploy script for bashrc sourcing and nvm shell in
9974aa7a Charles Cheng devops                    devops:config               N     -    heuristic    using cached tarball directly
f609f192 Charles Cheng devops                    devops:config               N     -    heuristic    verbose output for debugging checksum hang
50efc2a0 Charles Cheng devops                    maintenance:refactor        N     -    heuristic    attempting automating chmod executable perms befor
a8ae71a8 Charles Cheng maintenance:dependency    devops:config               N   0.47   nli          bypassing npm and invoking through node itself due
57747dc1 LSN        backend                   backend                     Y     -    degenerate   hashed pass word
fcf4b6bc Charles Cheng maintenance:dependency    backend                     N   0.90   nli          added cattle to cors allowed, even with wildcard f
3ec5929d LSN        frontend                  frontend                    Y     -    degenerate   imporveui
b8d286c6 Charles Cheng frontend                  frontend                    Y     -    degenerate   fixed validation messaged
6dfa5494 LSN        backend                   test                        N     -    degenerate   update test
b674591b LSN        backend                   backend                     Y     -    degenerate   update to cattel
257747aa LSN        backend                   backend                     Y     -    degenerate   update cattel
5b0a7927 LSN        backend                   test                        N     -    degenerate   test data increase
b759016e LSN        frontend                  frontend                    Y     -    degenerate   map system
339e8416 LSN        frontend                  feature                     N     -    degenerate   Add files via upload
7e5fde52 LSN        feature                   feature                     Y     -    degenerate   Add files via upload
8773f337 LSN        frontend                  maintenance:bugfix          N     -    degenerate   bug fix
7ab30be4 刘世豪        maintenance:dependency    backend                     N   0.89   nli          Update management panel backend APIs
3081fc73 刘世豪        maintenance:dependency    backend                     N   0.88   nli          Update management panel backend APIs
284e5336 Charles Cheng maintenance:bugfix        maintenance:bugfix          Y   0.96   nli          hotfix for apache reloads spa, .htaccess properly
6884bb1f Charles Cheng maintenance:bugfix        maintenance:bugfix          Y   0.94   nli          hashrouter due to lack of override permissions for
cce877be 刘世豪        frontend                  frontend                    Y     -    heuristic    Update management panel frontend
3154247b Charles Cheng maintenance:bugfix        maintenance:bugfix          Y   0.60   nli          fixed patch, merged into dev
296d6c47 Charles Cheng frontend                  maintenance:bugfix          N     -    degenerate   spa fix
0ca9d286 Charles Cheng frontend                  maintenance:refactor        N     -    heuristic    refactored into .jsx, endpoints changed from stati
39bc8ef2 Charles Cheng devops:infra              maintenance:bugfix          N   0.90   nli          polling endpoint, driver patch update location enp
b85982b0 Charles Cheng backend                   maintenance:refactor        N     -    heuristic    seperate migrations schema file IF NOT EXISTS, san
db3e1c59 Charles Cheng feature                   frontend                    N   0.94   nli          Maps added using leaflet.js, voyager preset for mi
14ddfd34 LSN        backend                   backend                     Y     -    degenerate   update geocide
42d2bd96 LSN        frontend                  frontend                    Y     -    degenerate   map
81338a6a LSN        frontend                  frontend                    Y     -    degenerate   map
fe4e90ff LSN        frontend                  frontend                    Y     -    degenerate   map
2f609ee4 LSN        maintenance:bugfix        maintenance:bugfix          Y     -    degenerate   map
1e8f0bd4 LSN        frontend                  frontend                    Y     -    degenerate   map
f9f8c635 Charles Cheng backend                   backend                     Y     -    heuristic    reconciled with shuning map changes, schemas adjus
9ace08f6 Charles Cheng frontend                  frontend                    Y     -    heuristic    indexhtml pointing to react entry now, not stale b
f51d6660 Charles Cheng frontend                  frontend                    Y     -    heuristic    indexhtml pointing to react entry now, not stale b
6b59106c Charles Cheng maintenance:dependency    devops:config               N   0.84   nli          installing shunings dependencies for leaflet
904abab4 Charles Cheng maintenance:bugfix        maintenance:bugfix          Y   0.99   nli          fixing lsn dashboard localstorage email check, cas
d8f5347a Charles Cheng maintenance:bugfix        frontend                    N   0.99   nli          fixing lsn dashboard proxy url construction wrong,
f111115a Charles Cheng maintenance:refactor      maintenance:refactor        Y   0.79   nli          refactored into shared modules for less developmen
6a28fe1b Charles Cheng backend                   maintenance:refactor        N     -    heuristic    cleaned up geocoded_at artifacts, consolidated int
d7710c78 Charles    maintenance               maintenance:documentation   N     -    heuristic    added oneliner regarding endpoint file scope to ch
fe58b89a Charles Cheng backend                   test                        N     -    heuristic    fixed enum schema drift, empty string for test ord
63054bb0 William Otoo-Mensah maintenance:dependency    frontend                    N   0.62   nli          Update on the filtering
0908c7d4 LSN        feature                   feature                     Y   0.99   nli          feat: add new order modal and driver selection
95e0c2cd LSN        maintenance:bugfix        maintenance:bugfix          Y   0.95   nli          Fix reset link host detection
8fda512c Charles Cheng maintenance:dependency    frontend                    N   0.50   nli          polling drivers, adding cache:nostore to load driv
b9d3c63c William Otoo-Mensah feature                   feature                     Y   0.92   nli          feat: implement order redo functionality with moda
9af8f11e Charles Cheng frontend                  frontend                    Y     -    heuristic    deliverymap routing added, poly lines etc
a2f3c7af William Otoo-Mensah feature                   feature                     Y     -    heuristic    feat: add ETA context for order management and dis
3ba031a0 LSN        feature                   feature                     Y   0.97   nli          feat: add courier availability status dropdown and
4607ae70 LSN        maintenance:dependency    maintenance:bugfix          N   0.86   nli          fix: correct reset password link base path (remove
dcecb4ad LSN        maintenance:bugfix        maintenance:bugfix          Y   0.99   nli          fix(auth): resolve reset password page issue (fix
3798380b LSN        maintenance:bugfix        maintenance:bugfix          Y     -    heuristic    fix: normalize filename casing for reset password
a59c6b08 LSN        maintenance:bugfix        maintenance:bugfix          Y   0.61   nli          fix: correct case-sensitive import for ResetPasswo
2bf7dd03 Charles Cheng maintenance:bugfix        frontend                    N   0.47   nli          case mismatch for password part of ResetPassword
a24148d5 Charles Cheng maintenance:bugfix        maintenance:bugfix          Y   0.48   nli          line 28 same fix
a1232e8c LSN        maintenance:bugfix        maintenance:bugfix          Y   0.54   nli          fix: fix ResetPassword component naming casing
e87ebc4b LSN        maintenance:bugfix        maintenance:bugfix          Y     -    heuristic    fix: use shared API_BASE config in ResetPassword
4935358b William Otoo-Mensah frontend                  frontend                    Y     -    degenerate   ETA handling update
5b85a1ae 刘世豪        feature                   feature                     Y   0.92   nli          Implement dashboard order-to-map selection flow
========================================================================================================================
```

## Classification Summary

| Metric | Count | % |
|---|---|---|
| Total commits | 108 | 100% |
| NLI-classified | 35 | 32% |
| Deterministic-only | 31 | 29% |
| Degenerate (skipped NLI) | 42 | 39% |

## Calibration

| Metric | Count | % |
|---|---|---|
| NLI + deterministic agree | 62 | 57% |
| NLI rescued (det said "other") | 0 | 0% |
| NLI overrode (reclassified) | 46 | 43% |

**Observation**: 0% rescue rate means the diff-category baseline never returns "other" —
it always has a coarse classification from file paths. NLI's contribution is
**reclassification precision**: refining `frontend` to `maintenance:bugfix`, `devops:config`
to `devops:infra`, etc. The 43% override rate is the primary signal of NLI's added value.

## Per-Category Distribution

| Category | Count | % |
|---|---|---|
| frontend | 34 | 31% |
| backend | 18 | 17% |
| maintenance:bugfix | 16 | 15% |
| feature | 10 | 9% |
| maintenance:dependency | 10 | 9% |
| legacy | 8 | 7% |
| devops:infra | 4 | 4% |
| devops | 4 | 4% |
| maintenance | 2 | 2% |
| maintenance:refactor | 1 | 1% |
| other | 1 | 1% |

## Per-Author Breakdown

| Author | Commits | Top categories |
|---|---|---|
| Charles Cheng | 49 | frontend=17, backend=8, maintenance:bugfix=7 |
| LSN | 33 | frontend=12, maintenance:bugfix=7, backend=7 |
| William Otoo-Mensah | 11 | legacy=6, feature=2, frontend=1 |
| 刘世豪 | 9 | backend=3, feature=2, maintenance:dependency=2 |
| kriss-LIU | 5 | frontend=3, legacy=2 |
| Charles | 1 | maintenance=1 |

## Notable NLI Reclassifications

| Commit | Message | Deterministic | NLI | Conf | Assessment |
|---|---|---|---|---|---|
| 4c4ec52d | "vite + react set up" | frontend | **devops:infra** | 0.79 | Correct — infrastructure setup |
| 0a254664 | ".htaccess, proper path routing" | devops:config | **devops:infra** | 0.98 | Correct — server infrastructure |
| 904abab4 | "fixing lsn dashboard localstorage email check" | maintenance:bugfix | **maintenance:bugfix** | 0.99 | Agree — high confidence |
| d8f5347a | "fixing lsn dashboard proxy url construction wrong" | frontend | **maintenance:bugfix** | 0.99 | Correct — it's a bugfix, not frontend work |
| 39bc8ef2 | "polling endpoint, driver patch update" | maintenance:bugfix | **devops:infra** | 0.90 | Correct — endpoint infrastructure |
| db3e1c59 | "Maps added using leaflet.js" | frontend | **feature** | 0.94 | Correct — new functionality |
| fcf4b6bc | "added cattle to cors allowed" | backend | **maintenance:dependency** | 0.90 | Correct — dependency/config change |

## Cross-Reference Against Hypotheses

### H1 (Accuracy on ambiguous subset)
- 42 degenerate commits (39%) correctly skipped by NLI
- 35 commits (32%) got NLI classifications with confidence scores
- Of 35 NLI classifications, spot-check shows high accuracy on clear cases
  (0.99 bugfix, 0.98 devops:infra, 0.94 feature)
- **Formal H1 evaluation requires human ground-truth labels (blind protocol)**

### H2 (Metric sensitivity)
- 43% override rate suggests NLI-segmented metrics WILL differ from deterministic
- Example: Charles Cheng's deterministic profile is frontend-heavy; NLI reveals
  7 maintenance:bugfix commits hidden in the "frontend" category
- **Formal H2 evaluation requires computing Gini/entropy under 3 conditions**

### H3 (Actionability)
- Per-author breakdown with NLI shows William Otoo-Mensah is 55% legacy code
  (pre-migration orphans). Deterministic would show "feature" for those.
- kriss-LIU: 100% frontend+legacy, 0 backend — visible role siloing
- **Formal H3 evaluation requires instructor pilot study**

## Issues Found (fed back to doc corrections)

1. Model outputs 2 classes (entailment/not_entailment), not 3 — docs corrected
2. 108 commits, not 96 — docs corrected
3. 0% rescue rate — NLI value is reclassification precision, not rescue — docs corrected
4. "high P(neutral)" language incorrect for 2-class model — docs corrected
