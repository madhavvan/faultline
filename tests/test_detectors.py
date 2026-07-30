"""Tests for the four structural detectors and the scan engine."""

from __future__ import annotations

import pytest

from faultline.config import FaultlineConfig
from faultline.detectors import (
    CompliancePropagationDetector,
    SemanticBaseline,
    SilentSemanticChangeDetector,
    TargetLeakageDetector,
    TrainServeSkewDetector,
)
from faultline.engine import scan
from faultline.graph.graph import MetadataGraph
from faultline.models import FindingKind, Severity

from .factory import (
    LABEL_TERM,
    PII_TERM,
    GraphBuilder,
    clean_world,
    column_urn,
    feature_urn,
    leaky_world,
    model_urn,
)


@pytest.fixture
def settings():
    return FaultlineConfig().detectors


# -- target leakage ----------------------------------------------------------------------


def test_leakage_is_found_in_the_leaky_world(settings) -> None:
    graph = MetadataGraph(leaky_world())
    findings = TargetLeakageDetector().run(graph, settings)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind is FindingKind.TARGET_LEAKAGE
    assert finding.feature_urn == feature_urn("segment_churn_rate")
    assert finding.column_urn == column_urn("analytics.customer_features", "churned")
    assert finding.proofs and finding.proofs[0].is_contiguous()
    assert not finding.validate_proofs()


def test_leakage_on_a_deployed_model_is_critical(settings) -> None:
    graph = MetadataGraph(leaky_world())
    finding = TargetLeakageDetector().run(graph, settings)[0]
    assert finding.severity is Severity.CRITICAL
    assert finding.evidence["model_deployed"] is True


def test_leakage_severity_steps_down_when_not_deployed(settings) -> None:
    snapshot = leaky_world()
    model = snapshot.entities[model_urn()]
    snapshot.entities[model_urn()] = model.model_copy(update={"deployments": []})

    finding = TargetLeakageDetector().run(MetadataGraph(snapshot), settings)[0]
    assert finding.severity is Severity.HIGH


def test_clean_world_produces_no_leakage(settings) -> None:
    graph = MetadataGraph(clean_world())
    assert TargetLeakageDetector().run(graph, settings) == []


def test_label_used_directly_as_a_feature_is_reported(settings) -> None:
    """The blunt case: the label column is wired straight in as a model input."""
    b = GraphBuilder()
    b.dataset(
        "analytics.training",
        {"customer_id": "BIGINT", "churned": "BOOLEAN"},
        terms={"churned": [LABEL_TERM]},
    )
    b.feature("churned", ["analytics.training"])
    b.model(features=["churned"], deployed=True)
    graph = MetadataGraph(b.build())

    findings = TargetLeakageDetector().run(graph, settings)
    assert len(findings) == 1
    assert findings[0].evidence["relationship"] == "feature_is_label"
    assert findings[0].severity is Severity.CRITICAL


def test_no_label_means_no_leakage_claim(settings) -> None:
    """Without a known target, the detector must stay silent rather than guess."""
    b = GraphBuilder()
    b.dataset("analytics.t", {"a": "INT", "b": "INT"})
    b.col_edge(("analytics.t", "a"), ("analytics.t", "b"), "derive")
    b.feature("b", ["analytics.t"])
    b.model(features=["b"])
    graph = MetadataGraph(b.build())

    assert TargetLeakageDetector().run(graph, settings) == []


def test_label_can_be_declared_by_custom_property(settings) -> None:
    b = GraphBuilder()
    b.dataset("analytics.t", {"target": "BOOLEAN", "leaky": "DECIMAL"})
    b.col_edge(("analytics.t", "target"), ("analytics.t", "leaky"), "avg(target)")
    b.feature("leaky", ["analytics.t"])
    b.model(features=["leaky"], deployed=True, label_column="target")
    graph = MetadataGraph(b.build())

    findings = TargetLeakageDetector().run(graph, settings)
    assert len(findings) == 1
    assert findings[0].evidence["label_identified_by"] == "declared"


# -- train/serve skew --------------------------------------------------------------------


def _skew_world(offline_transform: str, online_transform: str):
    """One raw column, materialised into an offline and an online feature."""
    b = GraphBuilder()
    b.dataset("raw.orders", {"amount": "DECIMAL"})
    b.dataset("analytics.offline_features", {"avg_order_value": "DECIMAL"})
    b.dataset("analytics.online_features", {"avg_order_value": "DECIMAL"})

    b.col_edge(("raw.orders", "amount"), ("analytics.offline_features", "avg_order_value"),
               offline_transform)
    b.col_edge(("raw.orders", "amount"), ("analytics.online_features", "avg_order_value"),
               online_transform)

    b.feature("avg_order_value", ["analytics.offline_features"],
              table="fs_offline", serving="offline")
    b.feature("avg_order_value", ["analytics.online_features"],
              table="fs_online", serving="online")
    b.model(features=["avg_order_value"], feature_table="fs_offline", deployed=True)
    return b.build()


