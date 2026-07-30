"""Faultline command line.

``faultline demo`` is the zero-setup entry point: it builds the bundled pipeline, captures a
baseline of it as it was believed to be, applies four ordinary changes, and proves what they
did to the production model. No Docker, no DataHub, about a second.
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

import typer
from rich.table import Table

from . import __version__
from .config import FaultlineConfig
from .detectors import REGISTRY, SemanticBaseline, active_detectors
from .engine import scan as run_scan
from .graph.graph import MetadataGraph
from .graph.loader import DataHubLoader, load_snapshot, save_snapshot
from .models import Severity
from .report import print_result, render_pr_comment, render_report
from .report.terminal import default_console


def _force_utf8() -> None:
    """Windows consoles default to a legacy codepage that cannot encode the report glyphs.

    Without this the whole run dies on a UnicodeEncodeError partway through printing, which
    is a miserable first impression for anyone evaluating the tool on Windows.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        # A detached or already-wrapped stream cannot be reconfigured; that is fine.
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")


_force_utf8()

app = typer.Typer(
    name="faultline",
    help="Structural ML failure detection on the DataHub metadata graph.",
    add_completion=False,
    no_args_is_help=True,
)
console = default_console()


def _version(value: bool) -> None:
    if value:
        console.print(f"faultline {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", callback=_version, is_eager=True, help="Show version and exit."
    ),
) -> None:
    """Faultline: your model didn't drift, your graph did."""


# --------------------------------------------------------------------------------------


@app.command()
def demo(
    explain: bool = typer.Option(False, "--explain", help="Describe the pipeline and its faults."),
    write_report: Path | None = typer.Option(
        None, "--report", help="Also write a markdown report to this path."
    ),
    write_comment: Path | None = typer.Option(
        None, "--pr-comment", help="Also write the PR-comment markdown to this path."
    ),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Show every proof path."),
) -> None:
    """Run Faultline against the bundled demo pipeline. No DataHub required."""
    from . import demo_world

    graph, baseline = _demo_graph()

    if explain:
        # Described from the graph actually being scanned, so the summary cannot drift away
        # from what the run then reports.
        datasets = graph.datasets()
        columns = sum(len(d.fields) for d in datasets)
        models = graph.models()
        deployments = [
            e.name for m in models for d in m.deployments if (e := graph.entity(d))
        ]
        console.print()
        console.print("[bold]Demo pipeline[/]")
        console.print(
            f"  {len(datasets)} tables · {columns} columns · "
            f"{len(graph.snapshot.column_edges)} column-level lineage edges"
        )
        if graph.snapshot.origin:
            console.print(
                f"  lineage parsed from compiled SQL · [dim]{graph.snapshot.origin}[/]"
            )
        for model in models:
            served = f" served at [cyan]{deployments[0]}[/]" if deployments else ""
            console.print(f"  model [cyan]{model.name}[/]{served}")
        console.print()
        console.print("[bold]Injected faults[/]")
        for name, description in demo_world.FAULTS.items():
            console.print(f"  [yellow]{name:<12}[/] {description}")
        console.print()

    result = run_scan(graph, FaultlineConfig(), baseline)

    print_result(result, console, verbose=verbose)

    if write_report:
        write_report.parent.mkdir(parents=True, exist_ok=True)
        write_report.write_text(render_report(result), encoding="utf-8")
        console.print(f"\n[dim]report written to {write_report}[/]")
    if write_comment:
        write_comment.parent.mkdir(parents=True, exist_ok=True)
        write_comment.write_text(render_pr_comment(result), encoding="utf-8")
        console.print(f"[dim]pr comment written to {write_comment}[/]")


