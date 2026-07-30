"""The demo world: a churn-prediction pipeline with four realistic, injected faults.

Every fault here is one a real data team has shipped. None of them are exotic; that is the
point. They survive code review, pass every test, keep the pipeline green, and are invisible
to distribution monitoring -- and each is a short walk in the lineage graph.

The same definition renders two ways: to a :class:`GraphSnapshot` for the offline demo, and
to DataHub metadata-change proposals for a live instance. One source of truth, so the demo
and the live path cannot drift.
"""

from __future__ import annotations

from typing import Any

import datahub.emitter.mce_builder as builder

from .graph.entities import Entity, GraphSnapshot, LineageEdge, SchemaField
from .models import NodeKind

PLATFORM = "duckdb"
ENV = "PROD"
FEATURE_PLATFORM = "feast"
MODEL_PLATFORM = "mlflow"
DEPLOY_PLATFORM = "sagemaker"

OFFLINE_TABLE = "customer_features_offline"
ONLINE_TABLE = "customer_features_online"
MODEL_NAME = "churn_propensity_v7"
ENDPOINT_NAME = "churn-propensity-prod"

LABEL_TERM = builder.make_term_urn("Label")
PII_TERM = builder.make_term_urn("PII")

#: The four faults, so the CLI and README can describe them without duplicating prose.
FAULTS = {
    "leakage": "segment_churn_rate is computed from the churn label itself",
    "skew": "avg_order_value_30d uses a 30-day window offline and a 7-day window online",
    "semantic": "raw.orders.amount changed from USD to EUR beneath the live model",
    "compliance": "a PII-classified email column reaches the deployed model",
}


def ds(name: str) -> str:
    return builder.make_dataset_urn(PLATFORM, name, ENV)


def col(dataset: str, column: str) -> str:
    return builder.make_schema_field_urn(ds(dataset), column)


def feat(name: str, table: str) -> str:
    return builder.make_ml_feature_urn(table, name)


def model_urn() -> str:
    return builder.make_ml_model_urn(MODEL_PLATFORM, MODEL_NAME, ENV)


def deployment_urn() -> str:
    return builder.make_ml_model_deployment_urn(DEPLOY_PLATFORM, ENDPOINT_NAME, ENV)


# --------------------------------------------------------------------------------------
# Table definitions
# --------------------------------------------------------------------------------------

Column = tuple[str, str, str | None, list[str]]  # name, type, description, terms


