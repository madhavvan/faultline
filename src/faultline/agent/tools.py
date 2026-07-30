"""Tools and prompt for the triage agent.

The tool surface is deliberately narrow. The agent can *read* what the detectors proved and
*read* DataHub for surrounding context, and it has exactly one way to produce output:
:func:`record_assessment`. It cannot create a finding, and it cannot promote one beyond what
the graph substantiates.

That constraint is the whole point. Structural claims come from deterministic traversal;
the model supplies judgement about consequence and priority, which is the part it is
actually good at.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..models import Finding, ScanResult, Severity, short_urn

SYSTEM_PROMPT = """\
You are the triage layer of Faultline, a tool that detects structural ML failures from a \
DataHub metadata graph.

The findings you are given have already been *proven*. Each one carries a lineage path that \
exists in the graph — a chain of real columns and transformations. You are not being asked \
whether the path exists; it does.

Your job is to judge what each one means for the team that owns it, and to say so in plain \
language a reviewer can act on in fifteen seconds.

For every finding:
  1. Read the proof path. Use the DataHub tools to gather context the detector could not \
see — who owns the assets, what else is downstream, how the data is actually queried, \
whether other consumers share the blast radius.
  2. Call `record_assessment` exactly once with a short narrative (2–3 sentences) explaining \
the real-world consequence. Lead with the consequence, not a restatement of the finding.
  3. Adjust severity only if the context genuinely changes the picture, and say why. \
You may move it by at most one step in either direction. The detector's severity stands \
unless you have a specific reason from the context you gathered.

Hard rules:
  - Never invent a finding, a lineage edge, or a column. If a tool did not return it, it \
does not exist.
  - Never claim a path is or is not real. That was settled before you were called.
  - If context is unavailable (DataHub unreachable, entity missing), say so plainly and \
assess on the proof alone. Do not speculate to fill the gap.
  - Write for a data engineer who did not ask for this and is mid-way through something \
else. No preamble, no restating the title, no hedging.
"""


@dataclass
class Assessment:
    """One finding's triage output."""

    fingerprint: str
    narrative: str
    severity: Severity | None = None
    severity_reason: str | None = None
    context_used: list[str] = field(default_factory=list)


@dataclass
class AgentSession:
    """Mutable state shared between the tool callbacks and the runner."""

    result: ScanResult
    graph: Any
    assessments: dict[str, Assessment] = field(default_factory=dict)
    tool_calls: list[str] = field(default_factory=list)

    def finding(self, fingerprint: str) -> Finding | None:
        for f in self.result.findings:
            if f.fingerprint == fingerprint:
                return f
        return None


