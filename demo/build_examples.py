"""Regenerate the machine-readable artefacts in examples/ from the bundled fixtures."""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(HERE))

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
        result, report_url="https://github.com/madhavvan/faultline/actions/runs/1"
    )
    (EX / "writeback-plan.txt").write_text(
        "faultline writeback --dry-run\n" + "=" * 78 + "\n\n" + plan.render() + "\n",
        encoding="utf-8",
    )
    for name in ("demo-graph.json", "demo-graph-clean.json", "demo-baseline.json"):
        (EX / name).write_text((DATA / name).read_text(encoding="utf-8"), encoding="utf-8")

    skew = _write_skew()

    print(f"{len(result.findings)} findings · {len(plan)} write-back changes · {skew}")


def _write_skew() -> str:
    """Measure the train/serve skew and write it out as UTF-8.

    This used to be a shell redirect, which is how ``examples/skew-measured.txt`` ended up
    encoded cp1252: on Windows the console codepage decides how the em-dash is written, and
    a lone 0x97 byte is not valid UTF-8, so GitHub renders it as a replacement character.
    Capturing the output here makes the artefact's encoding a property of this script rather
    than of whichever shell happened to run it.
    """
    import measure_skew

    if not measure_skew.WAREHOUSE.exists():
        return "skew skipped (run `make warehouse` first)"

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = measure_skew.main()
    if code != 0:
        return "skew measurement failed"

    (EX / "skew-measured.txt").write_text(buffer.getvalue(), encoding="utf-8")
    return "skew measured"


if __name__ == "__main__":
    main()
