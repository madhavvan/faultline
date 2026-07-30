"""Load a :class:`GraphSnapshot` from a live DataHub instance, or from a captured fixture.

Both paths produce the identical object, which is what makes the offline replay mode honest:
the fixtures are recorded from a real instance, and detectors cannot tell the difference
because there is none.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import DataHubConfig
from ..models import NodeKind
from .entities import Entity, GraphSnapshot, LineageEdge, SchemaField
from .graph import MetadataGraph

logger = logging.getLogger(__name__)

DATASET_ENTITY = "dataset"
ML_ENTITY_TYPES = (
    "mlModel",
    "mlModelGroup",
    "mlFeature",
    "mlFeatureTable",
    "mlPrimaryKey",
    "mlModelDeployment",
)


class DataHubLoader:
    """Reads the subset of DataHub's aspect model that structural detection needs."""

    def __init__(self, config: DataHubConfig | None = None) -> None:
        self.config = config or DataHubConfig()
        self._graph = None

    # -- connection ----------------------------------------------------------------------

    def connect(self):
        """Open (and memoise) a DataHub client."""
        if self._graph is not None:
            return self._graph

        from datahub.ingestion.graph.client import DatahubClientConfig, DataHubGraph

        self._graph = DataHubGraph(
            DatahubClientConfig(
                server=self.config.resolved_server(),
                token=self.config.resolved_token(),
                timeout_sec=int(self.config.timeout_seconds),
                retry_max_times=self.config.max_retries,
                disable_ssl_verification=not self.config.verify_ssl,
            )
        )
        return self._graph

    def check(self) -> dict[str, Any]:
        """Verify connectivity and report what the instance holds."""
        graph = self.connect()
        counts: dict[str, Any] = {"server": self.config.resolved_server()}
        for entity_type in (DATASET_ENTITY, *ML_ENTITY_TYPES):
            try:
                counts[entity_type] = sum(
                    1 for _ in graph.get_urns_by_filter(entity_types=[entity_type])
                )
            except Exception as exc:
                counts[entity_type] = f"error: {exc}"
        return counts

    # -- loading -------------------------------------------------------------------------

    def load(self, dataset_limit: int | None = None) -> GraphSnapshot:
        """Pull the full graph into a snapshot."""
        from datahub.metadata import schema_classes as S

        graph = self.connect()
        entities: dict[str, Entity] = {}
        column_edges: list[LineageEdge] = []
        dataset_edges: list[LineageEdge] = []

        dataset_urns = list(graph.get_urns_by_filter(entity_types=[DATASET_ENTITY]))
        if dataset_limit is not None:
            dataset_urns = dataset_urns[:dataset_limit]
        logger.info("loading %d datasets", len(dataset_urns))

        for urn in dataset_urns:
            entity = self._load_dataset(graph, S, urn)
            if entity is None:
                continue
            entities[urn] = entity

            upstream = graph.get_aspect(urn, S.UpstreamLineageClass)
            if upstream:
                dataset_edges.extend(self._dataset_edges(upstream, urn))
                column_edges.extend(self._column_edges(upstream))

        for entity_type in ML_ENTITY_TYPES:
            try:
                ml_urns = list(graph.get_urns_by_filter(entity_types=[entity_type]))
            except Exception as exc:
                # Not every entity in DataHub's metadata model has a member in the GraphQL
                # EntityType enum that search filters on -- mlModelDeployment does not, so
                # listing it fails on a perfectly healthy instance. Anything referenced by a
                # loaded entity is picked up by _load_referenced() below instead, so this is
                # expected rather than a problem worth shouting about.
                logger.debug("cannot list %s via search (%s); will fetch by urn", entity_type, exc)
                continue
            logger.info("loading %d %s", len(ml_urns), entity_type)
            for urn in ml_urns:
                entity = self._load_ml_entity(graph, S, urn, entity_type)
                if entity is not None:
                    entities[urn] = entity

        self._load_referenced(graph, S, entities)

        version = None
        with contextlib.suppress(Exception):
            version = str(graph.server_config.raw_config.get("versions", ""))[:200] or None

        return GraphSnapshot(
            entities=entities,
            column_edges=_dedupe_edges(column_edges),
            dataset_edges=_dedupe_edges(dataset_edges),
            captured_at=datetime.now(timezone.utc).isoformat(),
            source="live",
            datahub_version=version,
        )

    # -- datasets ------------------------------------------------------------------------

    def _load_dataset(self, graph, S, urn: str) -> Entity | None:
        status = graph.get_aspect(urn, S.StatusClass)
        if status and status.removed:
            return None

        props = graph.get_aspect(urn, S.DatasetPropertiesClass)
        schema = graph.get_aspect(urn, S.SchemaMetadataClass)
        editable = graph.get_aspect(urn, S.EditableSchemaMetadataClass)
        tags = graph.get_aspect(urn, S.GlobalTagsClass)
        terms = graph.get_aspect(urn, S.GlossaryTermsClass)
        ownership = graph.get_aspect(urn, S.OwnershipClass)
        structured = graph.get_aspect(urn, S.StructuredPropertiesClass)

        return Entity(
            urn=urn,
            kind=NodeKind.DATASET,
            name=(props.name if props else None) or _dataset_name(urn),
            platform=_platform_of(urn),
            description=props.description if props else None,
            tags=_tag_urns(tags),
            terms=_term_urns(terms),
            owners=[o.owner for o in ownership.owners] if ownership else [],
            custom_properties=dict(props.customProperties or {}) if props else {},
            structured_properties=_structured(structured),
            fields=self._fields(urn, schema, editable),
        )

    def _fields(self, dataset_urn: str, schema, editable) -> list[SchemaField]:
        """Merge schema-level and UI-edited field metadata.

        DataHub keeps column tags and terms in two aspects: ``SchemaMetadata`` holds what
        ingestion produced, ``EditableSchemaMetadata`` holds what people applied in the UI.
        Reading only the first misses every governance label a human ever set -- which is
        most of them, and precisely the ones the compliance detector needs.
        """
        if not schema or not schema.fields:
            return []

        overlay: dict[str, Any] = {}
        if editable and editable.editableSchemaFieldInfo:
            overlay = {f.fieldPath: f for f in editable.editableSchemaFieldInfo}

        import datahub.emitter.mce_builder as builder

        out: list[SchemaField] = []
        for field in schema.fields:
            edit = overlay.get(field.fieldPath)
            out.append(
                SchemaField(
                    field_path=field.fieldPath,
                    urn=builder.make_schema_field_urn(dataset_urn, field.fieldPath),
                    native_type=field.nativeDataType,
                    type_class=_type_name(field.type),
                    description=(edit.description if edit and edit.description else field.description),
                    nullable=bool(field.nullable),
                    tags=sorted(set(_tag_urns(field.globalTags)) | set(_tag_urns(getattr(edit, "globalTags", None)))),
                    terms=sorted(set(_term_urns(field.glossaryTerms)) | set(_term_urns(getattr(edit, "glossaryTerms", None)))),
                )
            )
        return out

    @staticmethod
    def _dataset_edges(upstream, downstream_urn: str) -> list[LineageEdge]:
        return [
            LineageEdge(upstream=u.dataset, downstream=downstream_urn)
            for u in (upstream.upstreams or [])
        ]

    @staticmethod
    def _column_edges(upstream) -> list[LineageEdge]:
        """Expand fine-grained lineage into one edge per (upstream column, downstream column)."""
        edges: list[LineageEdge] = []
        for fine in upstream.fineGrainedLineages or []:
            for up in fine.upstreams or []:
                for down in fine.downstreams or []:
                    edges.append(
                        LineageEdge(
                            upstream=up,
                            downstream=down,
                            transform=fine.transformOperation,
                            confidence=(
                                fine.confidenceScore
                                if fine.confidenceScore is not None
                                else 1.0
                            ),
                            query=fine.query,
                        )
                    )
        return edges

    # -- ML entities ---------------------------------------------------------------------

    def _load_ml_entity(self, graph, S, urn: str, entity_type: str) -> Entity | None:
        status = graph.get_aspect(urn, S.StatusClass)
        if status and status.removed:
            return None

        tags = graph.get_aspect(urn, S.GlobalTagsClass)
        terms = graph.get_aspect(urn, S.GlossaryTermsClass)
        structured = graph.get_aspect(urn, S.StructuredPropertiesClass)

        entity = Entity(
            urn=urn,
            kind=_ml_kind(entity_type),
            platform=_platform_of(urn),
            tags=_tag_urns(tags),
            terms=_term_urns(terms),
            structured_properties=_structured(structured),
        )

        if entity_type == "mlModel":
            props = graph.get_aspect(urn, S.MLModelPropertiesClass)
            if props:
                entity.name = props.name or _last_urn_part(urn)
                entity.description = props.description
                entity.custom_properties = dict(props.customProperties or {})
                entity.ml_features = list(props.mlFeatures or [])
                entity.deployments = list(props.deployments or [])
                entity.groups = list(props.groups or [])
                entity.training_jobs = list(props.trainingJobs or [])
                entity.downstream_jobs = list(props.downstreamJobs or [])
                entity.hyper_parameters = dict(props.hyperParameters or {})
                entity.training_metrics = _metrics(props.trainingMetrics)
                entity.online_metrics = _metrics(props.onlineMetrics)

        elif entity_type == "mlFeature":
            props = graph.get_aspect(urn, S.MLFeaturePropertiesClass)
            if props:
                entity.name = _last_urn_part(urn)
                entity.description = props.description
                entity.custom_properties = dict(props.customProperties or {})
                entity.feature_sources = list(props.sources or [])

        elif entity_type == "mlPrimaryKey":
            props = graph.get_aspect(urn, S.MLPrimaryKeyPropertiesClass)
            if props:
                entity.name = _last_urn_part(urn)
                entity.description = props.description
                entity.custom_properties = dict(props.customProperties or {})
                entity.feature_sources = list(props.sources or [])

        elif entity_type == "mlFeatureTable":
            props = graph.get_aspect(urn, S.MLFeatureTablePropertiesClass)
            if props:
                entity.name = _last_urn_part(urn)
                entity.description = props.description
                entity.custom_properties = dict(props.customProperties or {})
                entity.ml_features = list(props.mlFeatures or [])
                entity.ml_primary_keys = list(props.mlPrimaryKeys or [])

        elif entity_type == "mlModelGroup":
            props = graph.get_aspect(urn, S.MLModelGroupPropertiesClass)
            if props:
                entity.name = props.name or _last_urn_part(urn)
                entity.description = props.description
                entity.custom_properties = dict(props.customProperties or {})

        elif entity_type == "mlModelDeployment":
            props = graph.get_aspect(urn, S.MLModelDeploymentPropertiesClass)
            if props:
                entity.name = _last_urn_part(urn)
                entity.description = props.description
                entity.custom_properties = dict(props.customProperties or {})

        return entity


    def _load_referenced(self, graph, S, entities: dict[str, Entity]) -> None:
        """Fetch entities that loaded entities point at but search did not return.

        Search-based listing goes through DataHub's GraphQL EntityType enum, and not every
        entity in the metadata model has a member there -- ``mlModelDeployment`` does not, so
        listing it fails outright. Anything a model references is fetched directly by URN
        instead, which does not depend on that enum at all.
        """
        referenced: set[str] = set()
        for entity in list(entities.values()):
            if entity.kind is NodeKind.ML_MODEL:
                referenced.update(entity.deployments)
                referenced.update(entity.groups)
            elif entity.kind is NodeKind.ML_FEATURE_TABLE:
                referenced.update(entity.ml_features)
                referenced.update(entity.ml_primary_keys)

        for urn in sorted(referenced - set(entities)):
            entity_type = _entity_type_of(urn)
            if not entity_type:
                continue
            try:
                loaded = self._load_ml_entity(graph, S, urn, entity_type)
            except Exception as exc:
                logger.warning("could not fetch referenced %s: %s", urn, exc)
                continue
            if loaded is not None:
                entities[urn] = loaded
                logger.info("fetched referenced %s by urn", entity_type)