@app.command()
def scan(
    fixture: Path | None = typer.Option(
        None, "--fixture", help="Scan a captured snapshot instead of a live instance."
    ),
    config_path: Path | None = typer.Option(None, "--config", help="Path to .faultline.yml."),
    baseline_path: Path | None = typer.Option(
        None, "--baseline", help="Baseline for silent-change detection."
    ),
    json_out: Path | None = typer.Option(None, "--json", help="Write the raw result as JSON."),
    report_out: Path | None = typer.Option(None, "--report", help="Write a markdown report."),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Show every proof path."),
) -> None:
    """Scan a DataHub instance (or a captured fixture) for structural ML defects."""
    config = FaultlineConfig.load(config_path)
    graph = _load_graph(config, fixture)
    baseline = _load_baseline(baseline_path)

    result = run_scan(graph, config, baseline)
    print_result(result, console, verbose=verbose)

    if json_out:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        console.print(f"\n[dim]json written to {json_out}[/]")
    if report_out:
        report_out.parent.mkdir(parents=True, exist_ok=True)
        report_out.write_text(render_report(result), encoding="utf-8")
        console.print(f"[dim]report written to {report_out}[/]")


@app.command()
def check(
    fixture: Path | None = typer.Option(None, "--fixture", help="Scan a captured snapshot."),
    config_path: Path | None = typer.Option(None, "--config", help="Path to .faultline.yml."),
    baseline_path: Path | None = typer.Option(None, "--baseline", help="Semantic baseline."),
    fail_on: Severity | None = typer.Option(
        None, "--fail-on", help="Minimum severity that fails the run."
    ),
    comment_out: Path | None = typer.Option(
        None, "--comment", help="Write the PR-comment markdown here."
    ),
) -> None:
    """CI gate. Exits non-zero when a new blocking finding is present."""
    config = FaultlineConfig.load(config_path)
    threshold = fail_on or config.ci.fail_on

    graph = _load_graph(config, fixture)
    result = run_scan(graph, config, _load_baseline(baseline_path))

    known: set[str] = set()
    known_path = Path(config.ci.baseline_path) if config.ci.baseline_path else None
    if config.ci.fail_on_new_only and known_path and known_path.exists():
        known = set(json.loads(known_path.read_text(encoding="utf-8")).get("fingerprints", []))

    candidates = result.new_since(known) if config.ci.fail_on_new_only else result.findings
    blocking = [f for f in candidates if f.severity.rank >= threshold.rank]

    print_result(result, console)

    if comment_out:
        comment_out.parent.mkdir(parents=True, exist_ok=True)
        comment_out.write_text(render_pr_comment(result, candidates), encoding="utf-8")

    if blocking:
        console.print(
            f"\n[bold red]FAIL[/] {len(blocking)} new finding(s) at or above "
            f"{threshold.value}."
        )
        raise typer.Exit(code=1)

    suppressed = len(result.findings) - len(candidates)
    note = f" ({suppressed} pre-existing finding(s) baselined)" if suppressed else ""
    console.print(f"\n[bold green]PASS[/] no new findings at or above {threshold.value}{note}.")


