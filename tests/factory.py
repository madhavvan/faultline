"""Builders for synthetic metadata graphs used across the test-suite.

These construct :class:`GraphSnapshot` objects directly, so graph and detector logic can be
tested exhaustively without a DataHub instance. The shapes mirror what the real DuckDB/dbt
demo world emits, which keeps unit tests honest about the structures they assert on.
"""

from __future__ import annotations

import datahub.emitter.mce_builder as builder

from faultline.graph.entities import Entity, GraphSnapshot, LineageEdge, SchemaField
from faultline.models import NodeKind

PLATFORM = "duckdb"
ENV = "PROD"
FEATURE_TABLE = "customer_features"
ONLINE_FEATURE_TABLE = "customer_features_online"

LABEL_TERM = builder.make_term_urn("Label")
PII_TERM = builder.make_term_urn("PII")


def dataset_urn(name: str) -> str:
    return builder.make_dataset_urn(PLATFORM, name, ENV)


def column_urn(dataset: str, column: str) -> str:
    return builder.make_schema_field_urn(dataset_urn(dataset), column)


def feature_urn(name: str, table: str = FEATURE_TABLE) -> str:
    return builder.make_ml_feature_urn(table, name)


def model_urn(name: str = "churn_propensity") -> str:
    return builder.make_ml_model_urn("mlflow", name, ENV)


def deployment_urn(name: str = "churn_endpoint") -> str:
    return builder.make_ml_model_deployment_urn("sagemaker", name, ENV)


class GraphBuilder:
    """Fluent builder for a synthetic snapshot."""

    def __init__(self) -> None:
        self.entities: dict[str, Entity] = {}
        self.column_edges: list[LineageEdge] = []
        self.dataset_edges: list[LineageEdge] = []

    # -- entities ----------------------------------------------------------------------

    def dataset(
        self,
        name: str,
        columns: dict[str, str],
        *,
        terms: dict[str, list[str]] | None = None,
        descriptions: dict[str, str] | None = None,
    ) -> GraphBuilder:
        urn = dataset_urn(name)
        terms = terms or {}
        descriptions = descriptions or {}
        fields = [
            SchemaField(
                field_path=col,
                urn=column_urn(name, col),
                native_type=native,
                type_class=native,
                description=descriptions.get(col),
                terms=terms.get(col, []),
            )
            for col, native in columns.items()
        ]
        self.entities[urn] = Entity(
            urn=urn, kind=NodeKind.DATASET, name=name, platform=PLATFORM, fields=fields
        )
        return self

    def feature(
        self,
        name: str,
        sources: list[str],
        *,
        table: str = FEATURE_TABLE,
        explicit_columns: list[str] | None = None,
        serving: str | None = None,
        props: dict[str, str] | None = None,
    ) -> GraphBuilder:
        urn = feature_urn(name, table)
        props = dict(props or {})
        if explicit_columns:
            props["faultline.source_columns"] = ",".join(explicit_columns)
        if serving:
            props["faultline.serving"] = serving
        self.entities[urn] = Entity(
            urn=urn,
            kind=NodeKind.ML_FEATURE,
            name=name,
            feature_sources=[dataset_urn(s) for s in sources],
            custom_properties=props,
        )
        return self

    def feature_table(self, name: str, features: list[str]) -> GraphBuilder:
        urn = builder.make_ml_feature_table_urn("feast", name)
        self.entities[urn] = Entity(
            urn=urn,
            kind=NodeKind.ML_FEATURE_TABLE,
            name=name,
            ml_features=[feature_urn(f, name) for f in features],
        )
        return self

    def model(
        self,
        name: str = "churn_propensity",
        *,
        features: list[str] | None = None,
        feature_table: str = FEATURE_TABLE,
        deployed: bool = True,
        label_column: str | None = None,
        custom: dict[str, str] | None = None,
    ) -> GraphBuilder:
        urn = model_urn(name)
        props = dict(custom or {})
        if label_column:
            props["faultline.label_column"] = label_column
        deployments = [deployment_urn()] if deployed else []
        if deployed:
            dep = deployment_urn()
            self.entities[dep] = Entity(
                urn=dep, kind=NodeKind.ML_MODEL_DEPLOYMENT, name="churn_endpoint"
            )
        self.entities[urn] = Entity(
            urn=urn,
            kind=NodeKind.ML_MODEL,
            name=name,
            platform="mlflow",
            ml_features=[feature_urn(f, feature_table) for f in (features or [])],
            deployments=deployments,
            custom_properties=props,
        )
        return self

    # -- edges -------------------------------------------------------------------------

    def col_edge(
        self,
        src: tuple[str, str],
        dst: tuple[str, str],
        transform: str = "identity",
        confidence: float = 1.0,
    ) -> GraphBuilder:
        self.column_edges.append(
            LineageEdge(
                upstream=column_urn(*src),
                downstream=column_urn(*dst),
                transform=transform,
                confidence=confidence,
            )
        )
        return self

    def ds_edge(self, src: str, dst: str) -> GraphBuilder:
        self.dataset_edges.append(
            LineageEdge(upstream=dataset_urn(src), downstream=dataset_urn(dst))
        )
        return self

    def build(self) -> GraphSnapshot:
        return GraphSnapshot(
            entities=self.entities,
            column_edges=self.column_edges,
            dataset_edges=self.dataset_edges,
            source="synthetic",
        )


