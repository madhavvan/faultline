"""The replay fixture must be a faithful round-trip of a live load.

If these fail, offline mode and live mode can disagree -- which would make the demo a lie.
"""

from __future__ import annotations

from faultline.config import FaultlineConfig
from faultline.detectors import TargetLeakageDetector
from faultline.engine import scan
from faultline.graph.graph import MetadataGraph
from faultline.graph.loader import load_snapshot, save_snapshot

from .factory import leaky_world


def test_snapshot_round_trips_byte_for_byte(tmp_path) -> None:
    original = leaky_world()
    path = save_snapshot(original, tmp_path / "capture.json")
    restored = load_snapshot(path)

    assert set(restored.entities) == set(original.entities)
    assert len(restored.column_edges) == len(original.column_edges)
    assert restored.stats()["entities"] == original.stats()["entities"]


def test_detectors_agree_across_live_and_replay(tmp_path) -> None:
    original = leaky_world()
    settings = FaultlineConfig().detectors

    live = TargetLeakageDetector().run(MetadataGraph(original), settings)
    replay = TargetLeakageDetector().run(
        MetadataGraph(load_snapshot(save_snapshot(original, tmp_path / "c.json"))), settings
    )

    assert [f.fingerprint for f in live] == [f.fingerprint for f in replay]
    assert [f.severity for f in live] == [f.severity for f in replay]


def test_replay_source_is_labelled(tmp_path) -> None:
    """A scan must state where its graph came from, so a report is never ambiguous."""
    path = save_snapshot(leaky_world(), tmp_path / "capture.json")
    result = scan(MetadataGraph(load_snapshot(path)))
    assert result.graph_source.startswith("replay:")


def test_urn_name_extraction_handles_platform_qualified_urns() -> None:
    """Platform-qualified URNs are (platform, name, env) — the last segment is the
    environment, so taking it names every deployment "PROD"."""
    from faultline.graph.loader import _last_urn_part

    assert (
        _last_urn_part(
            "urn:li:mlModelDeployment:(urn:li:dataPlatform:sagemaker,churn-prod,PROD)"
        )
        == "churn-prod"
    )
    assert (
        _last_urn_part("urn:li:mlModel:(urn:li:dataPlatform:mlflow,churn_v7,PROD)")
        == "churn_v7"
    )
    # Two-part URNs have no platform, so the trailing segment is the name.
    assert _last_urn_part("urn:li:mlFeature:(customer_features,avg_value)") == "avg_value"