def _tables(currency: str) -> dict[str, list[Column]]:
    return {
        "raw.orders": [
            ("order_id", "BIGINT", "Primary key of the order", []),
            ("customer_id", "BIGINT", "Customer who placed the order", []),
            ("amount", "DECIMAL(18,2)", f"Order gross total in {currency}", []),
            ("status", "VARCHAR", "One of placed, shipped, cancelled, refunded", []),
            ("created_at", "TIMESTAMP", "When the order was placed", []),
        ],
        "raw.customers": [
            ("customer_id", "BIGINT", "Primary key of the customer", []),
            ("email", "VARCHAR", "Customer email address", [PII_TERM]),
            ("phone", "VARCHAR", "Customer phone number", [PII_TERM]),
            ("country", "VARCHAR", "ISO country code of the billing address", []),
            ("signup_at", "TIMESTAMP", "Account creation timestamp", []),
        ],
        "raw.support_tickets": [
            ("ticket_id", "BIGINT", "Primary key of the ticket", []),
            ("customer_id", "BIGINT", "Customer who opened the ticket", []),
            ("opened_at", "TIMESTAMP", "When the ticket was opened", []),
            ("resolved_at", "TIMESTAMP", "When the ticket was resolved", []),
            ("satisfaction_score", "INTEGER", "Post-resolution CSAT, 1-5", []),
        ],
        "staging.stg_orders": [
            ("order_id", "BIGINT", "Primary key of the order", []),
            ("customer_id", "BIGINT", "Customer who placed the order", []),
            ("amount_usd", "DECIMAL(18,2)", f"Order total converted from {currency}", []),
            ("is_completed", "BOOLEAN", "True when status is shipped", []),
            ("order_date", "DATE", "Date the order was placed", []),
        ],
        "staging.stg_customers": [
            ("customer_id", "BIGINT", "Primary key of the customer", []),
            ("email_hash", "VARCHAR", "SHA-256 of the customer email", [PII_TERM]),
            ("country", "VARCHAR", "ISO country code", []),
            ("tenure_days", "INTEGER", "Days since signup", []),
        ],
        "staging.stg_tickets": [
            ("ticket_id", "BIGINT", "Primary key of the ticket", []),
            ("customer_id", "BIGINT", "Customer who opened the ticket", []),
            ("resolution_hours", "DOUBLE", "Hours between opened_at and resolved_at", []),
            ("satisfaction_score", "INTEGER", "Post-resolution CSAT, 1-5", []),
        ],
        "marts.customer_360": [
            ("customer_id", "BIGINT", "Primary key of the customer", []),
            ("email_hash", "VARCHAR", "SHA-256 of the customer email", [PII_TERM]),
            ("avg_order_value_30d", "DECIMAL(18,2)", "Mean order value, trailing 30 days", []),
            ("order_count_90d", "BIGINT", "Completed orders, trailing 90 days", []),
            ("avg_resolution_hours", "DOUBLE", "Mean support resolution time", []),
            ("tenure_days", "INTEGER", "Days since signup", []),
            ("email_domain", "VARCHAR", "Domain part of the customer email", [PII_TERM]),
            ("churned", "BOOLEAN", "TARGET: no completed order in the last 90 days", [LABEL_TERM]),
        ],
        "marts.customer_risk": [
            ("customer_id", "BIGINT", "Primary key of the customer", []),
            (
                "segment_churn_rate",
                "DOUBLE",
                "Observed churn rate of this customer's segment",
                [],
            ),
        ],
        f"features.{OFFLINE_TABLE}": [
            ("customer_id", "BIGINT", "Entity key", []),
            ("avg_order_value_30d", "DECIMAL(18,2)", "Mean order value, trailing 30 days", []),
            ("order_count_90d", "BIGINT", "Completed orders, trailing 90 days", []),
            ("avg_resolution_hours", "DOUBLE", "Mean support resolution time", []),
            ("email_domain", "VARCHAR", "Domain part of the customer email", [PII_TERM]),
            ("segment_churn_rate", "DOUBLE", "Segment churn rate", []),
        ],
        f"features.{ONLINE_TABLE}": [
            ("customer_id", "BIGINT", "Entity key", []),
            ("avg_order_value_30d", "DECIMAL(18,2)", "Mean order value, trailing 30 days", []),
            ("order_count_90d", "BIGINT", "Completed orders, trailing 90 days", []),
            ("avg_resolution_hours", "DOUBLE", "Mean support resolution time", []),
            ("email_domain", "VARCHAR", "Domain part of the customer email", [PII_TERM]),
        ],
    }