# --------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------


def save_snapshot(snapshot: GraphSnapshot, path: str | Path) -> Path:
    """Write a snapshot to disk as the replay fixture."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(snapshot.model_dump(mode="json"), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return target


def load_snapshot(path: str | Path) -> GraphSnapshot:
    """Read a snapshot captured earlier by :func:`save_snapshot`."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    snapshot = GraphSnapshot.model_validate(raw)
    snapshot.origin = snapshot.origin or snapshot.source
    snapshot.source = f"replay:{Path(path).name}"
    return snapshot


def load_graph(
    config: DataHubConfig | None = None,
    fixture: str | Path | None = None,
    dataset_limit: int | None = None,
) -> MetadataGraph:
    """Build a graph from a fixture when given one, otherwise from a live instance."""
    if fixture:
        return MetadataGraph(load_snapshot(fixture))
    return MetadataGraph(DataHubLoader(config).load(dataset_limit=dataset_limit))


# --------------------------------------------------------------------------------------
# Aspect helpers
# --------------------------------------------------------------------------------------


def _entity_type_of(urn: str) -> str | None:
    """DataHub entity-type name from a URN, for direct-by-URN fetches."""
    if not urn.startswith("urn:li:"):
        return None
    name = urn.split(":", 3)[2]
    return name if name in ML_ENTITY_TYPES else None


