# Faultline scan report

- **Graph source:** `replay:demo-graph.json`
- **Models scanned:** 1
- **Features scanned:** 9
- **Detectors run:** compliance-propagation, silent-semantic-change, target-leakage, train-serve-skew
- **Findings:** 4

| | Finding | Model | Proof |
|---|---|---|---|
| 🔴 **CRITICAL** | `warehouse.main_raw.raw_orders.amount` changed (currency change) beneath `churn_propensity_v7` | `churn_propensity_v7` | 5 hop(s) |
| 🔴 **CRITICAL** | Feature `customer_features_offline.segment_churn_rate` derives from label `warehouse.main_marts.customer_360.churned` (3 hops) | `churn_propensity_v7` | 3 hop(s) |
| 🟠 **HIGH** | PII column reaches `churn_propensity_v7` | `churn_propensity_v7` | 5 hop(s) |
| 🟠 **HIGH** | Offline and online `avg_order_value_30d` are computed differently | `churn_propensity_v7` | 3 hop(s) |

## 🔴 CRITICAL — `warehouse.main_raw.raw_orders.amount` changed (currency change) beneath `churn_propensity_v7`

**Model:** `churn_propensity_v7` · **Status:** deployed

The documented currency changed from `usd` to `eur`. Values in this column now mean something different while remaining numerically plausible. Model `churn_propensity_v7` depends on this column through 5 lineage hop(s). The pipeline did not fail and the values remain plausible, so nothing else will flag this.

**Proof** — 5 lineage hop(s) _(weakest-link confidence 0.90)_

```
warehouse.main_raw.raw_orders.amount
  ↓  ROUND(amount * 1.0, 2)
warehouse.main_staging.stg_orders.amount_usd
  ↓  AVG(CASE WHEN order_date >= CAST('2026-07-01' AS DATE) - 30 THEN amount_usd END)
warehouse.main_marts.customer_360.avg_order_value_30d
  ↓  identity
warehouse.main_features.customer_features_offline.avg_order_value_30d
  ↓  feature materialisation (name_match)  (confidence 0.90)
customer_features_offline.avg_order_value_30d
  ↓  model input
churn_propensity_v7
```

**Fix:** Confirm whether `churn_propensity_v7` was retrained after this change. If not, its training distribution no longer matches its inputs -- retrain, or revert the upstream change.

<details><summary>Evidence · <code>b43337ba3264d950</code></summary>

```json
{
  "after": {
    "description": "Order gross total in EUR.",
    "native_type": "DECIMAL(18,2)",
    "nullable": true,
    "tags": [],
    "terms": []
  },
  "baseline_captured_at": "2026-07-27T21:27:52.636323+00:00",
  "before": {
    "description": "Order gross total in USD.",
    "native_type": "DECIMAL(18,2)",
    "nullable": true,
    "tags": [],
    "terms": []
  },
  "change_kind": "currency change",
  "model_deployed": true
}
```

</details>

## 🔴 CRITICAL — Feature `customer_features_offline.segment_churn_rate` derives from label `warehouse.main_marts.customer_360.churned` (3 hops)

**Model:** `churn_propensity_v7` · **Feature:** `customer_features_offline.segment_churn_rate` · **Status:** deployed

There is a lineage path from the prediction target `warehouse.main_marts.customer_360.churned` to `customer_features_offline.segment_churn_rate`, a feature of model `churn_propensity_v7`. The feature encodes the answer, so offline accuracy is inflated and will not survive contact with production data, where the label is not yet known.

**Proof** — 3 lineage hop(s) _(weakest-link confidence 0.90)_

```
warehouse.main_marts.customer_360.churned
  ↓  AVG(CASE WHEN churned THEN 1.0 ELSE 0.0 END) OVER (PARTITION BY country)
warehouse.main_marts.customer_risk.segment_churn_rate
  ↓  identity
warehouse.main_features.customer_features_offline.segment_churn_rate
  ↓  feature materialisation (name_match)  (confidence 0.90)
customer_features_offline.segment_churn_rate
```

**Fix:** Break the dependency: recompute `customer_features_offline.segment_churn_rate` from inputs that exclude `warehouse.main_marts.customer_360.churned`, or drop the feature. Re-evaluate the model afterwards -- the current metrics are not a measurement of predictive skill.

<details><summary>Evidence · <code>87229a250332ff93</code></summary>

```json
{
  "bridge_method": "name_match",
  "hops": 3,
  "label_identified_by": "declared",
  "model_deployed": true,
  "path_count": 1,
  "relationship": "feature_derives_from_label",
  "transforms": [
    "AVG(CASE WHEN churned THEN 1.0 ELSE 0.0 END) OVER (PARTITION BY country)",
    "identity",
    "feature materialisation (name_match)"
  ]
}
```

</details>

## 🟠 HIGH — PII column reaches `churn_propensity_v7`

**Model:** `churn_propensity_v7` · **Status:** deployed (churn-propensity-prod)

Column `warehouse.main_raw.raw_customers.email` is classified PII and flows into model `churn_propensity_v7`, which is served at churn-propensity-prod. Neither the model nor its features carry that classification, so the restriction is not visible to anyone reviewing them.

