---
name: datahub-ml-lineage
description: Trace and reason about end-to-end ML lineage in DataHub — from training data through features to models and deployments. Use when asked which data feeds a model, what breaks if a column changes, whether a feature leaks its label, or whether offline and online feature definitions agree.
---

# DataHub ML Lineage

DataHub's shipped skills cover search, table lineage, enrichment and quality. None of them
cross into ML entities, so an agent asked "what data actually feeds this model?" tends to
stop at the feature table and guess the rest.

This skill teaches the traversal, and — more importantly — the two places it goes wrong.

## The chain

ML lineage in DataHub is four links, and only the first is ordinary table lineage:

```
Dataset.column ──FineGrainedLineage──▶ Dataset.column   (column-level, inside the warehouse)
Dataset ────────MLFeatureProperties.sources───────────▶ MLFeature
MLFeature ──────MLFeatureTableProperties.mlFeatures───▶ MLFeatureTable
MLFeature ──────MLModelProperties.mlFeatures──────────▶ MLModel
MLModel ────────MLModelProperties.deployments─────────▶ MLModelDeployment
MLModel ────────MLModelProperties.trainingJobs────────▶ DataJob
```

## Trap 1: the granularity break

**`MLFeatureProperties.sources` points at datasets, not columns.**

`FineGrainedLineage` gives you column-level precision inside the warehouse, and then you
cross into ML entities and lose it. An agent that keeps reasoning at column level past that
boundary is inventing precision the graph does not have.

When you need to know *which column* a feature came from, resolve it explicitly and say how
confident you are:

1. **Declared** — the feature's `customProperties` name their source columns. Trust it.
2. **Name match** — a column in one of `sources` shares the feature's name. This is how
   feature stores that materialise from a table behave, so it is usually right.
3. **Dataset-wide** — no column-level signal. The feature depends on *some* column of those
   datasets. Report the conclusion as dataset-level, and do not claim otherwise.

Never silently pick one column out of a source dataset because it looks plausible.

## Trap 2: features are not their names

The same logical feature usually exists twice — once offline for training, once online for
serving — as two distinct `MLFeature` entities in two different feature tables. They are
*supposed* to be identical. Whether they actually are is exactly what nobody checks.

When comparing two paths, compare the **transformations**, not the node names. The offline
and online copies necessarily live in different tables, so any comparison that includes node
identity reports every feature as divergent. Compare the ordered chain of transform
operations, and ignore value-preserving hops (`identity`, `passthrough`) — one path routing
through an extra staging layer is different plumbing, not a different value.

## Workflows

### "What data feeds this model?"

1. `get_entities` on the model URN → read `mlFeatures`.
2. `get_entities` on each feature → read `sources`.
3. `get_lineage` upstream from each source dataset, `direction=UPSTREAM`.
4. For column precision, `get_lineage_paths_between` a candidate source column and the
   feature's dataset — and apply the tiered resolution above before naming columns.

State the deployment status. "Feeds a model" and "feeds a model serving live traffic" are
different answers to the same question.

### "What breaks if I change this column?"

1. `get_lineage` downstream from the column.
2. Filter the result to `mlFeature` and `mlModel` entities — those are the consumers that
   will not raise an error when they break.
3. For each model reached, check `deployments`.

A column change that reaches a deployed model is not a schema question, it is an incident.

### "Does this feature leak the label?"

1. Identify the target column: a `Label` glossary term, or a custom property on the model
   naming it. If neither exists, **say you cannot determine the label** — do not infer it
   from a column called `churned`, `target` or `y`.
2. `get_lineage_paths_between(label_column, feature)`.
3. A path means the feature is derived from the answer. Report the path itself; it is the
   evidence, and it is what makes the finding actionable rather than an accusation.

### "Do offline and online agree?"

1. Find both `MLFeature` entities for the logical feature.
2. Walk each back to shared source columns.
3. Compare transform chains from a shared source, normalised as described in Trap 2.
4. Report the first hop where they differ. That is the line of code to fix.

## Reporting

- Lead with the model and whether it is deployed.
- Show the path. A lineage claim without its path is an assertion; with it, it is evidence.
- Name the confidence tier whenever the answer crossed the granularity break.
- If the graph lacks the ML entities entirely, say so — an instance with no `mlModel`
  entities cannot answer these questions, and that is worth reporting plainly rather than
  approximating from table lineage.

## Contributing back

When you establish something durable — that a feature leaks, that two paths diverged, that a
model depends on restricted data — write it back with `add_tags`, `add_structured_properties`
or `update_description`, so the next agent inherits the conclusion instead of re-deriving it.

---

Extracted from [Faultline](https://github.com/madhavvan/faultline), which uses these
traversals to prove structural ML defects from the DataHub graph. Apache-2.0.
