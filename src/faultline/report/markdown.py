"""Markdown rendering: the PR comment and the standalone report.

The audience is a reviewer who has thirty seconds and did not ask for this. Lead with the
verdict, show the proof, give the fix. Everything else goes behind a fold.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..models import Finding, ScanResult, Severity, short_urn

SEVERITY_ICON = {
    Severity.CRITICAL: "🔴",
    Severity.HIGH: "🟠",
    Severity.MEDIUM: "🟡",
    Severity.LOW: "🔵",
    Severity.INFO: "⚪",
}

MARKER = "<!-- faultline-report -->"


def render_pr_comment(
    result: ScanResult,
    new_findings: list[Finding] | None = None,
    repo_url: str | None = None,
) -> str:
    """The comment Faultline posts on a pull request."""
    findings = new_findings if new_findings is not None else result.sorted_findings()
    blocking = [f for f in findings if f.is_blocking()]

    lines = [MARKER, ""]
    lines.append(_headline(findings, blocking))
    lines.append("")

    if not findings:
        lines.append(
            "No structural defects reachable from a model. Faultline checked "
            f"{len(result.scanned_models)} model(s) and "
            f"{len(result.scanned_features)} feature(s) against the DataHub lineage graph."
        )
        lines.append("")
        lines.append(_footer(result))
        return "\n".join(lines)

    lines.append(_summary_table(findings))
    lines.append("")

    for finding in findings:
        lines.extend(_finding_block(finding))
        lines.append("")

    lines.append(_footer(result))
    return "\n".join(lines)


def render_report(result: ScanResult) -> str:
    """A full standalone report, for ``examples/`` and for humans reading after the fact."""
    findings = result.sorted_findings()
    lines = [
        "# Faultline scan report",
        "",
        f"- **Graph source:** `{result.graph_source}`",
        f"- **Models scanned:** {len(result.scanned_models)}",
        f"- **Features scanned:** {len(result.scanned_features)}",
        f"- **Detectors run:** {', '.join(result.detectors_run)}",
        f"- **Findings:** {len(findings)}",
        "",
    ]
    if findings:
        lines.append(_summary_table(findings))
        lines.append("")
        for finding in findings:
            lines.extend(_finding_block(finding, heading="##"))
            lines.append("")
    else:
        lines.append("No structural defects found.")
        lines.append("")
    lines.append(_footer(result))
    return "\n".join(lines)


# --------------------------------------------------------------------------------------


def _headline(findings: list[Finding], blocking: list[Finding]) -> str:
    if not findings:
        return "## ✅ Faultline: no structural risk to any model"
    if blocking:
        noun = "defect" if len(blocking) == 1 else "defects"
        return (
            f"## 🔴 Faultline: {len(blocking)} structural {noun} "
            f"{'blocks' if len(blocking) == 1 else 'block'} this change"
        )
    noun = "finding" if len(findings) == 1 else "findings"
    return f"## 🟡 Faultline: {len(findings)} non-blocking {noun}"


def _summary_table(findings: Iterable[Finding]) -> str:
    rows = [
        "| | Finding | Model | Proof |",
        "|---|---|---|---|",
    ]
    for f in findings:
        model = f"`{short_urn(f.model_urn)}`" if f.model_urn else "—"
        hops = f.proofs[0].length if f.proofs else 0
        rows.append(
            f"| {SEVERITY_ICON[f.severity]} **{f.severity.value}** | {f.title} | {model} "
            f"| {hops} hop(s) |"
        )
    return "\n".join(rows)


def _finding_block(finding: Finding, heading: str = "###") -> list[str]:
    icon = SEVERITY_ICON[finding.severity]
    lines = [f"{heading} {icon} {finding.severity.value} — {finding.title}", ""]

    facts = []
    if finding.model_urn:
        facts.append(f"**Model:** `{short_urn(finding.model_urn)}`")
    if finding.feature_urn:
        facts.append(f"**Feature:** `{short_urn(finding.feature_urn)}`")
    deployments = finding.evidence.get("deployments")
    if finding.evidence.get("model_deployed"):
        served = f" ({', '.join(short_urn(d) for d in deployments)})" if deployments else ""
        facts.append(f"**Status:** deployed{served}")
    if facts:
        lines.append(" · ".join(facts))
        lines.append("")

    lines.append(finding.summary)
    lines.append("")

    if finding.narrative:
        lines.append(f"> {finding.narrative}")
        lines.append("")

    for i, proof in enumerate(finding.proofs):
        label = "Proof" if len(finding.proofs) == 1 else f"Proof {i + 1}"
        confidence = (
            f" _(weakest-link confidence {proof.confidence:.2f})_" if proof.confidence < 1.0 else ""
        )
        lines.append(f"**{label}** — {proof.length} lineage hop(s){confidence}")
        lines.append("")
        lines.append("```")
        lines.append(proof.render(indent=""))
        lines.append("```")
        lines.append("")

    if finding.remediation:
        lines.append(f"**Fix:** {finding.remediation}")
        lines.append("")

    lines.append(
        f"<details><summary>Evidence · <code>{finding.fingerprint}</code></summary>\n\n"
        f"```json\n{_pretty(finding.evidence)}\n```\n\n</details>"
    )
    return lines


def _footer(result: ScanResult) -> str:
    counts = result.severity_counts()
    parts = [f"{v} {k.lower()}" for k, v in counts.items() if v]
    tally = ", ".join(parts) if parts else "no findings"
    duration = (
        f" in {result.duration_seconds:.2f}s" if result.duration_seconds is not None else ""
    )
    return (
        "<sub>"
        f"Faultline · {tally}{duration} · graph `{result.graph_source}` · "
        "every finding above is a path that exists in the DataHub lineage graph, "
        "not a statistical inference."
        "</sub>"
    )


def _pretty(payload: dict) -> str:
    import json

    return json.dumps(payload, indent=2, sort_keys=True, default=str)