**Proof** — 5 lineage hop(s) _(weakest-link confidence 0.90)_

```
warehouse.main_raw.raw_customers.email
  ↓  SPLIT_PART(email, '@', 2)
warehouse.main_staging.stg_customers.email_domain
  ↓  identity
warehouse.main_marts.customer_360.email_domain
  ↓  identity
warehouse.main_features.customer_features_offline.email_domain
  ↓  feature materialisation (name_match)  (confidence 0.90)
customer_features_offline.email_domain
  ↓  model input
churn_propensity_v7
```

**Fix:** Either propagate PII onto the intermediate features and `churn_propensity_v7` so the restriction travels with the data, or remove the column from the model's lineage. Faultline can apply the propagation for you with `--write-back`.

<details><summary>Evidence · <code>7d89b57a909df2c4</code></summary>

```json
{
  "classifications": [
    "urn:li:glossaryTerm:PII"
  ],
  "deployments": [
    "urn:li:mlModelDeployment:(urn:li:dataPlatform:sagemaker,churn-propensity-prod,PROD)"
  ],
  "hops": 5,
  "model_deployed": true,
  "model_existing_tags": [],
  "model_existing_terms": []
}
```

</details>

## 🟠 HIGH — Offline and online `avg_order_value_30d` are computed differently

**Model:** `churn_propensity_v7` · **Feature:** `customer_features_offline.avg_order_value_30d` · **Status:** deployed

Feature `avg_order_value_30d` is derived one way for training and another way for serving. The paths agree for 1 hop(s) from `warehouse.main_raw.raw_orders.amount`, then split: the offline path applies `AVG(CASE WHEN order_date >= CAST('2026-07-01' AS DATE) - 30 THEN amount_usd END)` while the online path applies `AVG(CASE WHEN order_date >= CAST('2026-07-01' AS DATE) - 7 THEN amount_usd END)`. The model was trained on one definition and is scored against the other, so every live prediction is made on inputs the model never saw during training.

**Proof 1** — 3 lineage hop(s) _(weakest-link confidence 0.90)_

```
warehouse.main_raw.raw_orders.amount
  ↓  ROUND(amount * 1.0, 2)
warehouse.main_staging.stg_orders.amount_usd
  ↓  AVG(CASE WHEN order_date >= CAST('2026-07-01' AS DATE) - 7 THEN amount_usd END)
warehouse.main_features.customer_features_online.avg_order_value_30d
  ↓  feature materialisation (name_match)  (confidence 0.90)
customer_features_online.avg_order_value_30d
```

**Proof 2** — 4 lineage hop(s) _(weakest-link confidence 0.90)_

```
warehouse.main_raw.raw_orders.amount
  ↓  ROUND(amount * 1.0, 2)
warehouse.main_staging.stg_orders.amount_usd
  ↓  AVG(CASE WHEN order_date >= CAST('2026-07-01' AS DATE) - 30 THEN amount_usd END)
warehouse.main_marts.customer_360.avg_order_value_30d
  ↓  identity
warehouse.main_features.customer_features_offline.avg_order_value_30d
  ↓  feature materialisation (name_match)  (confidence 0.90)
customer_features_offline.avg_order_value_30d
```

**Fix:** Reconcile the two definitions of `avg_order_value_30d` at the fork point, then backfill or retrain. Longer term, compute the feature once and materialise both stores from that single definition so the paths cannot drift again.

<details><summary>Evidence · <code>e1662b7ddfe6a345</code></summary>

```json
{
  "fork_after_hops": 1,
  "logical_feature": "avg_order_value_30d",
  "model_deployed": true,
  "offline_feature": "urn:li:mlFeature:(customer_features_offline,avg_order_value_30d)",
  "offline_next_node": "warehouse.main_marts.customer_360.avg_order_value_30d",
  "offline_signature": "COLUMN_LINEAGE:ROUND(amount * 1.0, 2)|COLUMN_LINEAGE:AVG(CASE WHEN order_date >= CAST('2026-07-01' AS DATE) - 30 THEN amount_usd END)|FEATURE_SOURCE",
  "offline_transform": "AVG(CASE WHEN order_date >= CAST('2026-07-01' AS DATE) - 30 THEN amount_usd END)",
  "online_feature": "urn:li:mlFeature:(customer_features_online,avg_order_value_30d)",
  "online_next_node": "warehouse.main_features.customer_features_online.avg_order_value_30d",
  "online_signature": "COLUMN_LINEAGE:ROUND(amount * 1.0, 2)|COLUMN_LINEAGE:AVG(CASE WHEN order_date >= CAST('2026-07-01' AS DATE) - 7 THEN amount_usd END)|FEATURE_SOURCE",
  "online_transform": "AVG(CASE WHEN order_date >= CAST('2026-07-01' AS DATE) - 7 THEN amount_usd END)",
  "shared_root": "urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:duckdb,warehouse.main_raw.raw_orders,PROD),amount)"
}
```

</details>

<sub>Faultline · 2 critical, 2 high in 0.00s · graph `replay:demo-graph.json` · every finding above is a path that exists in the DataHub lineage graph, not a statistical inference.</sub>