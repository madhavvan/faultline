"""Tests for the DataHub emitter, against a recording fake client.

These assert the *shape* of what would be sent — that column-level lineage is expressed as
FineGrainedLineage, that glossary terms ride along on schema fields, that ML entities carry
their links. The live round-trip is verified separately by demo/verify_live.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from faultline.emit import SnapshotEmitter
from faultline.graph.loader import load_snapshot
from tests.factory import LABEL_TERM, PII_TERM, clean_world, leaky_world


class FakeClient:
    """Records every MCP instead of sending it."""

    def __init__(self, fail_on: type | None = None) -> None:
        self.emitted: list[tuple[str, Any]] = []
        self.fail_on = fail_on

    def emit_mcp(self, mcp: Any) -> None:
        if self.fail_on and isinstance(mcp.aspect, self.fail_on):
            raise RuntimeError("simulated transport failure")
        self.emitted.append((mcp.entityUrn, mcp.aspect))

    def aspects(self, name: str) -> list[Any]:
        return [a for _, a in self.emitted if type(a).__name__ == name]

    def for_urn(self, urn: str) -> list[Any]:
        return [a for u, a in self.emitted if u == urn]


@pytest.fixture
def emitted() -> FakeClient:
    client = FakeClient()
    SnapshotEmitter(client=client).emit(leaky_world())
    return client


def test_emits_something_for_every_entity(emitted: FakeClient) -> None:
    assert len(emitted.emitted) > 10
    urns = {u for u, _ in emitted.emitted}
    assert any(u.startswith("urn:li:dataset:") for u in urns)
    assert any(u.startswith("urn:li:mlModel:") for u in urns)
    assert any(u.startswith("urn:li:mlFeature:") for u in urns)


def test_glossary_terms_are_created_before_use(emitted: FakeClient) -> None:
    """A term association pointing at a non-existent term renders as a dangling URN."""
    infos = emitted.aspects("GlossaryTermInfoClass")
    created = {u for u, a in emitted.emitted if type(a).__name__ == "GlossaryTermInfoClass"}
    assert LABEL_TERM in created
    assert PII_TERM in created
    assert all(i.definition for i in infos), "every term needs a definition"

    first_term_index = min(
        i for i, (_, a) in enumerate(emitted.emitted)
        if type(a).__name__ == "GlossaryTermInfoClass"
    )
    first_schema_index = min(
        i for i, (_, a) in enumerate(emitted.emitted)
        if type(a).__name__ == "SchemaMetadataClass"
    )
    assert first_term_index < first_schema_index


def test_column_terms_ride_on_schema_fields(emitted: FakeClient) -> None:
    schemas = emitted.aspects("SchemaMetadataClass")
    assert schemas

    labelled = [
        f
        for s in schemas
        for f in s.fields
        if f.glossaryTerms and any(t.urn == LABEL_TERM for t in f.glossaryTerms.terms)
    ]
    assert len(labelled) == 1
    assert labelled[0].fieldPath == "churned"


def test_column_lineage_is_fine_grained(emitted: FakeClient) -> None:
    lineages = emitted.aspects("UpstreamLineageClass")
    assert lineages

    fine = [f for lin in lineages for f in (lin.fineGrainedLineages or [])]
    assert fine, "column edges must be emitted as FineGrainedLineage"
    for entry in fine:
        assert entry.downstreams and len(entry.downstreams) == 1
        assert entry.upstreams
        assert entry.downstreams[0].startswith("urn:li:schemaField:")


def test_fine_grained_lineage_always_has_table_upstreams(emitted: FakeClient) -> None:
    """DataHub renders column edges under a table edge; without one they hang unattached."""
    for lineage in emitted.aspects("UpstreamLineageClass"):
        if lineage.fineGrainedLineages:
            assert lineage.upstreams, "fine-grained lineage needs its table upstreams"


def test_model_carries_features_and_deployment(emitted: FakeClient) -> None:
    props = emitted.aspects("MLModelPropertiesClass")
    assert len(props) == 1
    assert props[0].mlFeatures
    assert props[0].deployments
    assert props[0].customProperties.get("faultline.label_column") == "churned"


def test_features_carry_their_sources(emitted: FakeClient) -> None:
    props = emitted.aspects("MLFeaturePropertiesClass")
    assert props
    assert all(p.sources for p in props)


def test_failures_are_collected_not_raised() -> None:
    """A transport error on one aspect must not abort the whole emission."""
    from datahub.metadata import schema_classes as S

    client = FakeClient(fail_on=S.SchemaMetadataClass)
    report = SnapshotEmitter(client=client).emit(clean_world())

    assert report.failures, "failures should be reported"
    assert report.aspects > 0, "other aspects should still have been emitted"
    assert "failed" in report.summary()


def test_dbt_derived_graph_is_emittable() -> None:
    """The real fixture, not just the synthetic one."""
    from pathlib import Path

    data = Path(__file__).resolve().parent.parent / "src" / "faultline" / "data"
    if not (data / "demo-graph.json").exists():
        pytest.skip("demo fixtures not built")

    client = FakeClient()
    report = SnapshotEmitter(client=client).emit(load_snapshot(data / "demo-graph.json"))

    assert not report.failures
    assert report.entities >= 20
    fine = [
        f
        for lin in client.aspects("UpstreamLineageClass")
        for f in (lin.fineGrainedLineages or [])
    ]
    assert len(fine) >= 30, "parsed column lineage must survive into the emission"
    assert any(
        f.transformOperation and "AVG(" in f.transformOperation for f in fine
    ), "the compiled SQL should ride along as the transform"