def _ml_kind(entity_type: str) -> NodeKind:
    """Map a DataHub ML entity type name to a node kind."""
    return {
        "mlModel": NodeKind.ML_MODEL,
        "mlModelGroup": NodeKind.ML_MODEL_GROUP,
        "mlFeature": NodeKind.ML_FEATURE,
        "mlPrimaryKey": NodeKind.ML_FEATURE,
        "mlFeatureTable": NodeKind.ML_FEATURE_TABLE,
        "mlModelDeployment": NodeKind.ML_MODEL_DEPLOYMENT,
    }.get(entity_type, NodeKind.UNKNOWN)


def _tag_urns(aspect) -> list[str]:
    if not aspect or not getattr(aspect, "tags", None):
        return []
    return [t.tag for t in aspect.tags]


def _term_urns(aspect) -> list[str]:
    if not aspect or not getattr(aspect, "terms", None):
        return []
    return [t.urn for t in aspect.terms]


def _structured(aspect) -> dict[str, list[Any]]:
    if not aspect or not getattr(aspect, "properties", None):
        return {}
    return {p.propertyUrn: list(p.values or []) for p in aspect.properties}


def _metrics(metrics) -> dict[str, Any]:
    if not metrics:
        return {}
    return {m.name: m.value for m in metrics if m.name is not None}


