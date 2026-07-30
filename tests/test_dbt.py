"""Tests for the dbt loader and the fixtures captured from it.

These run against the *committed* fixtures rather than requiring a dbt install, so the suite
stays fast and hermetic. The dbt-project tests are marked and skip when the warehouse has
not been built.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultline.config import FaultlineConfig
from faultline.detectors import SemanticBaseline
from faultline.engine import scan
from faultline.graph.graph import MetadataGraph
from faultline.graph.loader import load_snapshot
from faultline.models import FindingKind, Severity

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "src" / "faultline" / "data"
WAREHOUSE = REPO / "demo" / "warehouse"

pytestmark = pytest.mark.skipif(
    not (DATA / "demo-graph.json").exists(),
    reason="demo fixtures not built (run `make fixtures`)",
)


@pytest.fixture(scope="module")
def graph() -> MetadataGraph:
    return MetadataGraph(load_snapshot(DATA / "demo-graph.json"))


@pytest.fixture(scope="module")
def baseline() -> SemanticBaseline:
    return SemanticBaseline.model_validate_json(
        (DATA / "demo-baseline.json").read_text(encoding="utf-8")
    )


@pytest.fixture(scope="module")
def result(graph, baseline):
    return scan(graph, FaultlineConfig(), baseline)


# -- the captured graph -------------------------------------------------------------------


def test_lineage_came_from_dbt(graph) -> None:
    assert graph.snapshot.origin.startswith("dbt:"), "fixture must record its provenance"
    assert graph.snapshot.source.startswith("replay:")
    assert len(graph.snapshot.column_edges) > 30


def test_transforms_are_real_sql(graph) -> None:
    """Proof paths must carry the compiled expression, not a hand-written label."""
    transforms = {e.transform for e in graph.snapshot.column_edges if e.transform}
    assert any("AVG(" in t for t in transforms), "expected parsed aggregate expressions"
    assert any("CASE WHEN" in t for t in transforms)
    # Direct copies are normalised so pass-through hops do not read as divergence.
    assert "identity" in transforms


def test_table_qualifiers_are_stripped_from_transforms(graph) -> None:
    """Without this, the same operation over two CTE aliases compares unequal."""
    for edge in graph.snapshot.column_edges:
        if edge.transform and edge.transform != "identity":
            assert '"' not in edge.transform
            assert "stg_orders." not in edge.transform


def test_ml_layer_is_attached(graph) -> None:
    assert [m.name for m in graph.models()] == ["churn_propensity_v7"]
    model = graph.models()[0]
    assert model.is_deployed()
    assert model.custom_properties["faultline.label_column"] == "churned"


def test_end_to_end_chain_is_traversable(graph) -> None:
    """A raw warehouse column must reach the deployment through parsed lineage."""
    raw = next(
        f.urn
        for e in graph.datasets()
        if e.name and e.name.endswith("raw_orders")
        for f in e.fields
        if f.field_path == "amount"
    )
    deployment = next(
        e.urn for e in graph.entities.values() if e.urn.startswith("urn:li:mlModelDeployment:")
    )
    assert graph.reaches(raw, deployment)


# -- the four faults, found in real SQL ---------------------------------------------------


def test_exactly_one_finding_per_fault(result) -> None:
    assert len(result.findings) == 4
    assert {f.kind for f in result.findings} == {
        FindingKind.TARGET_LEAKAGE,
        FindingKind.TRAIN_SERVE_SKEW,
        FindingKind.SILENT_SEMANTIC_CHANGE,
        FindingKind.COMPLIANCE_PROPAGATION,
    }


def test_skew_names_the_window_difference(result) -> None:
    """The proof must point at the line of SQL to change, not just say 'they differ'."""
    skews = [f for f in result.findings if f.kind is FindingKind.TRAIN_SERVE_SKEW]
    assert len(skews) == 1, "only avg_order_value_30d diverges; the others are built correctly"

    offline = skews[0].evidence["offline_transform"]
    online = skews[0].evidence["online_transform"]
    assert "- 30" in offline and "- 7" in online
    assert skews[0].evidence["logical_feature"] == "avg_order_value_30d"


def test_leakage_traces_the_label_through_the_risk_model(result) -> None:
    leak = next(f for f in result.findings if f.kind is FindingKind.TARGET_LEAKAGE)
    assert leak.severity is Severity.CRITICAL
    assert "segment_churn_rate" in leak.feature_urn
    assert leak.column_urn.endswith(",churned)")
    assert any("customer_risk" in n for n in leak.proofs[0].nodes)


def test_currency_change_is_detected_against_the_clean_build(result) -> None:
    change = next(f for f in result.findings if f.kind is FindingKind.SILENT_SEMANTIC_CHANGE)
    assert change.evidence["change_kind"] == "currency change"
    assert change.column_urn.endswith(",amount)")


def test_pii_reaches_the_deployed_model(result) -> None:
    pii = next(f for f in result.findings if f.kind is FindingKind.COMPLIANCE_PROPAGATION)
    assert pii.column_urn.endswith(",email)")
    assert pii.evidence["model_deployed"] is True


def test_clean_world_has_no_findings() -> None:
    """The pipeline as intended, judged against itself, must be silent."""
    clean = MetadataGraph(load_snapshot(DATA / "demo-graph-clean.json"))
    assert scan(clean, FaultlineConfig(), SemanticBaseline.capture(clean)).findings == []


def test_clean_build_omits_the_fault_columns() -> None:
    """The two worlds differ in the warehouse itself, not just in Faultline's reading."""
    clean = MetadataGraph(load_snapshot(DATA / "demo-graph-clean.json"))
    offline = next(
        e for e in clean.datasets() if (e.name or "").endswith("customer_features_offline")
    )
    names = {f.field_path for f in offline.fields}
    assert "segment_churn_rate" not in names
    assert "email_domain" not in names


