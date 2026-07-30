"""Tests for the metadata graph: the ML column bridge and path traversal."""

from __future__ import annotations

import pytest

from faultline.graph.graph import (
    BRIDGE_DATASET_WIDE,
    BRIDGE_EXPLICIT,
    BRIDGE_NAME_MATCH,
    MetadataGraph,
)
from faultline.models import EdgeKind, NodeKind, short_urn

from .factory import (
    GraphBuilder,
    clean_world,
    column_urn,
    deployment_urn,
    feature_urn,
    leaky_world,
    model_urn,
)


@pytest.fixture
def clean() -> MetadataGraph:
    return MetadataGraph(clean_world())


@pytest.fixture
def leaky() -> MetadataGraph:
    return MetadataGraph(leaky_world())


# -- the ML bridge ----------------------------------------------------------------------


def test_bridge_resolves_by_name_match(clean: MetadataGraph) -> None:
    bridges = clean.feature_source_columns(feature_urn("avg_order_value_30d"))
    assert len(bridges) == 1
    assert bridges[0].method == BRIDGE_NAME_MATCH
    assert bridges[0].column_urn == column_urn(
        "analytics.customer_features", "avg_order_value_30d"
    )


def test_bridge_prefers_explicit_declaration() -> None:
    b = GraphBuilder()
    b.dataset("analytics.t", {"a": "INT", "b": "INT"})
    b.feature("some_feature", ["analytics.t"], explicit_columns=[column_urn("analytics.t", "b")])
    graph = MetadataGraph(b.build())

    bridges = graph.feature_source_columns(feature_urn("some_feature"))
    assert [x.method for x in bridges] == [BRIDGE_EXPLICIT]
    assert bridges[0].column_urn == column_urn("analytics.t", "b")
    assert bridges[0].confidence == 1.0


def test_bridge_accepts_bare_column_names() -> None:
    """The ergonomic form a human actually writes in a custom property."""
    b = GraphBuilder()
    b.dataset("analytics.t", {"a": "INT", "b": "INT"})
    b.feature("some_feature", ["analytics.t"], explicit_columns=["b"])
    graph = MetadataGraph(b.build())

    bridges = graph.feature_source_columns(feature_urn("some_feature"))
    assert [x.method for x in bridges] == [BRIDGE_EXPLICIT]
    assert bridges[0].column_urn == column_urn("analytics.t", "b")


def test_bridge_ignores_unknown_explicit_tokens() -> None:
    """An unresolvable declaration must not fabricate a column."""
    b = GraphBuilder()
    b.dataset("analytics.t", {"a": "INT"})
    b.feature("some_feature", ["analytics.t"], explicit_columns=["does_not_exist"])
    graph = MetadataGraph(b.build())

    bridges = graph.feature_source_columns(feature_urn("some_feature"))
    assert {x.method for x in bridges} == {BRIDGE_DATASET_WIDE}


def test_bridge_falls_back_to_dataset_wide_at_low_confidence() -> None:
    b = GraphBuilder()
    b.dataset("analytics.t", {"a": "INT", "b": "INT"})
    b.feature("no_matching_column", ["analytics.t"])
    graph = MetadataGraph(b.build())

    bridges = graph.feature_source_columns(feature_urn("no_matching_column"))
    assert {x.method for x in bridges} == {BRIDGE_DATASET_WIDE}
    assert len(bridges) == 2
    assert all(x.confidence < 0.5 for x in bridges)


def test_bridge_is_cached(clean: MetadataGraph) -> None:
    urn = feature_urn("avg_order_value_30d")
    assert clean.feature_source_columns(urn) is clean.feature_source_columns(urn)


# -- traversal --------------------------------------------------------------------------


def test_end_to_end_ml_chain_is_traversable(clean: MetadataGraph) -> None:
    """raw column -> staging -> feature table column -> MLFeature -> MLModel -> deployment."""
    paths = clean.paths_between(
        column_urn("raw.orders", "amount"), deployment_urn(), max_depth=12
    )
    assert paths, "expected the raw column to reach the deployment"

    path = paths[0]
    assert path.is_contiguous()
    kinds = [EdgeKind(h.edge) for h in path.hops]
    assert EdgeKind.COLUMN_LINEAGE in kinds
    assert EdgeKind.FEATURE_SOURCE in kinds
    assert EdgeKind.MODEL_FEATURE in kinds
    assert EdgeKind.MODEL_DEPLOYMENT in kinds
    assert path.terminus == deployment_urn()