@app.command("accept")
def accept_baseline(
    fixture: Path | None = typer.Option(None, "--fixture", help="Scan a captured snapshot."),
    config_path: Path | None = typer.Option(None, "--config", help="Path to .faultline.yml."),
    out: Path | None = typer.Option(None, "--out", help="Where to write the CI baseline."),
) -> None:
    """Record current findings as accepted, so CI only gates on new ones."""
    config = FaultlineConfig.load(config_path)
    result = run_scan(_load_graph(config, fixture), config)
    target = out or Path(config.ci.baseline_path or ".faultline-baseline.json")

    target.write_text(
        json.dumps(
            {
                "fingerprints": sorted(result.fingerprints()),
                "captured_at": result.started_at.isoformat(),
                "note": "Findings accepted as known. Delete a fingerprint to re-gate on it.",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    console.print(f"accepted {len(result.findings)} finding(s) into {target}")


@app.command()
def baseline(
    out: Path = typer.Argument(..., help="Where to write the semantic baseline."),
    fixture: Path | None = typer.Option(None, "--fixture", help="Capture from a snapshot."),
    config_path: Path | None = typer.Option(None, "--config", help="Path to .faultline.yml."),
) -> None:
    """Capture column semantics, so a later scan can prove what changed."""
    config = FaultlineConfig.load(config_path)
    graph = _load_graph(config, fixture)
    captured = SemanticBaseline.capture(graph, config.datahub.resolved_server())

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(captured.model_dump_json(indent=2), encoding="utf-8")
    console.print(f"captured {len(captured.columns)} column signatures to {out}")


@app.command()
def capture(
    out: Path = typer.Argument(..., help="Where to write the snapshot."),
    config_path: Path | None = typer.Option(None, "--config", help="Path to .faultline.yml."),
    limit: int | None = typer.Option(None, "--limit", help="Cap datasets loaded."),
) -> None:
    """Record a live DataHub graph to disk as a replay fixture."""
    config = FaultlineConfig.load(config_path)
    with console.status(f"loading graph from {config.datahub.resolved_server()}"):
        snapshot = DataHubLoader(config.datahub).load(dataset_limit=limit)
    path = save_snapshot(snapshot, out)
    stats = snapshot.stats()
    console.print(f"captured {stats['entities']} entities to {path}")
    console.print(f"  {stats['column_edges']} column edges · {stats['dataset_edges']} table edges")


@app.command("demo-fixture")
def demo_fixture(
    out: Path = typer.Argument(..., help="Where to write the demo snapshot."),
    clean: bool = typer.Option(False, "--clean", help="Write the pre-change (clean) world."),
) -> None:
    """Write the bundled demo world to disk as a fixture."""
    from . import demo_world

    snapshot = demo_world.clean_world() if clean else demo_world.faulty_world()
    console.print(f"wrote {snapshot.stats()['entities']} entities to {save_snapshot(snapshot, out)}")


@app.command()
def triage(
    fixture: Path | None = typer.Option(None, "--fixture", help="Scan a captured snapshot."),
    demo_pipeline: bool = typer.Option(
        False, "--demo", help="Triage the bundled demo pipeline."
    ),
    config_path: Path | None = typer.Option(None, "--config", help="Path to .faultline.yml."),
    baseline_path: Path | None = typer.Option(None, "--baseline", help="Semantic baseline."),
    report_out: Path | None = typer.Option(None, "--report", help="Write a markdown report."),
) -> None:
    """Run the Claude triage agent over proven findings, reading DataHub via its MCP server.

    The agent explains consequence and may adjust severity by one step with a reason. It
    cannot create a finding: every structural claim comes from the detectors.
    """
    from .agent import TriageAgent

    config = FaultlineConfig.load(config_path)

    if demo_pipeline:
        from . import demo_world

        baseline = SemanticBaseline.capture(MetadataGraph(demo_world.clean_world()))
        graph = MetadataGraph(demo_world.faulty_world())
    else:
        graph = _load_graph(config, fixture)
        baseline = _load_baseline(baseline_path)

    result = run_scan(graph, config, baseline)
    if not result.findings:
        console.print("[green]nothing to triage — no findings.[/]")
        return

    console.print(
        f"[dim]triaging {len(result.findings)} finding(s) with "
        f"{config.agent.model} (effort={config.agent.effort})[/]"
    )
    with console.status("agent working"):
        report = TriageAgent(config).run(result, graph)

    if report.mcp_error:
        console.print(
            f"[yellow]DataHub MCP server unavailable[/] ({report.mcp_error}) — "
            "triaged on proofs alone, without catalogue context."
        )
    else:
        console.print(f"[dim]MCP tools available: {', '.join(report.mcp_tools)}[/]")

    if report.refused:
        console.print(f"[yellow]{report.summary}[/]")

    print_result(result, console)

    if report.summary and not report.refused:
        console.print()
        console.print(f"[bold]Agent summary[/]\n{report.summary}")

    console.print()
    console.print(
        f"[dim]{report.assessed}/{report.total} assessed · "
        f"{report.input_tokens:,} in / {report.output_tokens:,} out · "
        f"~${report.estimated_cost_usd:.3f}[/]"
    )

    if report_out:
        report_out.parent.mkdir(parents=True, exist_ok=True)
        report_out.write_text(render_report(result), encoding="utf-8")
        console.print(f"[dim]report written to {report_out}[/]")


@app.command()
def writeback(
    fixture: Path | None = typer.Option(None, "--fixture", help="Scan a captured snapshot."),
    config_path: Path | None = typer.Option(None, "--config", help="Path to .faultline.yml."),
    baseline_path: Path | None = typer.Option(None, "--baseline", help="Semantic baseline."),
    report_url: str | None = typer.Option(None, "--report-url", help="Link to attach."),
    dry_run: bool = typer.Option(
        True, "--dry-run/--apply", help="Print the plan (default) or actually write it."
    ),
) -> None:
    """Contribute findings back to DataHub as tags, properties, incidents and an audit run."""
    from .writeback import DataHubWriter

    config = FaultlineConfig.load(config_path)
    result = run_scan(_load_graph(config, fixture), config, _load_baseline(baseline_path))

    writer = DataHubWriter(config)
    plan = writer.plan(result, report_url=report_url)

    console.print()
    console.print(f"[bold]Write-back plan[/] · {len(plan)} change(s)")
    console.print()
    console.print(plan.render())
    console.print()

    if dry_run:
        console.print(
            "[dim]dry run — nothing was written. Re-run with --apply to contribute "
            "these to DataHub.[/]"
        )
        return

    with console.status("writing to DataHub"):
        report = writer.apply(plan, dry_run=False)

    console.print(f"[green]applied {report.applied}[/] · failed {report.failed}")
    for error in report.errors:
        console.print(f"  [red]{error}[/]")
    if report.failed:
        raise typer.Exit(code=1)


@app.command()
def emit(
    fixture: Path | None = typer.Option(
        None, "--fixture", help="Snapshot or dbt project to emit. Defaults to the demo."
    ),
    config_path: Path | None = typer.Option(None, "--config", help="Path to .faultline.yml."),
) -> None:
    """Push a graph into a live DataHub instance.

    Emits the same snapshot the offline demo replays, so the live instance and the bundled
    fixtures are demonstrably one graph rather than two descriptions of one.
    """
    from .emit import SnapshotEmitter

    config = FaultlineConfig.load(config_path)

    if fixture:
        graph = _load_graph(config, fixture)
    else:
        graph, _ = _demo_graph()

    stats = graph.stats()
    console.print(
        f"emitting [bold]{stats['entities']}[/] entities · "
        f"{stats['column_edges']} column edges → "
        f"[cyan]{config.datahub.resolved_server()}[/]"
    )

    with console.status("emitting"):
        report = SnapshotEmitter(config.datahub).emit(graph.snapshot)

    console.print(f"[green]{report.summary()}[/]")
    for failure in report.failures[:10]:
        console.print(f"  [red]{failure}[/]")
    if report.failures:
        raise typer.Exit(code=1)

    console.print(
        f"\n[dim]open {config.datahub.resolved_server().replace(':8080', ':9002')} "
        "to browse the graph[/]"
    )


@app.command()
def detectors() -> None:
    """List the registered detectors."""
    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("detector")
    table.add_column("proves")
    for name in sorted(REGISTRY):
        detector = REGISTRY[name]
        table.add_row(name, detector.description)
    console.print()
    console.print(table)
    console.print()


@app.command()
def doctor(
    config_path: Path | None = typer.Option(None, "--config", help="Path to .faultline.yml."),
) -> None:
    """Check connectivity to DataHub and report what it holds."""
    config = FaultlineConfig.load(config_path)
    console.print(f"connecting to [cyan]{config.datahub.resolved_server()}[/] ...")
    try:
        counts = DataHubLoader(config.datahub).check()
    except Exception as exc:
        console.print(f"[bold red]cannot reach DataHub[/]: {exc}")
        raise typer.Exit(code=1) from exc

    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("entity type")
    table.add_column("count", justify="right")
    for key, value in counts.items():
        if key == "server":
            continue
        table.add_row(key, str(value))
    console.print(table)
    console.print(f"\n[dim]{len(active_detectors(config.detectors))} detectors enabled[/]")


# --------------------------------------------------------------------------------------


def _demo_graph() -> tuple[MetadataGraph, SemanticBaseline | None]:
    """The bundled demo: a recording of a real dbt run, with a synthetic fallback.

    The shipped fixtures are captured from ``demo/warehouse``, so the lineage the demo shows
    was parsed from compiled SQL rather than declared. If they are missing -- a checkout
    that has not run ``make warehouse`` -- fall back to the equivalent hand-built pipeline
    so ``faultline demo`` still works out of the box.
    """
    data = Path(__file__).parent / "data"
    graph_file = data / "demo-graph.json"
    baseline_file = data / "demo-baseline.json"

    if graph_file.exists():
        graph = MetadataGraph(load_snapshot(graph_file))
        baseline = (
            SemanticBaseline.model_validate_json(baseline_file.read_text(encoding="utf-8"))
            if baseline_file.exists()
            else None
        )
        return graph, baseline

    from . import demo_world

    console.print("[dim]bundled dbt fixtures not found — using the built-in pipeline[/]")
    return (
        MetadataGraph(demo_world.faulty_world()),
        SemanticBaseline.capture(MetadataGraph(demo_world.clean_world())),
    )


def _load_dbt(project_dir: Path) -> MetadataGraph:
    """Load a dbt project, discovering its ML spec by convention."""
    from .dbt import DbtProject

    candidates = [
        project_dir / "ml.yml",
        project_dir.parent / "ml.yml",
        project_dir / "ml.yaml",
        project_dir.parent / "ml.yaml",
    ]
    ml_spec = next((p for p in candidates if p.exists()), None)
    if ml_spec:
        console.print(f"[dim]dbt project {project_dir} · ml spec {ml_spec}[/]")
    else:
        console.print(
            f"[yellow]no ml.yml found near {project_dir}[/] — warehouse lineage only, "
            "so ML detectors will have nothing to check."
        )
    return MetadataGraph(DbtProject(project_dir).load(ml_spec=ml_spec))


def _load_graph(config: FaultlineConfig, fixture: Path | None) -> MetadataGraph:
    if fixture:
        # A directory is a dbt project; a file is a captured snapshot. Auto-detecting keeps
        # one flag instead of two that mean almost the same thing.
        if fixture.is_dir():
            if not (fixture / "target" / "manifest.json").exists():
                console.print(
                    f"[bold red]{fixture} is a directory but has no target/manifest.json[/]\n"
                    "[dim]Run `dbt build && dbt docs generate` in it first.[/]"
                )
                raise typer.Exit(code=2)
            return _load_dbt(fixture)
        return MetadataGraph(load_snapshot(fixture))
    try:
        return MetadataGraph(DataHubLoader(config.datahub).load())
    except Exception as exc:
        console.print(
            f"[bold red]could not load the graph from "
            f"{config.datahub.resolved_server()}[/]: {exc}\n"
            "[dim]Pass --fixture to scan a captured snapshot, or run "
            "`faultline demo` for the bundled pipeline.[/]"
        )
        raise typer.Exit(code=2) from exc


def _load_baseline(path: Path | None) -> SemanticBaseline | None:
    if not path:
        return None
    if not path.exists():
        console.print(f"[yellow]baseline {path} not found; skipping change detection[/]")
        return None
    return SemanticBaseline.model_validate_json(path.read_text(encoding="utf-8"))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