def test_skew_detected_when_transforms_diverge(settings) -> None:
    graph = MetadataGraph(_skew_world("avg over 30d window", "avg over 7d window"))
    findings = TrainServeSkewDetector().run(graph, settings)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind is FindingKind.TRAIN_SERVE_SKEW
    assert finding.evidence["offline_transform"] == "avg over 30d window"
    assert finding.evidence["online_transform"] == "avg over 7d window"
    assert finding.evidence["fork_after_hops"] == 0
    assert finding.severity is Severity.HIGH


def test_no_skew_when_transforms_match(settings) -> None:
    graph = MetadataGraph(_skew_world("avg over 30d window", "avg over 30d window"))
    assert TrainServeSkewDetector().run(graph, settings) == []


def test_disjoint_sources_are_critical(settings) -> None:
    """Two features with the same name and no shared ancestry at all."""
    b = GraphBuilder()
    b.dataset("raw.a", {"x": "DECIMAL"})
    b.dataset("raw.b", {"y": "DECIMAL"})
    b.dataset("analytics.off", {"score": "DECIMAL"})
    b.dataset("analytics.on", {"score": "DECIMAL"})
    b.col_edge(("raw.a", "x"), ("analytics.off", "score"), "sum")
    b.col_edge(("raw.b", "y"), ("analytics.on", "score"), "sum")
    b.feature("score", ["analytics.off"], table="fs_offline", serving="offline")
    b.feature("score", ["analytics.on"], table="fs_online", serving="online")
    b.model(features=["score"], feature_table="fs_offline", deployed=True)

    findings = TrainServeSkewDetector().run(MetadataGraph(b.build()), settings)
    assert len(findings) == 1
    assert findings[0].severity is Severity.CRITICAL
    assert "share no common source" in findings[0].title


def test_extra_upstream_join_on_one_path_is_still_caught(settings) -> None:
    """Root-level asymmetry must still fire -- the narrowing must not disable the check."""
    b = GraphBuilder()
    b.dataset("raw.orders", {"amount": "DECIMAL"})
    b.dataset("raw.promos", {"discount": "DECIMAL"})
    b.dataset("analytics.offline_features", {"avg_order_value": "DECIMAL"})
    b.dataset("analytics.online_features", {"avg_order_value": "DECIMAL"})

    # The offline path joins in a promo table the online path never reads.
    b.col_edge(("raw.orders", "amount"),
               ("analytics.offline_features", "avg_order_value"), "avg")
    b.col_edge(("raw.promos", "discount"),
               ("analytics.offline_features", "avg_order_value"), "avg")
    b.col_edge(("raw.orders", "amount"),
               ("analytics.online_features", "avg_order_value"), "avg")

    b.feature("avg_order_value", ["analytics.offline_features"],
              table="fs_offline", serving="offline")
    b.feature("avg_order_value", ["analytics.online_features"],
              table="fs_online", serving="online")
    b.model(features=["avg_order_value"], feature_table="fs_offline", deployed=True)

    findings = TrainServeSkewDetector().run(MetadataGraph(b.build()), settings)
    assert len(findings) == 1
    assert "extra columns" in findings[0].title
    assert findings[0].evidence["offline_only_columns"] == ["raw.promos.discount"]


def test_unpaired_features_are_ignored(settings) -> None:
    """A feature with no online twin is not skew -- there is nothing to diverge from."""
    b = GraphBuilder()
    b.dataset("analytics.off", {"score": "DECIMAL"})
    b.feature("score", ["analytics.off"], table="fs_offline", serving="offline")
    b.model(features=["score"], feature_table="fs_offline")

    assert TrainServeSkewDetector().run(MetadataGraph(b.build()), settings) == []


# -- silent semantic change ---------------------------------------------------------------


def test_semantic_detector_is_silent_without_a_baseline(settings) -> None:
    graph = MetadataGraph(clean_world())
    assert SilentSemanticChangeDetector().run(graph, settings, None) == []


def test_type_change_reaching_a_model_is_reported(settings) -> None:
    before = MetadataGraph(clean_world())
    baseline = SemanticBaseline.capture(before)

    snapshot = clean_world()
    target = column_urn("raw.orders", "amount")
    dataset = snapshot.entities[before.dataset_of_column(target)]
    fields = [
        f.model_copy(update={"native_type": "VARCHAR"}) if f.urn == target else f
        for f in dataset.fields
    ]
    snapshot.entities[dataset.urn] = dataset.model_copy(update={"fields": fields})

    findings = SilentSemanticChangeDetector().run(MetadataGraph(snapshot), settings, baseline)
    assert len(findings) == 1
    assert findings[0].evidence["change_kind"] == "type change"
    assert findings[0].model_urn == model_urn()


