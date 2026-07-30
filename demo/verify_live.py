"""End-to-end proof against a live DataHub instance.

This is the check that the submission's central claim is true: that Faultline reads a real
DataHub graph, proves the same defects there that it proves offline, and contributes the
findings back in a form the instance actually stores.

    1. emit the dbt-derived demo graph into DataHub
    2. read it back through the ordinary client (not the fixture)
    3. scan the round-tripped graph
    4. assert it finds the same four faults, with the same fingerprints
    5. apply the write-back
    6. read the tags, structured properties and incidents back out of DataHub

Every step is verified against what the server returns, not against what was sent.

Run: python demo/verify_live.py [--skip-writeback]
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from faultline.config import FaultlineConfig  # noqa: E402
from faultline.detectors import SemanticBaseline  # noqa: E402
from faultline.emit import SnapshotEmitter  # noqa: E402
from faultline.engine import scan  # noqa: E402
from faultline.graph.graph import MetadataGraph  # noqa: E402
from faultline.graph.loader import DataHubLoader, load_snapshot, save_snapshot  # noqa: E402
from faultline.models import FindingKind, short_urn  # noqa: E402
from faultline.writeback import DataHubWriter  # noqa: E402

DATA = REPO / "src" / "faultline" / "data"
CAPTURED = REPO / "examples" / "live-datahub-graph.json"

EXPECTED = {
    FindingKind.TARGET_LEAKAGE,
    FindingKind.TRAIN_SERVE_SKEW,
    FindingKind.SILENT_SEMANTIC_CHANGE,
    FindingKind.COMPLIANCE_PROPAGATION,
}

PASS, FAIL = "  PASS", "  FAIL"
_failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"{PASS if condition else FAIL}  {label}{(' — ' + detail) if detail else ''}")
    if not condition:
        _failures.append(label)
    return condition


def step(n: int, title: str) -> None:
    print(f"\n[{n}] {title}")
    print("-" * 72)


def _await_indexing(config, expected_datasets: int, budget: int) -> None:
    """Wait until search has caught up with what was just emitted."""
    from faultline.graph.loader import DataHubLoader

    client = DataHubLoader(config.datahub).connect()
    deadline = time.time() + max(budget, 180)
    seen = 0
    while time.time() < deadline:
        try:
            seen = sum(1 for _ in client.get_urns_by_filter(entity_types=["dataset"]))
        except Exception:
            seen = 0
        if seen >= expected_datasets:
            print(f"      search indexed {seen}/{expected_datasets} datasets")
            return
        time.sleep(5)
    print(f"      WARNING: only {seen}/{expected_datasets} datasets indexed before timeout")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-writeback", action="store_true")
    parser.add_argument("--settle", type=int, default=25, help="seconds to wait for indexing")
    args = parser.parse_args()

    logging.getLogger().setLevel(logging.ERROR)
    config = FaultlineConfig()
    server = config.datahub.resolved_server()
    print(f"Faultline live verification against {server}")

    # ---------------------------------------------------------------- 1. emit
    step(1, "Emit the dbt-derived demo graph into DataHub")
    local = load_snapshot(DATA / "demo-graph.json")
    report = SnapshotEmitter(config.datahub).emit(local)
    print(f"      {report.summary()}")
    if not check("emitted without failures", not report.failures, f"{len(report.failures)} failed"):
        for f in report.failures[:5]:
            print(f"        {f}")
        return 1

    # DataHub indexes asynchronously through Kafka, so a fixed sleep is a guess that is
    # either wasteful or wrong. Poll until the expected entities are searchable instead —
    # the loader reads through search, so an under-indexed instance looks like a smaller
    # graph rather than an error.
    expected_datasets = sum(
        1 for e in local.entities.values() if e.kind.value == "DATASET"
    )
    _await_indexing(config, expected_datasets, args.settle)

    # ------------------------------------------------------- 2. read it back
    step(2, "Read the graph back out of DataHub")
    live = DataHubLoader(config.datahub).load()
    graph = MetadataGraph(live)
    stats = graph.stats()
    print(f"      {stats}")

    check("datasets round-tripped", stats.get("DATASET", 0) >= 10, str(stats.get("DATASET")))
    check("ML models round-tripped", stats.get("ML_MODEL", 0) >= 1)
    check("ML features round-tripped", stats.get("ML_FEATURE", 0) >= 9)
    check(
        "column-level lineage round-tripped",
        stats["column_edges"] >= 40,
        f"{stats['column_edges']} edges",
    )

    model = next(iter(graph.models()), None)
    check("model carries its deployment", bool(model and model.is_deployed()))
    check(
        "label column survived the round trip",
        bool(model and model.custom_properties.get("faultline.label_column") == "churned"),
    )
    labelled = graph.columns_with_term("urn:li:glossaryTerm:Label")
    check("Label glossary term is on a column", bool(labelled), f"{len(labelled)} column(s)")
    pii = graph.columns_with_term("urn:li:glossaryTerm:PII")
    check("PII glossary terms round-tripped", len(pii) >= 3, f"{len(pii)} column(s)")

    save_snapshot(live, CAPTURED)
    print(f"      captured live graph -> {CAPTURED.relative_to(REPO)}")

    # ------------------------------------------------------------- 3. scan it
    step(3, "Scan the graph as loaded from DataHub")
    baseline = SemanticBaseline.model_validate_json(
        (DATA / "demo-baseline.json").read_text(encoding="utf-8")
    )
    result = scan(graph, config, baseline)
    for finding in result.sorted_findings():
        print(f"      {finding.severity.value:<9} {finding.title[:78]}")

    check("exactly four findings", len(result.findings) == 4, str(len(result.findings)))
    check("all four fault classes found", {f.kind for f in result.findings} == EXPECTED)
    check("every finding is substantiated", all(not f.validate_proofs() for f in result.findings))

    # Fingerprints must match the offline run: same graph, same conclusions, whether it came
    # from a fixture or from a live server.
    offline = scan(MetadataGraph(load_snapshot(DATA / "demo-graph.json")), config, baseline)
    check(
        "live findings match the offline run",
        result.fingerprints() == offline.fingerprints(),
        f"live={len(result.fingerprints())} offline={len(offline.fingerprints())}",
    )

    if args.skip_writeback:
        return _finish()

    # -------------------------------------------------------- 4. contribute back
    step(4, "Write the findings back into DataHub")
    writer = DataHubWriter(config)
    plan = writer.plan(result, report_url="https://example.invalid/faultline/live-verify")
    print(f"      plan: {len(plan)} change(s) — {plan.by_kind()}")
    applied = writer.apply(plan, dry_run=False)
    print(f"      applied={applied.applied} failed={applied.failed}")
    for err in applied.errors[:5]:
        print(f"        {err}")
    check("write-back applied cleanly", applied.failed == 0 and applied.applied > 0)

    print("      waiting 15s for indexing ...")
    time.sleep(15)

    # ------------------------------------------------- 5. verify it landed
    step(5, "Read the contribution back out of DataHub")
    client = DataHubLoader(config.datahub).connect()
    from datahub.metadata import schema_classes as S

    model_urn = next(f.model_urn for f in result.findings if f.model_urn)
    short_model = short_urn(model_urn)

    tags = client.get_aspect(model_urn, S.GlobalTagsClass)
    tag_names = [t.tag.rsplit(":", 1)[-1] for t in (tags.tags if tags else [])]
    check(
        "Faultline tags are on the model in DataHub",
        any(t.startswith("Faultline_") for t in tag_names),
        ", ".join(tag_names) or "none",
    )

    props = client.get_aspect(model_urn, S.StructuredPropertiesClass)
    assigned = {
        p.propertyUrn.rsplit(":", 1)[-1]: p.values for p in (props.properties if props else [])
    }
    check(
        "structured properties are on the model",
        any(k.startswith("io.faultline") for k in assigned),
        ", ".join(sorted(assigned)) or "none",
    )
    risk = assigned.get("io.faultline.risk")
    check("risk property records the worst severity", bool(risk), str(risk))

    blocking = [f for f in result.findings if f.is_blocking()]
    if blocking:
        incident_urn = f"urn:li:incident:faultline-{blocking[0].fingerprint}"
        info = client.get_aspect(incident_urn, S.IncidentInfoClass)
        check("an incident was opened for a blocking finding", info is not None)
        if info:
            check(
                "the incident carries the proof path",
                "Proof:" in (info.description or ""),
            )
            # DataHub rejects an mlModel URN in IncidentInfo.entities (422), so the incident
            # hangs off the dataset the defect originates in and names the model in its
            # title. Assert both halves of that.
            check(
                "the incident targets an entity DataHub accepts",
                all(
                    u.startswith(("urn:li:dataset:", "urn:li:dataJob:", "urn:li:dashboard:"))
                    for u in (info.entities or [])
                )
                and bool(info.entities),
                ", ".join(info.entities or []) or "none",
            )
            check(
                "the incident names the affected model",
                short_model in (info.title or ""),
                info.title or "",
            )

    memory = client.get_aspect(model_urn, S.InstitutionalMemoryClass)
    check("report link attached", bool(memory and memory.elements))

    return _finish()


def _finish() -> int:
    print("\n" + "=" * 72)
    if _failures:
        print(f"FAILED — {len(_failures)} check(s) did not pass:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED — Faultline works end to end against a live DataHub.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