def clean_world() -> GraphSnapshot:
    """A small, structurally healthy churn pipeline."""
    b = GraphBuilder()
    b.dataset(
        "raw.orders",
        {"order_id": "BIGINT", "customer_id": "BIGINT", "amount": "DECIMAL", "created_at": "TIMESTAMP"},
    )
    b.dataset(
        "raw.customers",
        {"customer_id": "BIGINT", "email": "VARCHAR", "signup_at": "TIMESTAMP"},
        terms={"email": [PII_TERM]},
    )
    b.dataset(
        "analytics.stg_orders",
        {"order_id": "BIGINT", "customer_id": "BIGINT", "amount_usd": "DECIMAL"},
    )
    b.dataset(
        "analytics.customer_features",
        {
            "customer_id": "BIGINT",
            "avg_order_value_30d": "DECIMAL",
            "order_count_30d": "BIGINT",
            "churned": "BOOLEAN",
        },
        terms={"churned": [LABEL_TERM]},
    )

    b.col_edge(("raw.orders", "order_id"), ("analytics.stg_orders", "order_id"))
    b.col_edge(("raw.orders", "customer_id"), ("analytics.stg_orders", "customer_id"))
    b.col_edge(("raw.orders", "amount"), ("analytics.stg_orders", "amount_usd"), "cast to usd")
    b.col_edge(
        ("analytics.stg_orders", "amount_usd"),
        ("analytics.customer_features", "avg_order_value_30d"),
        "avg over 30d window",
    )
    b.col_edge(
        ("analytics.stg_orders", "order_id"),
        ("analytics.customer_features", "order_count_30d"),
        "count over 30d window",
    )
    b.ds_edge("raw.orders", "analytics.stg_orders")
    b.ds_edge("analytics.stg_orders", "analytics.customer_features")

    b.feature("avg_order_value_30d", ["analytics.customer_features"])
    b.feature("order_count_30d", ["analytics.customer_features"])
    b.feature_table(FEATURE_TABLE, ["avg_order_value_30d", "order_count_30d"])
    b.model(
        features=["avg_order_value_30d", "order_count_30d"],
        deployed=True,
        label_column="churned",
    )
    return b.build()


def leaky_world() -> GraphSnapshot:
    """Clean world plus a feature computed *from the label* four hops up."""
    b = GraphBuilder()
    snapshot = clean_world()
    b.entities = dict(snapshot.entities)
    b.column_edges = list(snapshot.column_edges)
    b.dataset_edges = list(snapshot.dataset_edges)

    # A "helpful" analyst adds a churn-rate rollup derived from the label column ...
    b.dataset(
        "analytics.customer_risk",
        {"customer_id": "BIGINT", "segment_churn_rate": "DECIMAL"},
    )
    b.col_edge(
        ("analytics.customer_features", "churned"),
        ("analytics.customer_risk", "segment_churn_rate"),
        "avg(churned) over segment",
    )
    # ... and it becomes a model feature.
    b.feature("segment_churn_rate", ["analytics.customer_risk"])

    model = b.entities[model_urn()]
    b.entities[model_urn()] = model.model_copy(
        update={"ml_features": [*model.ml_features, feature_urn("segment_churn_rate")]}
    )
    ft_urn = builder.make_ml_feature_table_urn("feast", FEATURE_TABLE)
    ft = b.entities[ft_urn]
    b.entities[ft_urn] = ft.model_copy(
        update={"ml_features": [*ft.ml_features, feature_urn("segment_churn_rate")]}
    )
    return b.build()