def test_ancestors_are_data_flow_oriented(clean: MetadataGraph) -> None:
    target = column_urn("analytics.customer_features", "avg_order_value_30d")
    ancestors = clean.ancestors(target)
    raw = column_urn("raw.orders", "amount")
    assert raw in ancestors

    path = ancestors[raw]
    assert path.is_contiguous()
    assert path.origin == raw
    assert path.terminus == target


def test_model_source_columns_returns_only_columns(clean: MetadataGraph) -> None:
    columns = clean.model_source_columns(model_urn())
    assert columns
    assert all(u.startswith("urn:li:schemaField:") for u in columns)
    assert column_urn("raw.orders", "amount") in columns


def test_models_depending_on_column_is_the_blast_radius(clean: MetadataGraph) -> None:
    affected = clean.models_depending_on(column_urn("raw.orders", "amount"))
    assert model_urn() in affected
    assert affected[model_urn()].is_contiguous()


def test_unrelated_column_does_not_reach_the_model(clean: MetadataGraph) -> None:
    assert not clean.reaches(column_urn("raw.customers", "email"), model_urn())


def test_paths_between_is_bounded(leaky: MetadataGraph) -> None:
    paths = leaky.paths_between(
        column_urn("raw.orders", "amount"), model_urn(), max_paths=2
    )
    assert len(paths) <= 2


def test_removed_entities_are_excluded() -> None:
    snapshot = clean_world()
    ds = snapshot.entities[list(snapshot.entities)[0]]
    for urn, entity in snapshot.entities.items():
        if entity.kind is NodeKind.ML_MODEL:
            snapshot.entities[urn] = entity.model_copy(update={"removed": True})
    graph = MetadataGraph(snapshot)
    assert not graph.models()
    assert ds is not None


# -- leakage-shaped structure -----------------------------------------------------------


def test_label_column_reaches_a_feature_in_the_leaky_world(leaky: MetadataGraph) -> None:
    label = column_urn("analytics.customer_features", "churned")
    leaked_feature = feature_urn("segment_churn_rate")
    assert leaky.reaches(label, leaked_feature)

    paths = leaky.paths_between(label, leaked_feature)
    assert paths and paths[0].is_contiguous()


def test_clean_world_has_no_label_to_feature_path(clean: MetadataGraph) -> None:
    label = column_urn("analytics.customer_features", "churned")
    for feature in clean.features():
        assert not clean.reaches(label, feature.urn)


def test_columns_with_term_finds_the_label(clean: MetadataGraph) -> None:
    from .factory import LABEL_TERM

    labels = clean.columns_with_term(LABEL_TERM)
    assert labels == [column_urn("analytics.customer_features", "churned")]


# -- rendering --------------------------------------------------------------------------


def test_short_urn_compacts_column_urns() -> None:
    assert (
        short_urn(column_urn("analytics.customer_features", "churned"))
        == "analytics.customer_features.churned"
    )


def test_short_urn_compacts_ml_urns() -> None:
    assert short_urn(model_urn()) == "churn_propensity"
    assert short_urn(feature_urn("avg_order_value_30d")) == "customer_features.avg_order_value_30d"


def test_proof_path_renders_transforms(clean: MetadataGraph) -> None:
    target = column_urn("analytics.customer_features", "avg_order_value_30d")
    path = clean.ancestors(target)[column_urn("raw.orders", "amount")]
    rendered = path.render()
    assert "raw.orders.amount" in rendered
    assert "cast to usd" in rendered
    assert "avg over 30d window" in rendered


def test_signature_distinguishes_different_transforms() -> None:
    b1 = GraphBuilder().dataset("a.x", {"c": "INT"}).dataset("a.y", {"c": "INT"})
    b1.col_edge(("a.x", "c"), ("a.y", "c"), "sum")
    g1 = MetadataGraph(b1.build())

    b2 = GraphBuilder().dataset("a.x", {"c": "INT"}).dataset("a.y", {"c": "INT"})
    b2.col_edge(("a.x", "c"), ("a.y", "c"), "avg")
    g2 = MetadataGraph(b2.build())

    p1 = g1.ancestors(column_urn("a.y", "c"))[column_urn("a.x", "c")]
    p2 = g2.ancestors(column_urn("a.y", "c"))[column_urn("a.x", "c")]
    assert p1.signature() != p2.signature()