def build_tools(session: AgentSession) -> list:
    """Construct the Faultline-native tools, closed over ``session``."""
    # The runner is async (the MCP client requires it), so the tools must be too:
    # a sync BetaFunctionTool is not recognised by the async runner and ends up being
    # serialised into the request body instead of executed.
    from anthropic import beta_async_tool

    @beta_async_tool
    async def list_findings() -> str:
        """List every proven finding awaiting triage.

        Returns a JSON array of findings with their fingerprint, kind, detector-assigned
        severity, title, and the model each one affects.
        """
        session.tool_calls.append("list_findings")
        return json.dumps(
            [
                {
                    "fingerprint": f.fingerprint,
                    "kind": f.kind.value,
                    "severity": f.severity.value,
                    "title": f.title,
                    "model": short_urn(f.model_urn) if f.model_urn else None,
                    "model_urn": f.model_urn,
                    "assessed": f.fingerprint in session.assessments,
                }
                for f in session.result.sorted_findings()
            ],
            indent=2,
        )

    @beta_async_tool
    async def get_finding(fingerprint: str) -> str:
        """Get the full detail of one finding, including its proof path and evidence.

        Args:
            fingerprint: The finding's fingerprint, from `list_findings`.
        """
        session.tool_calls.append(f"get_finding:{fingerprint}")
        finding = session.finding(fingerprint)
        if finding is None:
            return f"No finding with fingerprint {fingerprint!r}. Call list_findings first."

        return json.dumps(
            {
                "fingerprint": finding.fingerprint,
                "kind": finding.kind.value,
                "severity": finding.severity.value,
                "title": finding.title,
                "summary": finding.summary,
                "model_urn": finding.model_urn,
                "feature_urn": finding.feature_urn,
                "column_urn": finding.column_urn,
                "dataset_urn": finding.dataset_urn,
                "remediation": finding.remediation,
                "evidence": finding.evidence,
                "proofs": [
                    {
                        "hops": p.length,
                        "confidence": p.confidence,
                        "path": p.render(indent=""),
                    }
                    for p in finding.proofs
                ],
            },
            indent=2,
            default=str,
        )

    @beta_async_tool
    async def get_blast_radius(column_urn: str) -> str:
        """List every ML model downstream of a column, with the hop count to each.

        Use this to judge how far a defect actually reaches before assigning severity.

        Args:
            column_urn: A DataHub schemaField URN.
        """
        session.tool_calls.append(f"get_blast_radius:{short_urn(column_urn)}")
        affected = session.graph.models_depending_on(column_urn)
        if not affected:
            return f"No models are downstream of {short_urn(column_urn)}."
        return json.dumps(
            [
                {
                    "model": short_urn(urn),
                    "model_urn": urn,
                    "hops": path.length,
                    "deployed": bool(
                        (e := session.graph.entity(urn)) and e.is_deployed()
                    ),
                }
                for urn, path in sorted(affected.items())
            ],
            indent=2,
        )

    @beta_async_tool
    async def record_assessment(
        fingerprint: str,
        narrative: str,
        severity: str = "",
        severity_reason: str = "",
    ) -> str:
        """Record your triage of one finding. Call this exactly once per finding.

        Args:
            fingerprint: The finding being assessed.
            narrative: 2-3 sentences on the real-world consequence, leading with the impact.
            severity: Optional. CRITICAL, HIGH, MEDIUM, LOW or INFO. Omit to keep the
                detector's severity. May differ by at most one step from it.
            severity_reason: Required when severity is given. Why the context changes it.
        """
        session.tool_calls.append(f"record_assessment:{fingerprint}")
        finding = session.finding(fingerprint)
        if finding is None:
            return f"No finding with fingerprint {fingerprint!r}."

        if not narrative.strip():
            return "narrative must not be empty."

        adjusted: Severity | None = None
        if severity:
            try:
                candidate = Severity(severity.strip().upper())
            except ValueError:
                return (
                    f"{severity!r} is not a severity. "
                    "Use CRITICAL, HIGH, MEDIUM, LOW or INFO."
                )
            if not severity_reason.strip():
                return "severity_reason is required when you change severity."
            distance = abs(candidate.rank - finding.severity.rank)
            if distance > 1:
                return (
                    f"Cannot move {finding.severity.value} to {candidate.value}: that is "
                    f"{distance} steps. Severity may move by at most one step, because the "
                    f"detector's ranking is derived from the proof."
                )
            adjusted = candidate

        session.assessments[fingerprint] = Assessment(
            fingerprint=fingerprint,
            narrative=narrative.strip(),
            severity=adjusted,
            severity_reason=severity_reason.strip() or None,
            context_used=list(session.tool_calls),
        )
        remaining = len(session.result.findings) - len(session.assessments)
        return (
            f"Recorded. {remaining} finding(s) still need assessment."
            if remaining
            else "Recorded. All findings assessed."
        )

    return [list_findings, get_finding, get_blast_radius, record_assessment]


def build_task_prompt(result: ScanResult) -> str:
    """The opening user turn."""
    lines = [
        f"Faultline scanned `{result.graph_source}` and proved "
        f"{len(result.findings)} structural finding(s) across "
        f"{len(result.scanned_models)} model(s).",
        "",
        "Triage each one. Start with `list_findings`, then work through them.",
        "Call `record_assessment` once per finding. When every finding has an assessment, "
        "reply with a two-sentence summary for the team and stop.",
    ]
    return "\n".join(lines)
