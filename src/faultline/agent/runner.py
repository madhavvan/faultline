"""The triage agent: Claude, reading DataHub through its own MCP server.

Two tool families are handed to one loop:

* Faultline's own tools, which expose what the detectors proved.
* Every tool the **DataHub MCP server** exposes -- ``search``, ``get_entities``,
  ``get_lineage``, ``get_lineage_paths_between``, ``get_dataset_queries`` -- converted for
  the Anthropic tool runner. That is a real MCP session against the official server, not a
  reimplementation of it.

If the MCP server cannot be reached, the agent still runs on the Faultline tools alone and
says so. Degrading is better than failing, and pretending the context was available would be
worse than either.
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from ..config import FaultlineConfig
from ..models import ScanResult
from .tools import SYSTEM_PROMPT, AgentSession, build_task_prompt, build_tools

logger = logging.getLogger(__name__)

#: Opt into server-side refusal fallbacks. A policy decline on a triage prompt is unlikely,
#: but without this a refused request simply stops, and the scan would silently lose its
#: narratives rather than degrade.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass
class TriageReport:
    """What the agent did, and what it cost."""

    assessed: int = 0
    total: int = 0
    mcp_tools: list[str] = field(default_factory=list)
    mcp_error: str | None = None
    summary: str | None = None
    refused: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""

    @property
    def estimated_cost_usd(self) -> float:
        """Claude Opus 5 list pricing: $5 / MTok in, $25 / MTok out."""
        return (self.input_tokens * 5.0 + self.output_tokens * 25.0) / 1_000_000


class TriageAgent:
    """Enriches a :class:`ScanResult` with narrative and bounded severity judgement."""

    def __init__(self, config: FaultlineConfig | None = None) -> None:
        self.config = config or FaultlineConfig()
        self.settings = self.config.agent

    # -- entry points ---------------------------------------------------------------------

    def run(self, result: ScanResult, graph: Any) -> TriageReport:
        """Synchronous wrapper. Returns a report; ``result`` is mutated in place."""
        import asyncio

        return asyncio.run(self.run_async(result, graph))

    async def run_async(self, result: ScanResult, graph: Any) -> TriageReport:
        report = TriageReport(total=len(result.findings), model=self.settings.model)
        if not result.findings:
            report.summary = "No findings to triage."
            return report

        from anthropic import AsyncAnthropic

        session = AgentSession(result=result, graph=graph)
        tools = build_tools(session)

        async with AsyncExitStack() as stack:
            mcp_tools, mcp_error = await self._mcp_tools(stack)
            if mcp_error and self.settings.require_mcp:
                raise RuntimeError(f"DataHub MCP server unavailable: {mcp_error}")
            report.mcp_error = mcp_error
            report.mcp_tools = [_tool_name(t) for t in mcp_tools]
            tools.extend(mcp_tools)

            client = AsyncAnthropic()
            await self._drive(client, tools, session, report)

        self._apply(result, session, report)
        return report

    # -- MCP ------------------------------------------------------------------------------

    async def _mcp_tools(self, stack: AsyncExitStack) -> tuple[list, str | None]:
        """Open a session with the DataHub MCP server and convert its tools."""
        if not self.settings.mcp_command:
            return [], "no MCP command configured"

        try:
            from anthropic.lib.tools.mcp import async_mcp_tool
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            params = StdioServerParameters(
                command=self.settings.mcp_command,
                args=list(self.settings.mcp_args),
                env=self._mcp_env(),
            )
            # The MCP server logs banners, deprecation warnings and connection retries to
            # stderr. Left attached it drowns the report in noise that says nothing to the
            # person reading it, so it goes to a log file the user can consult instead.
            # The handle *is* context-managed -- by the AsyncExitStack that owns it for the
            # whole MCP session. Ruff does not recognise enter_context() as such.
            errlog = stack.enter_context(
                open(self._mcp_log_path(), "a", encoding="utf-8")  # noqa: SIM115
            )
            read, write = await stack.enter_async_context(stdio_client(params, errlog=errlog))
            mcp_session = await stack.enter_async_context(ClientSession(read, write))
            await mcp_session.initialize()
            listed = await mcp_session.list_tools()
            return [async_mcp_tool(t, mcp_session) for t in listed.tools], None
        except Exception as exc:
            logger.warning("DataHub MCP server unavailable: %s", exc)
            return [], f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _mcp_log_path() -> str:
        import tempfile
        from pathlib import Path

        path = Path(tempfile.gettempdir()) / "faultline-mcp.log"
        return str(path)

    def _mcp_env(self) -> dict[str, str]:
        import os

        env = dict(os.environ)
        env["DATAHUB_GMS_URL"] = self.config.datahub.resolved_server()
        token = self.config.datahub.resolved_token()
        if token:
            env["DATAHUB_GMS_TOKEN"] = token
        return env

    # -- the loop -------------------------------------------------------------------------

    async def _drive(self, client, tools, session: AgentSession, report: TriageReport) -> None:
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": build_task_prompt(session.result)}
        ]

        kwargs: dict[str, Any] = {
            "model": self.settings.model,
            "max_tokens": self.settings.max_tokens,
            "system": SYSTEM_PROMPT,
            "messages": messages,
            "tools": tools,
            "output_config": {"effort": self.settings.effort},
            "thinking": {"type": "adaptive"},
            # Enough iterations to read and assess every finding without running away.
            "max_iterations": max(12, len(session.result.findings) * 6),
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",
        }

        try:
            runner = client.beta.messages.tool_runner(**kwargs)
        except TypeError:
            # An SDK without the fallbacks parameter: drop it rather than fail the scan.
            kwargs.pop("betas", None)
            kwargs.pop("fallbacks", None)
            runner = client.beta.messages.tool_runner(**kwargs)

        final = None
        async for message in runner:
            final = message
            usage = getattr(message, "usage", None)
            if usage:
                report.input_tokens += getattr(usage, "input_tokens", 0) or 0
                report.output_tokens += getattr(usage, "output_tokens", 0) or 0

        if final is None:
            return

        # Check the stop reason before reading content: on a refusal `content` is empty or
        # partial, and indexing it blindly is how this would crash in production.
        if getattr(final, "stop_reason", None) == "refusal":
            report.refused = True
            details = getattr(final, "stop_details", None)
            report.summary = (
                "Triage was declined by safety classifiers"
                + (f" ({details.category})" if details and details.category else "")
                + ". Findings and proofs are unaffected."
            )
            return

        report.summary = "".join(
            block.text for block in final.content if getattr(block, "type", None) == "text"
        ).strip() or None

    # -- applying -------------------------------------------------------------------------

    def _apply(self, result: ScanResult, session: AgentSession, report: TriageReport) -> None:
        """Fold assessments back into the findings. Proofs are never touched."""
        for finding in result.findings:
            assessment = session.assessments.get(finding.fingerprint)
            if assessment is None:
                continue
            finding.narrative = assessment.narrative
            if assessment.severity and assessment.severity is not finding.severity:
                finding.evidence["severity_before_triage"] = finding.severity.value
                finding.evidence["severity_change_reason"] = assessment.severity_reason
                finding.severity = assessment.severity
            report.assessed += 1

        result.stats["triage"] = {
            "assessed": report.assessed,
            "total": report.total,
            "mcp_tools": len(report.mcp_tools),
            "mcp_error": report.mcp_error,
            "model": report.model,
            "input_tokens": report.input_tokens,
            "output_tokens": report.output_tokens,
            "estimated_cost_usd": round(report.estimated_cost_usd, 4),
        }


def _tool_name(tool: Any) -> str:
    for attr in ("name", "__name__"):
        value = getattr(tool, attr, None)
        if isinstance(value, str):
            return value
    inner = getattr(tool, "tool", None)
    return getattr(inner, "name", type(tool).__name__)
