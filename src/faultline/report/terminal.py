"""Terminal rendering. This is what the demo video shows, so it has to read well."""

from __future__ import annotations

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..models import Finding, ScanResult, Severity, short_urn

SEVERITY_STYLE = {
    Severity.CRITICAL: "bold red",
    Severity.HIGH: "bold dark_orange",
    Severity.MEDIUM: "yellow",
    Severity.LOW: "cyan",
    Severity.INFO: "dim",
}

_UNICODE_ICONS = {
    Severity.CRITICAL: "●",
    Severity.HIGH: "●",
    Severity.MEDIUM: "●",
    Severity.LOW: "○",
    Severity.INFO: "·",
}

_ASCII_ICONS = {
    Severity.CRITICAL: "X",
    Severity.HIGH: "!",
    Severity.MEDIUM: "*",
    Severity.LOW: "-",
    Severity.INFO: ".",
}


def _icons(console: Console) -> dict[Severity, str]:
    """Pick a glyph set the target stream can actually encode.

    UTF-8 is forced at CLI start-up, but Faultline is also importable as a library and may be
    printing into a stream we do not control. Falling back beats raising.
    """
    encoding = getattr(console.file, "encoding", None) or "utf-8"
    try:
        "".join(_UNICODE_ICONS.values()).encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return _ASCII_ICONS
    return _UNICODE_ICONS


def default_console() -> Console:
    """A console that survives a Windows legacy terminal.

    rich's legacy-Windows renderer writes through a console API that raises on characters
    the active codepage cannot represent — which kills the run mid-report rather than
    degrading. Opting out keeps output on the plain stream, where the encoding-adaptive
    glyph set above handles the rest.
    """
    return Console(legacy_windows=False, soft_wrap=False)


def print_result(result: ScanResult, console: Console | None = None, verbose: bool = False) -> None:
    console = console or default_console()
    findings = result.sorted_findings()

    console.print()
    console.print(_header(result, findings))
    console.print()

    if not findings:
        console.print(
            Panel(
                Text(
                    "No structural defects reachable from any model.\n"
                    "Every feature's derivation was traced to its sources and checked.",
                    justify="left",
                ),
                title="[bold green]clean[/]",
                border_style="green",
            )
        )
        console.print()
        _print_footer(console, result)
        return

    console.print(_summary_table(findings, _icons(console)))
    console.print()

    for finding in findings:
        console.print(_finding_panel(finding, verbose=verbose))
        console.print()

    _print_footer(console, result)


def _header(result: ScanResult, findings: list[Finding]) -> Text:
    blocking = [f for f in findings if f.is_blocking()]
    if not findings:
        return Text("FAULTLINE  ·  no structural risk detected", style="bold green")
    if blocking:
        return Text(
            f"FAULTLINE  ·  {len(blocking)} blocking, {len(findings)} total",
            style="bold red",
        )
    return Text(f"FAULTLINE  ·  {len(findings)} non-blocking findings", style="bold yellow")


def _summary_table(findings: list[Finding], icons: dict[Severity, str]) -> Table:
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("", width=2)
    table.add_column("severity", width=9)
    table.add_column("finding")
    table.add_column("model", overflow="fold")
    table.add_column("hops", justify="right", width=5)

    for f in findings:
        style = SEVERITY_STYLE[f.severity]
        table.add_row(
            Text(icons[f.severity], style=style),
            Text(f.severity.value, style=style),
            f.title,
            short_urn(f.model_urn) if f.model_urn else "—",
            str(f.proofs[0].length if f.proofs else 0),
        )
    return table


def _finding_panel(finding: Finding, verbose: bool = False) -> Panel:
    style = SEVERITY_STYLE[finding.severity]
    body: list = [Text(finding.summary)]

    if finding.narrative:
        body.append(Text(""))
        body.append(Text(finding.narrative, style="italic dim"))

    for i, proof in enumerate(finding.proofs if verbose else finding.proofs[:1]):
        body.append(Text(""))
        label = "proof" if len(finding.proofs) == 1 else f"proof {i + 1}"
        body.append(Text(f"{label} · {proof.length} lineage hop(s)", style="bold dim"))
        body.append(Text(proof.render(indent="  "), style="cyan"))

    if finding.remediation:
        body.append(Text(""))
        body.append(Text("fix", style="bold dim"))
        body.append(Text(f"  {finding.remediation}"))

    return Panel(
        Group(*body),
        title=f"[{style}]{finding.severity.value}[/] · {finding.title}",
        subtitle=f"[dim]{finding.fingerprint}[/]",
        border_style=style,
        title_align="left",
        subtitle_align="right",
    )


def _print_footer(console: Console, result: ScanResult) -> None:
    duration = (
        f"{result.duration_seconds:.2f}s" if result.duration_seconds is not None else "—"
    )
    stats = result.stats.get("graph", {})
    origin = result.stats.get("graph_origin")
    provenance = f"{result.graph_source}"
    if origin and origin != result.graph_source:
        provenance += f" (captured from {origin})"
    console.print(
        Text(
            f"graph {provenance} · "
            f"{stats.get('entities', 0)} entities · "
            f"{stats.get('column_edges', 0)} column edges · "
            f"{len(result.detectors_run)} detectors · {duration}",
            style="dim",
        )
    )
