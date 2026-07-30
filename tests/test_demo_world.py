"""The demo world is the submission's main exhibit. It must map 1:1 to its injected faults.

These tests exist because a demo that reports six findings for four faults, or misses one,
undermines the whole claim that Faultline reports only what it can prove.
"""

from __future__ import annotations

import pytest

from faultline import demo_world
from faultline.config import FaultlineConfig
from faultline.detectors import SemanticBaseline
from faultline.engine import scan
from faultline.graph.graph import MetadataGraph
from faultline.models import FindingKind, Severity


@pytest.fixture
def result():
    baseline = SemanticBaseline.capture(MetadataGraph(demo_world.clean_world()))
    return scan(MetadataGraph(demo_world.faulty_world()), FaultlineConfig(), baseline)


def test_exactly_one_finding_per_injected_fault(result) -> None:
    assert len(result.findings) == len(demo_world.FAULTS) == 4
    assert {f.kind for f in result.findings} == {
        FindingKind.TARGET_LEAKAGE,
        FindingKind.TRAIN_SERVE_SKEW,
        FindingKind.SILENT_SEMANTIC_CHANGE,
        FindingKind.COMPLIANCE_PROPAGATION,
    }


def test_every_finding_is_substantiated(result) -> None:
    for finding in result.findings:
        assert not finding.validate_proofs()
        assert finding.proofs
        assert finding.proofs[0].is_contiguous()
        assert finding.remediation


def test_leakage_names_the_right_feature_and_label(result) -> None:
    leak = next(f for f in result.findings if f.kind is FindingKind.TARGET_LEAKAGE)
    assert leak.severity is Severity.CRITICAL
    assert "segment_churn_rate" in leak.feature_urn
    assert leak.column_urn.endswith("churned)")


def test_skew_is_reported_only_for_the_diverged_feature(result) -> None:
    """The other three feature pairs are built correctly and must stay silent."""
    skews = [f for f in result.findings if f.kind is FindingKind.TRAIN_SERVE_SKEW]
    assert len(skews) == 1
    assert skews[0].evidence["logical_feature"] == "avg_order_value_30d"
    assert skews[0].evidence["offline_transform"] == "avg over 30d window"
    assert skews[0].evidence["online_transform"] == "avg over 7d window"


def test_semantic_change_is_reported_at_its_root(result) -> None:
    """Only raw.orders.amount -- not every column downstream that inherited the change."""
    changes = [f for f in result.findings if f.kind is FindingKind.SILENT_SEMANTIC_CHANGE]
    assert len(changes) == 1
    assert changes[0].column_urn.endswith(",amount)")
    assert changes[0].evidence["change_kind"] == "currency change"


def test_compliance_is_reported_at_the_source_column(result) -> None:
    pii = [f for f in result.findings if f.kind is FindingKind.COMPLIANCE_PROPAGATION]
    assert len(pii) == 1
    assert pii[0].column_urn.endswith(",email)")


def test_clean_world_is_clean() -> None:
    """The pipeline as intended must produce nothing at all -- no baseline noise."""
    clean = MetadataGraph(demo_world.clean_world())
    assert scan(clean, FaultlineConfig(), SemanticBaseline.capture(clean)).findings == []


def test_collapsing_keeps_distinct_defects_on_separate_chains() -> None:
    """Cascade collapsing must not merge two genuinely independent findings."""
    from tests.factory import PII_TERM, GraphBuilder

    b = GraphBuilder()
    # Two unrelated PII columns, each reaching the model by its own path.
    b.dataset("raw.a", {"email": "VARCHAR"}, terms={"email": [PII_TERM]})
    b.dataset("raw.b", {"phone": "VARCHAR"}, terms={"phone": [PII_TERM]})
    b.dataset("analytics.f", {"fa": "VARCHAR", "fb": "VARCHAR"})
    b.col_edge(("raw.a", "email"), ("analytics.f", "fa"), "hash")
    b.col_edge(("raw.b", "phone"), ("analytics.f", "fb"), "hash")
    b.feature("fa", ["analytics.f"])
    b.feature("fb", ["analytics.f"])
    b.model(features=["fa", "fb"], deployed=True)

    findings = scan(MetadataGraph(b.build())).findings
    pii = [f for f in findings if f.kind is FindingKind.COMPLIANCE_PROPAGATION]
    assert len(pii) == 2, "independent chains must not collapse into one"
    assert {f.column_urn.rsplit(",", 1)[-1] for f in pii} == {"email)", "phone)"}
