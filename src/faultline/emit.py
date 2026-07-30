"""Emit a :class:`GraphSnapshot` into a live DataHub instance.

This is what turns the demo from a local exercise into something you can open in the DataHub
UI: the dbt-derived warehouse, its column-level lineage, and the ML entities layered on top
all become real DataHub metadata, which Faultline then reads back through the normal client
and the MCP server.

Emitting the same snapshot the offline demo uses is deliberate -- it means the live path and
the replay path are demonstrably the same graph, not two hand-maintained descriptions of one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .config import DataHubConfig
from .graph.entities import Entity, GraphSnapshot
from .models import NodeKind

logger = logging.getLogger(__name__)


@dataclass
class EmitReport:
    """What was pushed, and what refused to go."""

    aspects: int = 0
    entities: int = 0
    terms: int = 0
    failures: list[str] = field(default_factory=list)

    def summary(self) -> str:
        base = f"{self.aspects} aspects across {self.entities} entities ({self.terms} glossary terms)"
        return base if not self.failures else f"{base}; {len(self.failures)} failed"


class SnapshotEmitter:
    """Pushes a snapshot to DataHub as metadata change proposals."""

    def __init__(self, config: DataHubConfig | None = None, client: Any = None) -> None:
        self.config = config or DataHubConfig()
        self._client = client

    def client(self):
        if self._client is None:
            from .graph.loader import DataHubLoader

            self._client = DataHubLoader(self.config).connect()
        return self._client

    # -- entry point ---------------------------------------------------------------------

    def emit(self, snapshot: GraphSnapshot) -> EmitReport:
        report = EmitReport()
        client = self.client()

        self._emit_glossary(client, snapshot, report)

        for entity in snapshot.entities.values():
            if entity.kind is NodeKind.DATASET:
                self._emit_dataset(client, entity, snapshot, report)
            elif entity.kind is NodeKind.ML_FEATURE:
                self._emit_feature(client, entity, report)
            elif entity.kind is NodeKind.ML_FEATURE_TABLE:
                self._emit_feature_table(client, entity, report)
            elif entity.kind is NodeKind.ML_MODEL:
                self._emit_model(client, entity, report)
            elif entity.kind is NodeKind.ML_MODEL_DEPLOYMENT:
                self._emit_deployment(client, entity, report)
            else:
                continue
            report.entities += 1

        return report

    # -- glossary ------------------------------------------------------------------------

    def _emit_glossary(self, client, snapshot: GraphSnapshot, report: EmitReport) -> None:
        """Create the glossary terms the snapshot references.

        A term association pointing at a term that does not exist renders as a dangling URN
        in the UI, so the vocabulary is created before anything references it.
        """
        from datahub.metadata import schema_classes as S

        terms: set[str] = set()
        for entity in snapshot.entities.values():
            terms.update(entity.terms)
            for f in entity.fields:
                terms.update(f.terms)

        for urn in sorted(terms):
            name = urn.rsplit(":", 1)[-1]
            info = S.GlossaryTermInfoClass(
                name=name,
                definition=_TERM_DEFINITIONS.get(
                    name, f"{name} — created by Faultline for the demo pipeline."
                ),
                termSource="INTERNAL",
            )
            if self._push(client, urn, info, report):
                report.terms += 1

    # -- datasets ------------------------------------------------------------------------

    def _emit_dataset(
        self, client, entity: Entity, snapshot: GraphSnapshot, report: EmitReport
    ) -> None:
        from datahub.metadata import schema_classes as S

        self._push(
            client,
            entity.urn,
            S.DatasetPropertiesClass(
                name=entity.name,
                description=entity.description,
                customProperties=dict(entity.custom_properties),
            ),
            report,
        )

        if entity.fields:
            fields = [
                S.SchemaFieldClass(
                    fieldPath=f.field_path,
                    type=S.SchemaFieldDataTypeClass(type=_type_for(f.native_type)),
                    nativeDataType=f.native_type or "unknown",
                    description=f.description,
                    nullable=f.nullable,
                    globalTags=S.GlobalTagsClass(
                        tags=[S.TagAssociationClass(tag=t) for t in f.tags]
                    )
                    if f.tags
                    else None,
                    glossaryTerms=S.GlossaryTermsClass(
                        terms=[S.GlossaryTermAssociationClass(urn=t) for t in f.terms],
                        auditStamp=_stamp(S),
                    )
                    if f.terms
                    else None,
                )
                for f in entity.fields
            ]
            self._push(
                client,
                entity.urn,
                S.SchemaMetadataClass(
                    schemaName=entity.name or entity.urn,
                    platform=f"urn:li:dataPlatform:{entity.platform or 'unknown'}",
                    version=0,
                    hash="",
                    platformSchema=S.OtherSchemaClass(rawSchema=""),
                    fields=fields,
                ),
                report,
            )

        upstream = self._upstream_lineage(entity, snapshot)
        if upstream:
            self._push(client, entity.urn, upstream, report)

    def _upstream_lineage(self, entity: Entity, snapshot: GraphSnapshot):
        """Table-level upstreams plus the column-level lineage parsed from SQL."""
        from datahub.metadata import schema_classes as S

        table_upstreams = sorted(
            {e.upstream for e in snapshot.dataset_edges if e.downstream == entity.urn}
        )

        by_column: dict[tuple[str, str | None], list[str]] = {}
        for edge in snapshot.column_edges:
            if not edge.downstream.startswith(f"urn:li:schemaField:({entity.urn},"):
                continue
            by_column.setdefault((edge.downstream, edge.transform), []).append(edge.upstream)

        fine: list[Any] = [
            S.FineGrainedLineageClass(
                upstreamType=S.FineGrainedLineageUpstreamTypeClass.FIELD_SET,
                downstreamType=S.FineGrainedLineageDownstreamTypeClass.FIELD,
                upstreams=sorted(upstreams),
                downstreams=[downstream],
                transformOperation=transform,
                confidenceScore=1.0,
            )
            for (downstream, transform), upstreams in sorted(by_column.items())
        ]

        # Column-level lineage lives inside UpstreamLineage, so a dataset with fine-grained
        # edges but no declared table upstreams still needs the table entries -- otherwise
        # DataHub shows the columns wired to nothing.
        if fine and not table_upstreams:
            table_upstreams = sorted(
                {
                    u.split(",", 1)[0].replace("urn:li:schemaField:(", "")
                    for edges in by_column.values()
                    for u in edges
                }
            )

        if not table_upstreams and not fine:
            return None

        return S.UpstreamLineageClass(
            upstreams=[
                S.UpstreamClass(dataset=u, type=S.DatasetLineageTypeClass.TRANSFORMED)
                for u in table_upstreams
            ],
            fineGrainedLineages=fine or None,
        )

    # -- ML entities ---------------------------------------------------------------------

    def _emit_feature(self, client, entity: Entity, report: EmitReport) -> None:
        from datahub.metadata import schema_classes as S

        self._push(
            client,
            entity.urn,
            S.MLFeaturePropertiesClass(
                description=entity.description,
                customProperties=dict(entity.custom_properties),
                sources=list(entity.feature_sources),
            ),
            report,
        )

    def _emit_feature_table(self, client, entity: Entity, report: EmitReport) -> None:
        from datahub.metadata import schema_classes as S

        self._push(
            client,
            entity.urn,
            S.MLFeatureTablePropertiesClass(
                description=entity.description,
                customProperties=dict(entity.custom_properties),
                mlFeatures=list(entity.ml_features),
                mlPrimaryKeys=list(entity.ml_primary_keys),
            ),
            report,
        )

    def _emit_model(self, client, entity: Entity, report: EmitReport) -> None:
        from datahub.metadata import schema_classes as S

        self._push(
            client,
            entity.urn,
            S.MLModelPropertiesClass(
                name=entity.name,
                description=entity.description,
                customProperties=dict(entity.custom_properties),
                mlFeatures=list(entity.ml_features),
                deployments=list(entity.deployments),
                groups=list(entity.groups),
                trainingMetrics=[
                    S.MLMetricClass(name=k, value=str(v))
                    for k, v in entity.training_metrics.items()
                ]
                or None,
                onlineMetrics=[
                    S.MLMetricClass(name=k, value=str(v))
                    for k, v in entity.online_metrics.items()
                ]
                or None,
            ),
            report,
        )

    def _emit_deployment(self, client, entity: Entity, report: EmitReport) -> None:
        from datahub.metadata import schema_classes as S

        self._push(
            client,
            entity.urn,
            S.MLModelDeploymentPropertiesClass(
                description=entity.description,
                customProperties=dict(entity.custom_properties),
            ),
            report,
        )

    # -- plumbing ------------------------------------------------------------------------

    def _push(self, client, urn: str, aspect: Any, report: EmitReport) -> bool:
        from datahub.emitter.mcp import MetadataChangeProposalWrapper

        try:
            client.emit_mcp(MetadataChangeProposalWrapper(entityUrn=urn, aspect=aspect))
            report.aspects += 1
            return True
        except Exception as exc:
            logger.warning("failed to emit %s on %s: %s", type(aspect).__name__, urn, exc)
            report.failures.append(f"{type(aspect).__name__} on {urn}: {exc}")
            return False


_TERM_DEFINITIONS = {
    "Label": (
        "The prediction target of a model. Faultline uses this to identify a model's label "
        "column; any feature derived from a Label column is target leakage."
    ),
    "PII": (
        "Personally identifiable information. Restricted data that must not reach a "
        "deployed model without the classification travelling with it."
    ),
}


def _stamp(S: Any):
    from datetime import datetime, timezone

    return S.AuditStampClass(
        time=int(datetime.now(timezone.utc).timestamp() * 1000),
        actor="urn:li:corpuser:faultline",
    )


def _type_for(native: str | None):
    """Map a warehouse type onto DataHub's schema-field type union."""
    from datahub.metadata import schema_classes as S

    text = (native or "").upper()
    if any(k in text for k in ("INT", "BIGINT", "SERIAL")):
        return S.NumberTypeClass()
    if any(k in text for k in ("DECIMAL", "NUMERIC", "DOUBLE", "FLOAT", "REAL")):
        return S.NumberTypeClass()
    if "BOOL" in text:
        return S.BooleanTypeClass()
    if any(k in text for k in ("TIMESTAMP", "DATETIME")):
        return S.TimeTypeClass()
    if "DATE" in text:
        return S.DateTypeClass()
    return S.StringTypeClass()
