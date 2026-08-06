"""Severity is an ordering, and it has to behave like one everywhere.

``Severity`` subclasses ``str``. Any comparison operator left undefined therefore falls
through to alphabetical string comparison, under which ``INFO > CRITICAL`` is true and
``max()`` over a model's findings returns ``HIGH`` for a model carrying two ``CRITICAL``s.
That wrong answer was being written into DataHub as the model's recorded risk: plausible,
silent, and never surfaced by anything -- the precise failure mode Faultline exists to catch.
"""

from __future__ import annotations

import pytest

from faultline.config import FaultlineConfig
from faultline.models import Finding, FindingKind, ScanResult, Severity
from faultline.writeback import DataHubWriter

LADDER = [Severity.INFO, Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]


@pytest.mark.parametrize("lower,higher", list(zip(LADDER, LADDER[1:], strict=False)))
def test_every_adjacent_pair_orders_by_rank(lower: Severity, higher: Severity) -> None:
    assert lower < higher
    assert lower <= higher
    assert higher > lower
    assert higher >= lower
    assert not higher < lower
    assert not lower > higher


def test_max_and_min_pick_by_rank_not_alphabetically() -> None:
    """The bug in one line: sorted alphabetically, "HIGH" beats "CRITICAL"."""
    assert max([Severity.HIGH, Severity.CRITICAL]) is Severity.CRITICAL
    assert min([Severity.HIGH, Severity.CRITICAL]) is Severity.HIGH
    assert max(LADDER) is Severity.CRITICAL
    assert min(LADDER) is Severity.INFO


def test_severity_still_compares_equal_to_its_own_string() -> None:
    """Config files and JSON round-trips depend on the str behaviour we did not override."""
    assert Severity.HIGH == "HIGH"
    assert Severity("CRITICAL") is Severity.CRITICAL


def _finding(severity: Severity) -> Finding:
    return Finding(
        kind=FindingKind.TARGET_LEAKAGE,
        severity=severity,
        title=f"{severity.value} finding",
        summary="",
        model_urn="urn:li:mlModel:(urn:li:dataPlatform:mlflow,m,PROD)",
    )


def test_writeback_records_the_worst_severity_it_proved() -> None:
    """What DataHub ends up carrying is the whole point of getting the ordering right."""
    model_urn = "urn:li:mlModel:(urn:li:dataPlatform:mlflow,m,PROD)"
    result = ScanResult(
        findings=[_finding(Severity.CRITICAL), _finding(Severity.HIGH)],
        scanned_models=[model_urn],
    )

    plan = DataHubWriter(FaultlineConfig()).plan(result)
    prop = next(c for c in plan.changes if c.kind == "structured-property")

    assert prop.detail == "risk=CRITICAL finding_count=2"
    assert prop.payload["io.faultline.risk"] == ["CRITICAL"]
