"""Regenerate the machine-readable artefacts in examples/ from the bundled fixtures."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from faultline.config import FaultlineConfig  # noqa: E402
from faultline.detectors import SemanticBaseline  # noqa: E402
from faultline.engine import scan  # noqa: E402
from faultline.graph.graph import MetadataGraph  # noqa: E402
from faultline.graph.loader import load_snapshot  # noqa: E402
from faultline.writeback import DataHubWriter  # noqa: E402

DATA = REPO / "src" / "faultline" / "data"
EX = REPO / "examples"


def main() -> None:
    EX.mkdir(exist_ok=True)
    graph = MetadataGraph(load_snapshot(DATA / "demo-graph.json"))
    baseline = SemanticBaseline.model_validate_json(
        (DATA / "demo-baseline.json").read_text(encoding="utf-8")
    )
    result = scan(graph, FaultlineConfig(), baseline)

    (EX / "scan-result.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")

    plan = DataHubWriter().plan(
        result, report_url="https://github.com/faultline-dev/faultline/actions/runs/1"
    )
    (EX / "writeback-plan.txt").write_text(
        "faultline writeback --dry-run\n" + "=" * 78 + "\n\n" + plan.render() + "\n",
        encoding="utf-8",
    )
    for name in ("demo-graph.json", "demo-graph-clean.json", "demo-baseline.json"):
        (EX / name).write_text((DATA / name).read_text(encoding="utf-8"), encoding="utf-8")

    print(f"{len(result.findings)} findings · {len(plan)} write-back changes")


if __name__ == "__main__":
    main()
