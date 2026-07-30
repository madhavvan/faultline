"""Scan orchestration: run every enabled detector over a graph and assemble the result.

Deliberately boring. All the intelligence lives in the detectors; this layer's job is to run
them predictably, refuse to emit findings it cannot substantiate, and never let one broken
detector take down a scan.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone

from .config import FaultlineConfig
from .detectors import SemanticBaseline, active_detectors
from .graph.graph import MetadataGraph
from .models import Finding, ScanResult

logger = logging.getLogger(__name__)


def scan(
    graph: MetadataGraph,
    config: FaultlineConfig | None = None,
    baseline: SemanticBaseline | None = None,
) -> ScanResult:
    """Run all enabled detectors and return a deduplicated, validated result."""
    config = config or FaultlineConfig()
    settings = config.detectors
    detectors = active_detectors(settings)

    started = datetime.now(timezone.utc)
    collected: list[Finding] = []
    stats: dict[str, object] = {"graph": graph.stats(), "graph_origin": graph.snapshot.origin}
    per_detector: dict[str, int] = {}
    errors: dict[str, str] = {}
    rejected = 0

    models = _filtered_models(graph, config)

    for detector in detectors:
        try:
            produced = detector.run(graph, settings, baseline)
        except Exception as exc:  # one bad detector must not lose the whole scan
            logger.exception("detector %s failed", detector.name)
            errors[detector.name] = f"{type(exc).__name__}: {exc}"
            continue

        kept: list[Finding] = []
        for finding in produced:
            if config.model_filter and finding.model_urn and finding.model_urn not in models:
                continue
            problems = finding.validate_proofs()
            if problems:
                # A finding whose own proof does not hold together is a detector bug. Drop
                # it loudly rather than reporting a defect we cannot substantiate.
                logger.error(
                    "detector %s produced an unsubstantiated finding %r: %s",
                    detector.name,
                    finding.title,
                    "; ".join(problems),
                )
                rejected += 1
                continue
            kept.append(finding)

        per_detector[detector.name] = len(kept)
        collected.extend(kept)

    deduped = _dedupe(collected)
    collapsed = _collapse_cascades(deduped, graph)

    stats["per_detector"] = per_detector
    stats["duplicates_removed"] = len(collected) - len(deduped)
    stats["cascades_collapsed"] = len(deduped) - len(collapsed)
    stats["unsubstantiated_rejected"] = rejected
    if errors:
        stats["detector_errors"] = errors

    return ScanResult(
        findings=collapsed,
        scanned_models=sorted(models),
        scanned_features=sorted(f.urn for f in graph.features()),
        detectors_run=[d.name for d in detectors],
        started_at=started,
        finished_at=datetime.now(timezone.utc),
        graph_source=graph.snapshot.source,
        stats=stats,
    )


def _filtered_models(graph: MetadataGraph, config: FaultlineConfig) -> set[str]:
    urns = {m.urn for m in graph.models()}
    if not config.model_filter:
        return urns
    return {u for u in urns if any(needle in u for needle in config.model_filter)}


def _collapse_cascades(findings: list[Finding], graph: MetadataGraph) -> list[Finding]:
    """Report one defect once, at its root cause, instead of at every column it passes.

    A currency change on ``raw.orders.amount`` also changes every column downstream of it,
    and a PII column is still PII three joins later. Emitting a finding at each hop is
    technically true and practically useless: the reviewer has to work out which one to
    actually fix, and the repetition is what gets a tool muted.

    So within a group of findings that share a kind, a model and a feature, any finding
    whose subject column is *downstream* of another's is dropped. What survives is the
    column a human would actually go and change. The suppressed hops remain visible in the
    surviving finding's proof path, so nothing is hidden -- only de-duplicated.
    """
    groups: dict[tuple, list[Finding]] = defaultdict(list)
    passthrough: list[Finding] = []

    for finding in findings:
        if not finding.column_urn:
            passthrough.append(finding)
            continue
        groups[(finding.kind, finding.model_urn, finding.feature_urn)].append(finding)

    kept: list[Finding] = list(passthrough)

    for group in groups.values():
        if len(group) == 1:
            kept.extend(group)
            continue
        roots: list[Finding] = []
        for candidate in group:
            downstream_of_another = any(
                other is not candidate
                and other.column_urn != candidate.column_urn
                and graph.reaches(other.column_urn, candidate.column_urn)
                for other in group
            )
            if not downstream_of_another:
                roots.append(candidate)
        # If every member is downstream of another (a cycle), keep the group intact rather
        # than silently discarding a real finding.
        kept.extend(roots or group)

    return sorted(kept, key=lambda f: (-f.severity.rank, f.kind.value, f.title))


def _dedupe(findings: list[Finding]) -> list[Finding]:
    """Collapse identical findings, keeping the most severe rendition of each."""
    best: dict[str, Finding] = {}
    for finding in findings:
        key = finding.fingerprint
        current = best.get(key)
        if current is None or finding.severity.rank > current.severity.rank:
            best[key] = finding
    return sorted(best.values(), key=lambda f: (-f.severity.rank, f.kind.value, f.title))
