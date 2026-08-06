"""What `faultline check` tells CI, which is entirely its exit code.

Three different facts share one channel: 0 means nothing blocking was proved, 1 means a
blocking finding exists, and anything else means the scan never ran. Collapsing the third
into the second is how a tool ends up announcing a production defect it did not find --
precisely the failure this project exists to prevent, committed by its own gate.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from faultline import demo_world
from faultline.cli import app
from faultline.graph.loader import save_snapshot

runner = CliRunner()


@pytest.fixture
def clean_fixture(tmp_path):
    """The pipeline before the four changes: a graph with nothing to find."""
    return save_snapshot(demo_world.clean_world(), tmp_path / "clean.json")


@pytest.fixture
def faulty_fixture(tmp_path):
    return save_snapshot(demo_world.faulty_world(), tmp_path / "faulty.json")


def test_empty_baseline_is_not_reported_as_a_finding(clean_fixture, tmp_path) -> None:
    """CI restores the baseline with ``git show origin/main:... > baseline.json``, which
    leaves an *empty* file when main does not carry one yet. Parsing that as a baseline
    raises, exits 1, and the workflow reads exit 1 as "a structural risk was proved"."""
    baseline = tmp_path / "baseline.json"
    baseline.write_text("", encoding="utf-8")

    result = runner.invoke(
        app, ["check", "--fixture", str(clean_fixture), "--baseline", str(baseline)]
    )

    assert result.exit_code == 0, result.output
    assert "skipping change detection" in result.output


def test_unparseable_baseline_is_not_reported_as_a_finding(clean_fixture, tmp_path) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text("not json at all", encoding="utf-8")

    result = runner.invoke(
        app, ["check", "--fixture", str(clean_fixture), "--baseline", str(baseline)]
    )

    assert result.exit_code == 0, result.output


def test_a_real_blocking_finding_still_exits_one(faulty_fixture) -> None:
    """The degradation above must not have muted the signal it degrades around."""
    result = runner.invoke(app, ["check", "--fixture", str(faulty_fixture), "--fail-on", "HIGH"])

    assert result.exit_code == 1, result.output


def test_a_missing_graph_exits_two_not_one(tmp_path) -> None:
    """Exit 2 means "nothing was scanned". CI must be able to tell it from exit 1."""
    result = runner.invoke(app, ["check", "--fixture", str(tmp_path)])

    assert result.exit_code == 2, result.output