# -- against a live dbt project ------------------------------------------------------------


@pytest.mark.skipif(
    not (WAREHOUSE / "target" / "manifest.json").exists(),
    reason="dbt project not built (run `make warehouse`)",
)
def test_loading_the_project_matches_the_committed_fixture() -> None:
    """The fixture must be a faithful recording, not a stale artefact."""
    from faultline.dbt import DbtProject

    fresh = DbtProject(WAREHOUSE).load(ml_spec=REPO / "demo" / "ml.yml")
    committed = load_snapshot(DATA / "demo-graph.json")

    assert set(fresh.entities) == set(committed.entities)
    assert len(fresh.column_edges) == len(committed.column_edges)


@pytest.mark.skipif(
    not (WAREHOUSE / "target" / "manifest.json").exists(),
    reason="dbt project not built",
)
def test_every_model_parsed_without_error() -> None:
    """A model whose SQL fails to parse silently loses all of its lineage."""
    manifest = json.loads((WAREHOUSE / "target" / "manifest.json").read_text(encoding="utf-8"))
    models = [
        n for n in manifest["nodes"].values() if n.get("resource_type") == "model"
    ]
    assert models

    from datahub.sql_parsing.schema_resolver import SchemaResolver
    from datahub.sql_parsing.sqlglot_lineage import sqlglot_lineage

    from faultline.dbt import DbtProject

    project = DbtProject(WAREHOUSE)
    snapshot = project.load()
    resolver = SchemaResolver(platform="duckdb", env="PROD")
    for urn, entity in snapshot.entities.items():
        resolver.add_raw_schema_info(
            urn, {f.field_path: (f.native_type or "string") for f in entity.fields}
        )

    for node in models:
        parsed = sqlglot_lineage(
            node["compiled_code"],
            schema_resolver=resolver,
            default_db=node["database"],
            default_schema=node["schema"],
        )
        assert not (parsed.debug_info and parsed.debug_info.error), node["name"]
        assert parsed.column_lineage, f"{node['name']} produced no column lineage"