def _column_lineage(inject_leakage: bool, inject_skew: bool) -> list[tuple]:
    """(upstream_table, upstream_col, downstream_table, downstream_col, transform)."""
    edges: list[tuple] = [
        # raw -> staging
        ("raw.orders", "order_id", "staging.stg_orders", "order_id", "identity"),
        ("raw.orders", "customer_id", "staging.stg_orders", "customer_id", "identity"),
        ("raw.orders", "amount", "staging.stg_orders", "amount_usd", "convert to reporting currency"),
        ("raw.orders", "status", "staging.stg_orders", "is_completed", "status = 'shipped'"),
        ("raw.orders", "created_at", "staging.stg_orders", "order_date", "cast to date"),
        ("raw.customers", "customer_id", "staging.stg_customers", "customer_id", "identity"),
        ("raw.customers", "email", "staging.stg_customers", "email_hash", "sha256(email)"),
        ("raw.customers", "country", "staging.stg_customers", "country", "identity"),
        ("raw.customers", "signup_at", "staging.stg_customers", "tenure_days", "datediff(now, signup_at)"),
        ("raw.support_tickets", "ticket_id", "staging.stg_tickets", "ticket_id", "identity"),
        ("raw.support_tickets", "customer_id", "staging.stg_tickets", "customer_id", "identity"),
        ("raw.support_tickets", "opened_at", "staging.stg_tickets", "resolution_hours", "datediff(resolved_at, opened_at)"),
        ("raw.support_tickets", "resolved_at", "staging.stg_tickets", "resolution_hours", "datediff(resolved_at, opened_at)"),
        ("raw.support_tickets", "satisfaction_score", "staging.stg_tickets", "satisfaction_score", "identity"),
        # staging -> marts
        ("staging.stg_customers", "customer_id", "marts.customer_360", "customer_id", "identity"),
        ("staging.stg_customers", "email_hash", "marts.customer_360", "email_hash", "identity"),
        ("staging.stg_customers", "tenure_days", "marts.customer_360", "tenure_days", "identity"),
        ("staging.stg_orders", "amount_usd", "marts.customer_360", "avg_order_value_30d", "avg over 30d window"),
        ("staging.stg_orders", "is_completed", "marts.customer_360", "order_count_90d", "count where is_completed"),
        ("staging.stg_orders", "order_date", "marts.customer_360", "churned", "max(order_date) < now - 90d"),
        ("staging.stg_tickets", "resolution_hours", "marts.customer_360", "avg_resolution_hours", "avg"),
        ("raw.customers", "email", "marts.customer_360", "email_domain", "split_part(email, '@', 2)"),
        # marts -> offline features
        ("marts.customer_360", "customer_id", f"features.{OFFLINE_TABLE}", "customer_id", "identity"),
        ("marts.customer_360", "avg_order_value_30d", f"features.{OFFLINE_TABLE}", "avg_order_value_30d", "identity"),
        ("marts.customer_360", "order_count_90d", f"features.{OFFLINE_TABLE}", "order_count_90d", "identity"),
        ("marts.customer_360", "avg_resolution_hours", f"features.{OFFLINE_TABLE}", "avg_resolution_hours", "identity"),
        ("marts.customer_360", "email_domain", f"features.{OFFLINE_TABLE}", "email_domain", "identity"),
        ("marts.customer_360", "email_domain", f"features.{ONLINE_TABLE}", "email_domain", "identity"),
        # staging -> online features (the serving path recomputes from staging)
        ("staging.stg_orders", "is_completed", f"features.{ONLINE_TABLE}", "order_count_90d", "count where is_completed"),
        ("staging.stg_tickets", "resolution_hours", f"features.{ONLINE_TABLE}", "avg_resolution_hours", "avg"),
        ("staging.stg_customers", "customer_id", f"features.{ONLINE_TABLE}", "customer_id", "identity"),
    ]

    # FAULT 2 -- the online path was re-pointed at a 7-day window during an incident and
    # never put back. Offline still trains on 30 days.
    online_window = "avg over 7d window" if inject_skew else "avg over 30d window"
    edges.append(
        ("staging.stg_orders", "amount_usd", f"features.{ONLINE_TABLE}",
         "avg_order_value_30d", online_window)
    )

    # FAULT 1 -- a segment-level churn rate computed from the label, then used as a feature.
    if inject_leakage:
        edges.extend([
            ("marts.customer_360", "customer_id", "marts.customer_risk", "customer_id", "identity"),
            ("marts.customer_360", "churned", "marts.customer_risk", "segment_churn_rate",
             "avg(churned) over segment"),
            ("marts.customer_risk", "segment_churn_rate", f"features.{OFFLINE_TABLE}",
             "segment_churn_rate", "identity"),
        ])

    # FAULT 4 -- the hashed email travels into the feature table and on into the model.
    edges.append(
        ("marts.customer_360", "email_hash", f"features.{OFFLINE_TABLE}", "customer_id",
         "entity resolution join key")
    )
    return edges


# --------------------------------------------------------------------------------------
# Snapshot rendering
# --------------------------------------------------------------------------------------


