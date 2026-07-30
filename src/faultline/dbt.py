"""Build a Faultline graph from a dbt project.

Column-level lineage here is **parsed from the compiled SQL**, not declared. Faultline runs
DataHub's own ``sqlglot_lineage`` -- the same parser DataHub's dbt ingestion uses -- so the
edges a detector traverses are the edges DataHub would have produced from the same models.
Nothing about the lineage is hand-authored to make the demo work.

dbt knows about tables and columns. It does not know about features, models or endpoints, so
the ML layer is declared separately in ``ml.yml``, standing in for the feature store and
model registry that would supply it in a real deployment.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import yaml

from .graph.entities import Entity, GraphSnapshot, LineageEdge, SchemaField
from .models import NodeKind

logger = logging.getLogger(__name__)

DEFAULT_PLATFORM = "duckdb"
DEFAULT_ENV = "PROD"

#: dbt column `meta` key naming the glossary terms Faultline should attach.
TERM_META_KEY = "faultline_terms"


class DbtProject:
    """Reads a built dbt project and renders it as a :class:`GraphSnapshot`."""

    def __init__(
        self,
        project_dir: str | Path,
        platform: str = DEFAULT_PLATFORM,
        env: str = DEFAULT_ENV,
    ) -> None:
        self.project_dir = Path(project_dir)
        self.target_dir = self.project_dir / "target"
        self.platform = platform
        self.env = env
        self._manifest: dict[str, Any] | None = None
        self._catalog: dict[str, Any] | None = None

    # -- artefacts -----------------------------------------------------------------------

    @property
    def manifest(self) -> dict[str, Any]:
        if self._manifest is None:
            path = self.target_dir / "manifest.json"
            if not path.exists():
                raise FileNotFoundError(
                    f"{path} not found. Run `dbt build` in {self.project_dir} first."
                )
            self._manifest = json.loads(path.read_text(encoding="utf-8"))
        return self._manifest

    @property
    def catalog(self) -> dict[str, Any]:
        if self._catalog is None:
            path = self.target_dir / "catalog.json"
            if not path.exists():
                raise FileNotFoundError(
                    f"{path} not found. Run `dbt docs generate` in {self.project_dir} first."
                )
            self._catalog = json.loads(path.read_text(encoding="utf-8"))
        return self._catalog

    # -- URNs ----------------------------------------------------------------------------

    def dataset_urn(self, node: dict[str, Any]) -> str:
        """Three-part ``database.schema.name``, matching what the SQL parser produces.

        This has to agree with ``sqlglot_lineage`` exactly. The parser resolves table
        references to three-part names, so a two-part URN here would leave every parsed
        upstream pointing at a node that does not exist in the graph -- traversal would stop
        at the first hop and the detectors would quietly see an empty pipeline.
        """
        import datahub.emitter.mce_builder as builder

        return builder.make_dataset_urn(self.platform, self.dataset_name(node), self.env)

    @staticmethod
    def dataset_name(node: dict[str, Any]) -> str:
        parts = [node.get("database"), node.get("schema"), node.get("name")]
        return ".".join(str(p) for p in parts if p)

    def column_urn(self, node: dict[str, Any], column: str) -> str:
        import datahub.emitter.mce_builder as builder

        return builder.make_schema_field_urn(self.dataset_urn(node), column)

    # -- loading -------------------------------------------------------------------------

    def load(self, ml_spec: str | Path | None = None) -> GraphSnapshot:
        """Render the project, optionally layering ML entities from ``ml_spec``."""
        nodes = self._materialised_nodes()
        entities = self._datasets(nodes)
        column_edges, dataset_edges = self._lineage(nodes, entities)

        snapshot = GraphSnapshot(
            entities=entities,
            column_edges=column_edges,
            dataset_edges=dataset_edges,
            source=f"dbt:{self.project_dir.name}",
        )

        if ml_spec:
            attach_ml_entities(snapshot, ml_spec, platform=self.platform, env=self.env)
        return snapshot

    def _materialised_nodes(self) -> dict[str, dict[str, Any]]:
        """Models and seeds -- the things that exist as tables and can carry lineage."""
        return {
            uid: node
            for uid, node in self.manifest["nodes"].items()
            if node.get("resource_type") in {"model", "seed"}
        }

    def _datasets(self, nodes: dict[str, dict[str, Any]]) -> dict[str, Entity]:
        catalog_nodes = {**self.catalog.get("nodes", {}), **self.catalog.get("sources", {})}
        entities: dict[str, Entity] = {}

        for uid, node in nodes.items():
            urn = self.dataset_urn(node)
            documented = node.get("columns") or {}
            catalogued = (catalog_nodes.get(uid) or {}).get("columns") or {}

            # The catalogue is authoritative for which columns exist and their types; the
            # manifest is authoritative for what they mean. A column present in only one is
            # still worth keeping -- an undocumented column can still carry a defect.
            names = list(catalogued) or list(documented)
            fields: list[SchemaField] = []
            for name in names:
                doc = documented.get(name) or {}
                cat = catalogued.get(name) or {}
                meta = doc.get("meta") or {}
                fields.append(
                    SchemaField(
                        field_path=name,
                        urn=self.column_urn(node, name),
                        native_type=cat.get("type"),
                        type_class=(cat.get("type") or "").split("(")[0] or None,
                        description=doc.get("description") or None,
                        terms=_term_urns(meta.get(TERM_META_KEY)),
                        tags=list(doc.get("tags") or []),
                    )
                )

            entities[urn] = Entity(
                urn=urn,
                kind=NodeKind.DATASET,
                name=f"{node['schema']}.{node['name']}",
                platform=self.platform,
                description=node.get("description") or None,
                custom_properties={
                    "dbt_unique_id": uid,
                    "dbt_resource_type": node.get("resource_type", ""),
                    "dbt_materialization": (node.get("config") or {}).get("materialized", ""),
                },
                fields=fields,
            )
        return entities

    def _lineage(
        self, nodes: dict[str, dict[str, Any]], entities: dict[str, Entity]
    ) -> tuple[list[LineageEdge], list[LineageEdge]]:
        from datahub.sql_parsing.schema_resolver import SchemaResolver
        from datahub.sql_parsing.sqlglot_lineage import sqlglot_lineage

        resolver = SchemaResolver(platform=self.platform, env=self.env)
        for urn, entity in entities.items():
            resolver.add_raw_schema_info(
                urn, {f.field_path: (f.native_type or "string") for f in entity.fields}
            )

        column_edges: list[LineageEdge] = []
        dataset_edges: list[LineageEdge] = []
        parsed = failed = 0

        for uid, node in nodes.items():
            downstream_urn = self.dataset_urn(node)

            for parent_uid in (node.get("depends_on") or {}).get("nodes", []):
                parent = nodes.get(parent_uid)
                if parent:
                    dataset_edges.append(
                        LineageEdge(
                            upstream=self.dataset_urn(parent), downstream=downstream_urn
                        )
                    )

            sql = node.get("compiled_code")
            if not sql:
                continue  # seeds have no SQL; their columns are roots

            try:
                result = sqlglot_lineage(
                    sql,
                    schema_resolver=resolver,
                    default_db=node.get("database"),
                    default_schema=node.get("schema"),
                )
            except Exception as exc:
                logger.warning("could not parse lineage for %s: %s", uid, exc)
                failed += 1
                continue

            if result.debug_info and result.debug_info.error:
                logger.warning("lineage parse error for %s: %s", uid, result.debug_info.error)
                failed += 1
                continue

            parsed += 1
            for info in result.column_lineage or []:
                # dbt compiles each model to a bare SELECT, so the parser has no INSERT
                # target to name. The downstream table is the model we are parsing.
                downstream_col = info.downstream.column
                if not downstream_col:
                    continue
                target = self.column_urn(node, downstream_col)
                for ref in info.upstreams:
                    column_edges.append(
                        LineageEdge(
                            upstream=_column_ref_urn(ref),
                            downstream=target,
                            transform=_describe_transform(info),
                            confidence=1.0,
                        )
                    )

        logger.info("parsed lineage for %d models (%d failed)", parsed, failed)
        return _dedupe(column_edges), _dedupe(dataset_edges)


# --------------------------------------------------------------------------------------
# ML layer
# --------------------------------------------------------------------------------------


def attach_ml_entities(
    snapshot: GraphSnapshot,
    spec_path: str | Path,
    platform: str = DEFAULT_PLATFORM,
    env: str = DEFAULT_ENV,
) -> GraphSnapshot:
    """Layer feature tables, models and deployments onto a dbt-derived snapshot.

    A feature whose backing column does not exist in the warehouse is skipped. That is what
    makes the clean and faulty worlds stay consistent without maintaining two specs: when
    ``dbt build --vars '{clean: true}'`` omits ``segment_churn_rate``, the feature that would
    have consumed it simply is not created.
    """
    import datahub.emitter.mce_builder as builder

    spec = yaml.safe_load(Path(spec_path).read_text(encoding="utf-8")) or {}
    feature_platform = spec.get("feature_platform", "feast")
    model_platform = spec.get("model_platform", "mlflow")
    deploy_platform = spec.get("deployment_platform", "sagemaker")

    columns_of: dict[str, set[str]] = {
        urn: {f.field_path for f in entity.fields}
        for urn, entity in snapshot.entities.items()
    }

    def dataset_urn(name: str) -> str:
        """Resolve a spec's table name to a warehouse dataset.

        Accepts the fully-qualified ``database.schema.table`` and also a shorter suffix, so
        the spec stays readable and does not have to hard-code the dbt target database.
        """
        exact = builder.make_dataset_urn(platform, name, env)
        if exact in snapshot.entities:
            return exact
        suffix = f".{name}"
        matches = [
            urn
            for urn, entity in snapshot.entities.items()
            if entity.kind is NodeKind.DATASET
            and (entity.name == name or (entity.name or "").endswith(suffix))
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.warning("ambiguous feature source %r matches %d datasets", name, len(matches))
        return exact

    table_features: dict[str, list[str]] = {}

    for table in spec.get("feature_tables", []):
        table_name = table["name"]
        source_urn = dataset_urn(table["source"])
        available = columns_of.get(source_urn, set())
        if not available:
            logger.warning("feature table %s: source %s not in warehouse", table_name, table["source"])

        created: list[str] = []
        for feature_name in table.get("features", []):
            if feature_name not in available:
                logger.info(
                    "skipping feature %s.%s: no such column in %s",
                    table_name, feature_name, table["source"],
                )
                continue
            urn = builder.make_ml_feature_urn(table_name, feature_name)
            snapshot.entities[urn] = Entity(
                urn=urn,
                kind=NodeKind.ML_FEATURE,
                name=feature_name,
                description=f"{table.get('serving', 'offline').title()} copy of {feature_name}",
                feature_sources=[source_urn],
                custom_properties={"faultline.serving": table.get("serving", "offline")},
            )
            created.append(urn)

        table_features[table_name] = created
        table_urn = builder.make_ml_feature_table_urn(feature_platform, table_name)
        snapshot.entities[table_urn] = Entity(
            urn=table_urn,
            kind=NodeKind.ML_FEATURE_TABLE,
            name=table_name,
            platform=feature_platform,
            description=table.get("description"),
            ml_features=created,
        )

    for model in spec.get("models", []):
        deployments: list[str] = []
        if model.get("deployment"):
            dep_urn = builder.make_ml_model_deployment_urn(
                deploy_platform, model["deployment"], env
            )
            snapshot.entities[dep_urn] = Entity(
                urn=dep_urn,
                kind=NodeKind.ML_MODEL_DEPLOYMENT,
                name=model["deployment"],
                platform=deploy_platform,
                description=model.get("deployment_description"),
            )
            deployments.append(dep_urn)

        props = {"env": env}
        if model.get("label_column"):
            props["faultline.label_column"] = model["label_column"]
        if model.get("owner"):
            props["owner"] = model["owner"]

        model_urn = builder.make_ml_model_urn(model_platform, model["name"], env)
        snapshot.entities[model_urn] = Entity(
            urn=model_urn,
            kind=NodeKind.ML_MODEL,
            name=model["name"],
            platform=model_platform,
            description=model.get("description"),
            ml_features=table_features.get(model.get("feature_table", ""), []),
            deployments=deployments,
            custom_properties=props,
            training_metrics={k: str(v) for k, v in (model.get("training_metrics") or {}).items()},
            online_metrics={k: str(v) for k, v in (model.get("online_metrics") or {}).items()},
        )

    return snapshot


# --------------------------------------------------------------------------------------


def _column_ref_urn(ref: Any) -> str:
    import datahub.emitter.mce_builder as builder

    return builder.make_schema_field_urn(ref.table, ref.column)


def _describe_transform(info: Any, dialect: str = "duckdb") -> str | None:
    """A comparable, readable label for what the SQL does to a value.

    Two normalisations, both required for skew detection to work on real SQL:

    * ``is_direct_copy`` from the parser becomes ``identity``. The skew detector treats
      value-preserving hops as no-ops, so a column passed through a staging layer does not
      register as a difference in derivation.
    * **Table qualifiers are stripped.** The offline path aggregates
      ``AVG("stg_tickets"."resolution_hours")`` while the online path writes the same
      aggregate over a CTE alias. They are the same operation; compared as raw text they
      differ, and every correctly-built feature pair would be reported as diverged. Removing
      the qualifier leaves the operation, which is the thing that actually matters.

    What survives normalisation is genuine: a 30-day window and a 7-day window compile to
    different date literals, and those differences remain visible.
    """
    logic = getattr(info, "logic", None)
    if logic is None:
        return None

    if getattr(logic, "is_direct_copy", False):
        return "identity"

    raw = getattr(logic, "column_logic", None) or str(logic)
    try:
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one(raw, dialect=dialect)
        if isinstance(tree, exp.Alias):
            tree = tree.this
        for column in tree.find_all(exp.Column):
            column.set("table", None)
        text = tree.sql(dialect=dialect, identify=False)
    except Exception:
        # An expression sqlglot cannot re-parse still describes the hop; keep it readable
        # rather than dropping the transform entirely.
        text = raw

    text = " ".join(str(text).replace('"', "").split())
    if not text:
        return None
    return text if len(text) <= 100 else text[:97] + "..."


def _dedupe(edges: list[LineageEdge]) -> list[LineageEdge]:
    best: dict[tuple[str, str, str | None], LineageEdge] = {}
    for edge in edges:
        key = (edge.upstream, edge.downstream, edge.transform)
        if key not in best or edge.confidence > best[key].confidence:
            best[key] = edge
    return sorted(best.values(), key=lambda e: (e.upstream, e.downstream, e.transform or ""))


def _term_urns(values: Any) -> list[str]:
    import datahub.emitter.mce_builder as builder

    if not values:
        return []
    if isinstance(values, str):
        values = [values]
    return [
        v if str(v).startswith("urn:li:glossaryTerm:") else builder.make_term_urn(str(v))
        for v in values
    ]