def test_currency_change_in_documentation_is_critical(settings) -> None:
    """The demo case: values stay numeric and in-range, but now mean something else."""
    b = GraphBuilder()
    b.dataset(
        "raw.orders",
        {"amount": "DECIMAL"},
        descriptions={"amount": "Order total in USD"},
    )
    b.dataset("analytics.features", {"avg_amount": "DECIMAL"})
    b.col_edge(("raw.orders", "amount"), ("analytics.features", "avg_amount"), "avg")
    b.feature("avg_amount", ["analytics.features"])
    b.model(features=["avg_amount"], deployed=True)
    baseline = SemanticBaseline.capture(MetadataGraph(b.build()))

    b2 = GraphBuilder()
    b2.dataset(
        "raw.orders",
        {"amount": "DECIMAL"},
        descriptions={"amount": "Order total in EUR"},
    )
    b2.dataset("analytics.features", {"avg_amount": "DECIMAL"})
    b2.col_edge(("raw.orders", "amount"), ("analytics.features", "avg_amount"), "avg")
    b2.feature("avg_amount", ["analytics.features"])
    b2.model(features=["avg_amount"], deployed=True)

    findings = SilentSemanticChangeDetector().run(MetadataGraph(b2.build()), settings, baseline)
    assert len(findings) == 1
    assert findings[0].severity is Severity.CRITICAL
    assert findings[0].evidence["change_kind"] == "currency change"


def test_change_nothing_consumes_is_not_reported(settings) -> None:
    b = GraphBuilder().dataset("raw.orphan", {"col": "INT"}, descriptions={"col": "old"})
    baseline = SemanticBaseline.capture(MetadataGraph(b.build()))

    b2 = GraphBuilder().dataset("raw.orphan", {"col": "INT"}, descriptions={"col": "new"})
    assert SilentSemanticChangeDetector().run(MetadataGraph(b2.build()), settings, baseline) == []


# -- compliance propagation ---------------------------------------------------------------


def test_pii_reaching_a_model_is_reported(settings) -> None:
    b = GraphBuilder()
    b.dataset("raw.customers", {"email": "VARCHAR"}, terms={"email": [PII_TERM]})
    b.dataset("analytics.features", {"email_domain": "VARCHAR"})
    b.col_edge(("raw.customers", "email"), ("analytics.features", "email_domain"), "split")
    b.feature("email_domain", ["analytics.features"])
    b.model(features=["email_domain"], deployed=True)

    findings = CompliancePropagationDetector().run(MetadataGraph(b.build()), settings)
    assert len(findings) == 1
    assert findings[0].kind is FindingKind.COMPLIANCE_PROPAGATION
    assert findings[0].severity is Severity.HIGH
    assert findings[0].evidence["classifications"] == [PII_TERM]


def test_pii_not_reaching_a_model_is_ignored(settings) -> None:
    """The clean world has a PII column that no model consumes."""
    graph = MetadataGraph(clean_world())
    assert CompliancePropagationDetector().run(graph, settings) == []


def test_model_already_carrying_the_label_is_not_flagged(settings) -> None:
    b = GraphBuilder()
    b.dataset("raw.customers", {"email": "VARCHAR"}, terms={"email": [PII_TERM]})
    b.dataset("analytics.features", {"email_domain": "VARCHAR"})
    b.col_edge(("raw.customers", "email"), ("analytics.features", "email_domain"), "split")
    b.feature("email_domain", ["analytics.features"])
    b.model(features=["email_domain"], deployed=True)
    snapshot = b.build()
    model = snapshot.entities[model_urn()]
    snapshot.entities[model_urn()] = model.model_copy(update={"terms": [PII_TERM]})

    assert CompliancePropagationDetector().run(MetadataGraph(snapshot), settings) == []


# -- engine -------------------------------------------------------------------------------


def test_engine_runs_all_detectors_and_sorts_by_severity() -> None:
    result = scan(MetadataGraph(leaky_world()))
    assert set(result.detectors_run) == {
        "target-leakage",
        "train-serve-skew",
        "silent-semantic-change",
        "compliance-propagation",
    }
    assert result.findings
    ranks = [f.severity.rank for f in result.findings]
    assert ranks == sorted(ranks, reverse=True)
    assert result.finished_at is not None


def test_engine_reports_clean_world_as_clean() -> None:
    result = scan(MetadataGraph(clean_world()))
    assert result.findings == []
    assert result.blocking() == []


def test_fingerprints_are_stable_across_runs() -> None:
    a = scan(MetadataGraph(leaky_world()))
    b = scan(MetadataGraph(leaky_world()))
    assert a.fingerprints() == b.fingerprints()


def test_new_since_baseline_filters_known_findings() -> None:
    result = scan(MetadataGraph(leaky_world()))
    assert result.new_since(result.fingerprints()) == []
    assert len(result.new_since([])) == len(result.findings)


def test_model_filter_scopes_the_scan() -> None:
    config = FaultlineConfig(model_filter=["does-not-exist"])
    result = scan(MetadataGraph(leaky_world()), config)
    assert result.findings == []
