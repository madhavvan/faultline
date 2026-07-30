"""Capture the built dbt project as the fixtures `faultline demo` replays.

Run after `make warehouse`. The committed fixtures are therefore a recording of a real dbt
run — column lineage parsed from compiled SQL by DataHub's own parser — which is what lets
anyone evaluate Faultline in a second without installing dbt, DuckDB or DataHub.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from faultline.dbt import DbtProject  # noqa: E402
from faultline.detectors import SemanticBaseline  # noqa: E402
from faultline.graph.graph import MetadataGraph  # noqa: E402
from faultline.graph.loader import save_snapshot  # noqa: E402

WAREHOUSE = REPO / "demo" / "warehouse"
ML_SPEC = REPO / "demo" / "ml.yml"
DATA = REPO / "src" / "faultline" / "data"


def main() -> int:
    if not (WAREHOUSE / "target" / "catalog.json").exists():
        print("dbt artefacts missing — run `make warehouse` first.", file=sys.stderr)
        return 1

    DATA.mkdir(parents=True, exist_ok=True)

    faulty = DbtProject(WAREHOUSE).load(ml_spec=ML_SPEC)
    faulty.source = "dbt:faultline_demo"
    save_snapshot(faulty, DATA / "demo-graph.json")

    clean_project = DbtProject(WAREHOUSE)
    clean_project.target_dir = WAREHOUSE / "target-clean"
    clean = clean_project.load(ml_spec=ML_SPEC)
    clean.source = "dbt:faultline_demo(clean)"
    save_snapshot(clean, DATA / "demo-graph-clean.json")

    baseline = SemanticBaseline.capture(MetadataGraph(clean))
    (DATA / "demo-baseline.json").write_text(
        baseline.model_dump_json(indent=2), encoding="utf-8"
    )

    print(f"faulty  : {faulty.stats()}")
    print(f"clean   : {clean.stats()}")
    print(f"baseline: {len(baseline.columns)} column signatures")
    print(f"written to {DATA}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
