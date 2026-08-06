# Faultline — Devpost submission

**Category:** Production ML Agents
**Tagline:** Your model didn't drift. Your graph did.

---

## Inspiration

Drift monitoring watches the data. Nobody watches the *shape* of the pipeline — and most
silent ML failures live there.

A leaked feature is not out-of-distribution. A train/serve skew keeps both copies inside
their historical ranges. A currency column that switches from USD to EUR stays perfectly
numeric. Nothing errors, nothing drifts, no alert fires. The model is quietly wrong and stays
that way until someone reconciles the revenue.

All of those are a short walk in a lineage graph. DataHub already stores that graph. The gap
was that nothing walked it looking for *ML* defects specifically.

We measured this on our own demo warehouse. The train/serve skew we inject — a 30-day window
offline against a 7-day window online — produces:

| What a distribution monitor compares | |
|---|---|
| shift in the mean | **1.4%** — no alert fires |
| correlation | 0.881 |

| What the model is actually served | |
|---|---|
| mean per-row error | **20.7%** |
| rows more than 10% wrong | **51.3%** |

Half the served rows are materially wrong while the aggregates agree to within a couple of
percent. That gap is the whole project.

## What it does

Faultline proves four classes of structural ML defect from the DataHub metadata graph:

| Detector | The defect |
|---|---|
| `target-leakage` | A feature is computed, however indirectly, from the label |
| `train-serve-skew` | A feature's offline and online derivations have diverged |
| `silent-semantic-change` | An upstream column changed meaning beneath a live model |
| `compliance-propagation` | Restricted data reaches a deployed model unlabelled |

Every finding carries a **proof path** — an ordered chain of lineage edges that exists in
DataHub or does not. A reviewer can read it without trusting the tool:

```
marts.customer_360.churned                     ← the prediction target
  ↓  AVG(CASE WHEN churned THEN 1.0 ELSE 0.0 END) OVER (PARTITION BY country)
marts.customer_risk.segment_churn_rate
  ↓  identity
features.customer_features_offline.segment_churn_rate
  ↓  feature materialisation (name_match)
customer_features_offline.segment_churn_rate
  ↓  model input
churn_propensity_v7                            ← 0.94 AUC, and it was never real
```

It then **writes the finding back into DataHub** — tags, structured properties, an incident
carrying the proof, an institutional-memory link, and a `DataProcessInstance` recording the
scan — so the next person or agent inherits the conclusion instead of rediscovering it.

## How we built it

**Detectors are deterministic graph algorithms.** No sampling, no scoring, no inference. The
engine drops any finding whose own proof does not hold together rather than report a defect
it cannot substantiate.

**The ML lineage bridge.** DataHub's `MLFeatureProperties.sources` points at *datasets* while
`FineGrainedLineage` operates on *columns*. Nothing in the open-source model joins those two
granularities — so a naive traversal loses column precision exactly where it crosses into ML
entities, which is exactly where ML defects live. Faultline bridges it with a tiered resolver
(explicit declaration → name match → dataset-wide) that **degrades confidence rather than
guessing**, and caps findings that rest only on the weakest tier below blocking severity.

**Lineage parsed from real SQL.** The demo is a genuine dbt project on DuckDB. Column lineage
is extracted by running **DataHub's own `sqlglot_lineage`** over each compiled model, so the
edges Faultline traverses are the edges DataHub would produce from the same project. Proof
paths quote the actual expressions.

**The agent reads DataHub through its own MCP server.** A real MCP session against
`mcp-server-datahub`, converted for the Anthropic tool runner. The agent explains consequence
and may move a severity by **at most one step with a recorded reason** — it cannot create a
finding. `examples/triage-report.md` is a committed run against a live instance: it moved PII
down to MEDIUM ("a coarse domain fragment rather than an identifier") and train/serve skew up
to CRITICAL ("a 4x window mismatch on one of only ~5 inputs corrupts every production
prediction"), and it found from the catalogue that the leaked feature has no column in the
online store at all. The run cost $0.44 — 273K input tokens, 241K served from prompt cache,
against $1.47 for the same run uncached.

## Challenges

Four bugs, each invisible in a demo and fatal in use:

- **Path signatures included node identity.** A feature's offline and online copies
  necessarily live in different tables, so every pair compared as divergent — skew fired on
  everything.
- **`identity` pass-through hops counted as divergence.** One path routing through a mart
  while the other read from staging looked like a defect. Fixed by treating value-preserving
  hops as the no-ops they are.
- **Transform text compared with table qualifiers.** `AVG("stg_tickets"."resolution_hours")`
  versus the same aggregate over a CTE alias — same operation, different text. Three
  correctly-built feature pairs reported as diverged.
- **Cascading findings.** A currency change reports at every column downstream of it. True,
  and useless. Findings now collapse to their root cause — the column a human would actually
  go and change.

The through-line: on real data, the hard part is not finding defects. It is *not* reporting
the ones that are not there.

## Accomplishments

- **Exactly four findings for four injected faults** — no false positives, no cascade noise,
  asserted directly in the test suite so it cannot silently regress.
- **Live and offline runs produce identical fingerprints**, verified by
  `demo/verify_live.py`, which emits into DataHub, reads the graph back through the ordinary
  client, scans it, writes back, and then **reads the tags, properties and incidents back
  out** to confirm they landed.
- A **new skill** for DataHub's skills registry, written in its format. The registry's own
  `datahub-lineage` skill enumerates three entity types — dataset, dashboard, chart — and
  nothing in the registry crosses into ML entities.

## What we learned

The graph is a *better* substrate for some questions than the data is. Structural claims are
cheap to verify and impossible to fake — a path exists or it doesn't — which makes them a
much better foundation for an agent than statistical inference. Letting the model explain
proven facts, rather than produce them, is what makes the output trustworthy.

## What's next

- Upstream the `datahub-ml-lineage` skill.
- Column-level lineage inside feature stores, so the bridge's weakest tier is needed less.
- A "prove it before you merge" mode that runs the proposed model against a shadow build.

## Built with

Python · DataHub (OSS metadata platform, `sqlglot_lineage`, MCP server) · dbt · DuckDB ·
Claude Opus 5 via the Anthropic tool runner · MCP · GitHub Actions

## Try it

```bash
pip install -e .
faultline demo --explain      # ten seconds, no DataHub required
```