def build_world(
    *,
    inject_leakage: bool = True,
    inject_skew: bool = True,
    inject_compliance: bool = True,
    currency: str = "USD",
    source: str = "demo",
) -> GraphSnapshot:
    """Render the demo pipeline as a graph snapshot."""
    entities: dict[str, Entity] = {}

    for table, columns in _tables(currency).items():
        urn = ds(table)
        entities[urn] = Entity(
            urn=urn,
            kind=NodeKind.DATASET,
            name=table,
            platform=PLATFORM,
            description=f"dbt model `{table.split('.')[-1]}`",
            fields=[
                SchemaField(
                    field_path=name,
                    urn=col(table, name),
                    native_type=native,
                    type_class=native.split("(")[0],
                    description=desc,
                    terms=list(terms),
                )
                for name, native, desc, terms in columns
            ],
        )

    column_edges = [
        LineageEdge(
            upstream=col(su, sc),
            downstream=col(du, dc),
            transform=transform,
            confidence=1.0,
        )
        for su, sc, du, dc, transform in _column_lineage(inject_leakage, inject_skew)
    ]

    dataset_edges = _derive_dataset_edges(column_edges)

    # ML entities -------------------------------------------------------------------
    offline_features = ["avg_order_value_30d", "order_count_90d", "avg_resolution_hours"]
    online_features = ["avg_order_value_30d", "order_count_90d", "avg_resolution_hours"]
    if inject_compliance:
        offline_features.append("email_domain")
        online_features.append("email_domain")
    if inject_leakage:
        offline_features.append("segment_churn_rate")

    for name in offline_features:
        urn = feat(name, OFFLINE_TABLE)
        entities[urn] = Entity(
            urn=urn,
            kind=NodeKind.ML_FEATURE,
            name=name,
            description=f"Offline (training) copy of {name}",
            feature_sources=[ds(f"features.{OFFLINE_TABLE}")],
            custom_properties={"faultline.serving": "offline"},
        )

    for name in online_features:
        urn = feat(name, ONLINE_TABLE)
        entities[urn] = Entity(
            urn=urn,
            kind=NodeKind.ML_FEATURE,
            name=name,
            description=f"Online (serving) copy of {name}",
            feature_sources=[ds(f"features.{ONLINE_TABLE}")],
            custom_properties={"faultline.serving": "online"},
        )

    for table, names in ((OFFLINE_TABLE, offline_features), (ONLINE_TABLE, online_features)):
        urn = builder.make_ml_feature_table_urn(FEATURE_PLATFORM, table)
        entities[urn] = Entity(
            urn=urn,
            kind=NodeKind.ML_FEATURE_TABLE,
            name=table,
            platform=FEATURE_PLATFORM,
            description=f"Feature table `{table}`",
            ml_features=[feat(n, table) for n in names],
        )

    dep = deployment_urn()
    entities[dep] = Entity(
        urn=dep,
        kind=NodeKind.ML_MODEL_DEPLOYMENT,
        name=ENDPOINT_NAME,
        platform=DEPLOY_PLATFORM,
        description="Production inference endpoint, ~1.2M scores/day",
    )

    group = builder.make_ml_model_group_urn(MODEL_PLATFORM, "churn_models", ENV)
    entities[group] = Entity(
        urn=group, kind=NodeKind.ML_MODEL_GROUP, name="churn_models", platform=MODEL_PLATFORM
    )

    mu = model_urn()
    entities[mu] = Entity(
        urn=mu,
        kind=NodeKind.ML_MODEL,
        name=MODEL_NAME,
        platform=MODEL_PLATFORM,
        description="Predicts 90-day churn probability for the retention campaign.",
        ml_features=[feat(n, OFFLINE_TABLE) for n in offline_features],
        deployments=[dep],
        groups=[group],
        custom_properties={
            "faultline.label_column": "churned",
            "env": "PROD",
            "owner": "ml-platform",
        },
        training_metrics={"auc": "0.94", "precision_at_10": "0.71"},
        online_metrics={"auc": "0.62"},
    )

    return GraphSnapshot(
        entities=entities,
        column_edges=column_edges,
        dataset_edges=dataset_edges,
        source=source,
    )


def clean_world() -> GraphSnapshot:
    """The pipeline as its authors believed they had built it."""
    return build_world(
        inject_leakage=False,
        inject_skew=False,
        inject_compliance=False,
        currency="USD",
        source="demo-clean",
    )


def faulty_world() -> GraphSnapshot:
    """The pipeline as it actually is, after four ordinary changes."""
    return build_world(currency="EUR", source="demo")


def _derive_dataset_edges(column_edges: list[LineageEdge]) -> list[LineageEdge]:
    """Table-level lineage implied by the column-level edges."""
    from .models import short_urn  # noqa: F401  (kept for symmetry with URN helpers)

    seen: set[tuple[str, str]] = set()
    out: list[LineageEdge] = []
    for edge in column_edges:
        up = _dataset_of_field(edge.upstream)
        down = _dataset_of_field(edge.downstream)
        if up and down and up != down and (up, down) not in seen:
            seen.add((up, down))
            out.append(LineageEdge(upstream=up, downstream=down))
    return out


def _dataset_of_field(field_urn: str) -> str | None:
    marker = "urn:li:schemaField:("
    if not field_urn.startswith(marker):
        return None
    inner = field_urn[len(marker) :].rstrip(")")
    return inner.rsplit(",", 1)[0].strip()


def describe() -> dict[str, Any]:
    """Summary used by ``faultline demo --explain``."""
    world = faulty_world()
    return {
        "datasets": sum(1 for e in world.entities.values() if e.kind is NodeKind.DATASET),
        "columns": sum(len(e.fields) for e in world.entities.values()),
        "column_edges": len(world.column_edges),
        "model": MODEL_NAME,
        "endpoint": ENDPOINT_NAME,
        "faults": FAULTS,
    }