def _type_name(field_type) -> str | None:
    inner = getattr(field_type, "type", None)
    return type(inner).__name__.replace("Class", "") if inner is not None else None


def _platform_of(urn: str) -> str | None:
    marker = "urn:li:dataPlatform:"
    if marker not in urn:
        return None
    start = urn.index(marker) + len(marker)
    rest = urn[start:]
    for stop in (",", ")"):
        if stop in rest:
            rest = rest.split(stop)[0]
    return rest or None


def _dataset_name(urn: str) -> str | None:
    from ..models import _split_top_level

    if not urn.startswith("urn:li:dataset:"):
        return None
    parts = _split_top_level(urn[len("urn:li:dataset:") :].strip("()"))
    return parts[1] if len(parts) >= 2 else None


def _last_urn_part(urn: str) -> str | None:
    """The human-readable name inside a URN.

    Platform-qualified URNs are ``(platform, name, env)``, so the *last* segment is the
    environment — taking it names every deployment "PROD". The name is the middle segment
    for those, and the trailing one for URNs like ``mlFeature:(table, feature)``.
    """
    from ..models import _split_top_level

    if ":(" not in urn:
        return urn.rsplit(":", 1)[-1]

    inner = urn[urn.index(":(") + 2 :].rstrip(")")
    parts = _split_top_level(inner)
    if not parts:
        return None
    if parts[0].startswith("urn:li:dataPlatform:"):
        return parts[1] if len(parts) >= 2 else parts[0]
    return parts[-1]


def _dedupe_edges(edges: Iterable[LineageEdge]) -> list[LineageEdge]:
    """Collapse duplicate edges, keeping the highest-confidence rendition of each."""
    best: dict[tuple[str, str, str | None], LineageEdge] = {}
    for edge in edges:
        key = (edge.upstream, edge.downstream, edge.transform)
        current = best.get(key)
        if current is None or edge.confidence > current.confidence:
            best[key] = edge
    return sorted(best.values(), key=lambda e: (e.upstream, e.downstream, e.transform or ""))
