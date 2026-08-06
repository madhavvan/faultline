# Faultline

**Your model didn't drift. Your graph did.**

[![Faultline](https://github.com/madhavvan/faultline/actions/workflows/faultline.yml/badge.svg)](https://github.com/madhavvan/faultline/actions/workflows/faultline.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

Faultline finds ML failures that are **structural** — provable from the DataHub lineage
graph, and by construction invisible to anything that watches the data distribution.

A leaked feature is not out-of-distribution. A train/serve skew keeps both copies inside
their historical ranges. A currency column that switches from USD to EUR stays perfectly
numeric. Nothing errors, nothing drifts, no alert fires. The model is just quietly wrong,
and stays that way until someone reconciles the revenue.

All three are a short walk in the lineage graph.

```
                                    ┌─ drift monitors watch this ─┐
   raw.orders.amount ──▶ stg_orders ──▶ customer_360 ──▶ feature ──▶ model ──▶ endpoint
        │                                                                          │
        └──────────────── nobody watches the shape of this ────────────────────────┘
```

---

## Try it in ten seconds

No DataHub, no Docker, no credentials:

```bash
pip install -e .
faultline demo --explain
```

Or run it in the browser with nothing installed at all — the container comes up
with the demo ready:

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/madhavvan/faultline?quickstart=1)

Faultline replays a real dbt/DuckDB warehouse, applies four changes any team would merge on
a Tuesday, and proves what they did to the production model. Every finding comes with the
lineage path that substantiates it.

The lineage is **parsed from compiled SQL**, not declared: proofs carry the actual
expressions.

```
warehouse.main_raw.raw_orders.amount
  ↓  ROUND(amount * 1.0, 2)
warehouse.main_staging.stg_orders.amount_usd
  ↓  AVG(CASE WHEN order_date >= CAST('2026-07-01' AS DATE) - 30 THEN amount_usd END)
warehouse.main_marts.customer_360.avg_order_value_30d
```

## Why a drift monitor cannot find this

The demo's train/serve skew is a 30-day window offline against a 7-day window online. Run
`python demo/measure_skew.py` against the built warehouse and it reports:

| What a distribution monitor compares | |
|---|---|
| shift in the mean | **1.4%** — no alert fires |
| correlation | 0.881 |

| What the model is actually served | |
|---|---|
| mean per-row error | **20.7%** |
| rows more than 10% wrong | **51.3%** |

The aggregates agree to within a couple of percent while half the served rows are materially
wrong. That gap is the entire argument. This defect is invisible in the data and unambiguous
in the graph.

---

## What it proves

| Detector | The defect | Why nothing else catches it |
|---|---|---|
| `target-leakage` | A feature is computed, however indirectly, from the label | The feature looks perfect. So does the model — that's the problem. |
| `train-serve-skew` | A feature's offline and online derivations have diverged | Both copies stay in range. Only the *paths* differ. |
| `silent-semantic-change` | An upstream column changed meaning beneath a live model | Values stay plausible; the pipeline never fails. |
| `compliance-propagation` | Restricted data reaches a deployed model | Every asset is individually compliant. The violation is only visible end to end. |

### The distinction that matters

Faultline does **not** score, sample, or infer. Detectors are deterministic graph
algorithms, and every finding carries a `ProofPath` — an ordered chain of lineage edges that
either exists in DataHub or does not:

```
marts.customer_360.churned            ← the prediction target
  ↓  avg(churned) over segment
marts.customer_risk.segment_churn_rate
  ↓  identity
features.customer_features_offline.segment_churn_rate
  ↓  feature materialisation (name_match)
customer_features_offline.segment_churn_rate
  ↓  model input
churn_propensity_v7                   ← 0.94 AUC, and it was never real
```

A reviewer can read that without trusting the tool. If a detector ever emits a finding whose
proof does not hold together, the engine drops it and says so — Faultline will not report a
defect it cannot substantiate.

The LLM layer explains and triages what the detectors prove. It never creates a finding.

---

## How it uses DataHub

Faultline is built on the open-source metadata graph, and it contributes back to it.

**Reads** — column-level `FineGrainedLineage`, `SchemaMetadata` merged with
`EditableSchemaMetadata` (governance labels applied in the UI live there, and missing them
would blind the compliance detector), plus the full ML chain:
`MLFeatureProperties.sources` → `MLFeatureTableProperties.mlFeatures` →
`MLModelProperties.mlFeatures` → `MLModelProperties.deployments`.

**The bridge.** DataHub's `MLFeatureProperties.sources` points at *datasets*, while
`FineGrainedLineage` operates on *columns*. Nothing in the open-source model joins those two
granularities — so a naive traversal loses column precision exactly where it crosses into ML
entities, which is exactly where ML defects live. Faultline bridges the gap with a tiered
resolver that degrades confidence rather than guessing:

| Tier | How | Confidence |
|---|---|---|
| explicit | the feature declares `faultline.source_columns` | 1.0 |
| name match | a source-dataset column shares the feature's name | 0.9 |
| dataset wide | no column-level signal; attribute to all columns | 0.4 |

Findings resting only on the lowest tier are **capped below blocking severity**. Faultline
would rather under-report than page someone on evidence it cannot substantiate at column
level.

**Writes back** — tags, structured properties, DataHub incidents, an institutional-memory
link, and a `DataProcessInstance` recording the scan itself, so the next person (or agent)
inherits the finding instead of rediscovering it. `faultline writeback --dry-run` prints the
full plan before anything is sent; see `examples/writeback-plan.txt`.

---

## The agent

```bash
faultline triage --demo
```

Claude reads the proven findings and DataHub's surrounding context — through the **official
`mcp-server-datahub` MCP server**, a real MCP session, not a reimplementation — then writes a
plain-language consequence for each finding.

The tool surface is deliberately narrow. The agent can read what the detectors proved and
read the catalogue for context, and it has exactly one output channel. It **cannot create a
finding**, and it may move a severity by **at most one step**, with a stated reason recorded
in the finding's evidence. Structural claims come from deterministic traversal; the model
supplies judgement about consequence, which is the part it is good at.

[`examples/triage-report.md`](examples/triage-report.md) is a committed run against a live
DataHub, not a description of one. In it the agent moved severity in both directions and
said why each time:

- **PII, HIGH → MEDIUM** — *"the propagated value is a coarse domain fragment rather than an
  identifier"*, and the warehouse columns on the path already carry the PII term.
- **Train/serve skew, HIGH → CRITICAL** — *"a 4x window mismatch on one of only ~5 inputs
  corrupts every production prediction, not a subset."*

It also read context no detector supplies: that `customer_features_online` has no
`segment_churn_rate` column at all, so the leaked feature cannot even be served — and that
`raw_orders` already carries two open incidents, which Faultline itself had written back.

That run cost **$0.44** at Claude Opus 5 list pricing — 273K input tokens, 241K of them
served from cache. A tool loop resends the whole conversation every turn, so caching the
prefix is most of the bill: without it the same run costs $1.47.

If the MCP server is unreachable, the agent runs on the proofs alone and says so, rather than
speculating to fill the gap.

---

## Contributing back to DataHub

DataHub's skills registry covers the catalogue — setup, search, lineage, enrich, quality —
and connector development. Its `datahub-lineage` skill enumerates exactly three entity
types: dataset, dashboard, chart. Nothing in the registry crosses into ML entities.

[`skills/datahub-ml-lineage/`](skills/datahub-ml-lineage/) is written for upstream
contribution in their format: it teaches an agent the
dataset → feature → model → deployment traversal, and the two traps that make naive versions
wrong (the column/dataset granularity break, and comparing feature paths by node identity).

---

## Against a live DataHub

```bash
datahub docker quickstart          # DataHub's own one-command local stack
faultline emit                     # push the demo graph into it
faultline doctor                   # confirm what the instance now holds
faultline scan                     # scan it for real
faultline writeback --apply        # contribute the findings back
```

`python demo/verify_live.py` runs the whole loop as an assertion: emit → read back through
the ordinary client → scan → compare fingerprints against the offline run → write back →
**read the tags, structured properties and incidents back out of DataHub**. Every step is
checked against what the server returns, not against what was sent. If the live and offline
runs ever disagree, that check fails.

## Commands

```bash
faultline demo                     # bundled pipeline, no DataHub needed
faultline doctor                   # check connectivity, show what the instance holds
faultline emit                     # push a graph into DataHub
faultline scan                     # scan a live DataHub
faultline scan --fixture graph.json
faultline scan --fixture demo/warehouse    # a dbt project works too
faultline baseline before.json     # capture column semantics
faultline scan --baseline before.json
faultline check --fail-on HIGH     # CI gate; exits non-zero on new blocking findings
faultline accept                   # baseline existing findings so CI gates only on new ones
faultline capture graph.json       # record a live graph as a replay fixture
faultline detectors                # list what's registered
```

### Offline replay

`faultline capture` records a live DataHub graph to disk; `--fixture` replays it. Both paths
build the identical `GraphSnapshot`, so detectors cannot tell the difference — a property
the test suite asserts directly. This is what lets anyone evaluate Faultline without
standing up 14 containers.

---

## Configuration

Optional `.faultline.yml`; every threshold has a working default.

```yaml
datahub:
  server: http://localhost:8080

detectors:
  label_term: "urn:li:glossaryTerm:Label"     # how your team marks prediction targets
  restricted_terms:                            # what counts as restricted
    - "urn:li:glossaryTerm:PII"
  max_depth: 16

ci:
  fail_on: HIGH
  fail_on_new_only: true                       # existing debt doesn't block the next PR
```

---

## The demo warehouse

`demo/warehouse/` is a real dbt project on DuckDB — three seeds, seven models, an offline and
an online feature table. The four faults live in the SQL itself and are toggled by a single
dbt var, so one project renders both worlds and they cannot drift apart:

```bash
make warehouse   # builds it twice: clean first, then with all four faults
make fixtures    # captures both as the fixtures `faultline demo` replays
```

Column lineage is extracted by running **DataHub's own `sqlglot_lineage`** over each model's
compiled SQL — the same parser DataHub's dbt ingestion uses — so the edges Faultline
traverses are the edges DataHub would produce from the same project. Two normalisations make
those edges comparable, and both are load-bearing:

- The parser's `is_direct_copy` becomes `identity`, so a column passed through a staging
  layer is not mistaken for a transformation.
- Table qualifiers are stripped, because the same aggregate over two different CTE aliases is
  the same operation. Without this, every correctly-built feature pair reports as diverged.

What survives normalisation is real: a 30-day and a 7-day window compile to different date
literals, and the skew finding quotes both.

dbt describes tables and columns; it has no notion of a feature, model or endpoint. Those are
declared in [`demo/ml.yml`](demo/ml.yml), standing in for the feature store and model
registry that would emit them in a real deployment. A feature whose backing column does not
exist is skipped — which is why the clean build, where `dbt` omits `segment_churn_rate`
entirely, produces no leaky feature rather than a dangling one.

## Development

```bash
uv venv && uv pip install -e ".[dev,demo,agent]"
make check      # lint + tests + demo
```

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
