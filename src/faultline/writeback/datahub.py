"""Write findings back into DataHub, so the graph carries what Faultline learned.

Reading a catalogue is table stakes. The reason to run this against DataHub specifically is
that the answer can go *back*: the next engineer who opens `churn_propensity_v7`, and the
next agent that queries it over MCP, both inherit the finding instead of rediscovering it.

Everything here is planned first and applied second. ``faultline writeback --dry-run`` prints
exactly what would change, because a tool that silently mutates a company's metadata
catalogue on first run does not deserve to be trusted with one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..config import FaultlineConfig
from ..models import Finding, FindingKind, ScanResult, Severity, short_urn

logger = logging.getLogger(__name__)

#: Tag applied per finding kind, so the graph is filterable by defect class.
KIND_TAGS = {
    FindingKind.TARGET_LEAKAGE: "TargetLeakage",
    FindingKind.TRAIN_SERVE_SKEW: "TrainServeSkew",
    FindingKind.SILENT_SEMANTIC_CHANGE: "SilentSemanticChange",
    FindingKind.COMPLIANCE_PROPAGATION: "RestrictedData",
    FindingKind.ORPHANED_FEATURE: "OrphanedFeature",
    FindingKind.UNGOVERNED_UPSTREAM: "UngovernedUpstream",
}

#: Structured properties Faultline defines and maintains on ML models.
PROPERTY_SPECS = [
    ("risk", "STRING", "Highest severity Faultline can currently prove for this model"),
    ("finding_count", "NUMBER", "Number of open structural findings"),
    ("last_scanned", "STRING", "UTC timestamp of the most recent Faultline scan"),
    ("findings", "STRING", "Open findings as `fingerprint:kind:severity`"),
    ("proof_summary", "STRING", "One-line proof for each open finding"),
]


@dataclass
class Change:
    """One intended mutation, renderable before it is applied."""

    entity_urn: str
    kind: str
    detail: str
    payload: Any = None

    def describe(self) -> str:
        return f"{self.kind:<22} {short_urn(self.entity_urn):<45} {self.detail}"


@dataclass
class WritebackPlan:
    """Everything a scan would contribute back to the graph."""

    changes: list[Change] = field(default_factory=list)
    property_definitions: list[str] = field(default_factory=list)

    def add(self, change: Change) -> None:
        self.changes.append(change)

    def __len__(self) -> int:
        return len(self.changes)

    def by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for change in self.changes:
            counts[change.kind] = counts.get(change.kind, 0) + 1
        return counts

    def render(self) -> str:
        if not self.changes:
            return "nothing to write back"
        lines = []
        if self.property_definitions:
            lines.append(
                f"define {len(self.property_definitions)} structured propert"
                f"{'y' if len(self.property_definitions) == 1 else 'ies'}: "
                + ", ".join(self.property_definitions)
            )
            lines.append("")
        lines.extend(change.describe() for change in self.changes)
        return "\n".join(lines)


@dataclass
class WritebackReport:
    """What actually happened."""

    applied: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False


class DataHubWriter:
    """Plans and applies Faultline's contribution to the DataHub graph."""

    def __init__(self, config: FaultlineConfig | None = None, client: Any = None) -> None:
        self.config = config or FaultlineConfig()
        self.settings = self.config.writeback
        self._client = client

    # -- client ---------------------------------------------------------------------------

    def client(self):
        if self._client is None:
            from ..graph.loader import DataHubLoader

            self._client = DataHubLoader(self.config.datahub).connect()
        return self._client

    # -- planning -------------------------------------------------------------------------

    def plan(self, result: ScanResult, report_url: str | None = None) -> WritebackPlan:
        """Compute every change without touching DataHub."""
        plan = WritebackPlan()
        ns = self.settings.structured_property_namespace
        prefix = self.settings.tag_prefix

        if self.settings.write_structured_properties:
            plan.property_definitions = [f"{ns}.{name}" for name, _, _ in PROPERTY_SPECS]

        by_model: dict[str, list[Finding]] = {}
        for finding in result.findings:
            if finding.model_urn:
                by_model.setdefault(finding.model_urn, []).append(finding)

        # Models that were scanned and came back clean still get a stamp: "checked, and
        # nothing found" is information the next reader needs just as much.
        for model_urn in result.scanned_models:
            findings = by_model.get(model_urn, [])
            if self.settings.write_structured_properties:
                plan.add(
                    Change(
                        model_urn,
                        "structured-property",
                        self._risk_detail(findings),
                        self._model_properties(model_urn, findings, result),
                    )
                )

        for finding in result.findings:
            tag = f"{prefix}_{KIND_TAGS.get(finding.kind, 'Finding')}"

            if self.settings.write_tags:
                for target in self._tag_targets(finding):
                    plan.add(Change(target, "tag", f"+{tag}", tag))

            if self.settings.write_incidents and finding.is_blocking() and finding.model_urn:
                plan.add(
                    Change(
                        finding.model_urn,
                        "incident",
                        f"{finding.severity.value}: {finding.title}",
                        finding,
                    )
                )

        if self.settings.write_documents and report_url:
            for model_urn in by_model:
                plan.add(
                    Change(model_urn, "link", f"report -> {report_url}", report_url)
                )

        if self.settings.write_run_audit:
            plan.add(
                Change(
                    self._run_urn(result),
                    "audit-run",
                    f"{len(result.findings)} finding(s) across "
                    f"{len(result.scanned_models)} model(s)",
                    result,
                )
            )

        return plan

    def _tag_targets(self, finding: Finding) -> list[str]:
        """Tag every entity a reader might open first."""
        targets = [
            urn
            for urn in (finding.model_urn, finding.feature_urn, finding.column_urn)
            if urn
        ]
        return list(dict.fromkeys(targets))

    @staticmethod
    def _risk_detail(findings: list[Finding]) -> str:
        if not findings:
            return "risk=CLEAR finding_count=0"
        worst = max(f.severity for f in findings)
        return f"risk={worst.value} finding_count={len(findings)}"

    def _model_properties(
        self, model_urn: str, findings: list[Finding], result: ScanResult
    ) -> dict[str, list[Any]]:
        ns = self.settings.structured_property_namespace
        worst = max((f.severity for f in findings), default=None)
        return {
            f"{ns}.risk": [worst.value if worst else "CLEAR"],
            f"{ns}.finding_count": [float(len(findings))],
            f"{ns}.last_scanned": [
                (result.finished_at or datetime.now(timezone.utc)).isoformat()
            ],
            f"{ns}.findings": [
                f"{f.fingerprint}:{f.kind.value}:{f.severity.value}" for f in findings
            ]
            or ["none"],
            f"{ns}.proof_summary": [
                f"{f.title} — {f.proofs[0].length if f.proofs else 0} hop(s)"
                for f in findings
            ]
            or ["no structural findings"],
        }

    @staticmethod
    def _run_urn(result: ScanResult) -> str:
        import datahub.emitter.mce_builder as builder

        stamp = (result.finished_at or result.started_at).strftime("%Y%m%dT%H%M%S")
        return builder.make_data_process_instance_urn(f"faultline-scan-{stamp}")

    # -- applying -------------------------------------------------------------------------

    def apply(self, plan: WritebackPlan, dry_run: bool | None = None) -> WritebackReport:
        """Emit the plan. With ``dry_run`` nothing is sent."""
        dry = self.settings.dry_run if dry_run is None else dry_run
        report = WritebackReport(dry_run=dry)

        if dry:
            report.skipped = len(plan)
            return report

        if not self.settings.enabled:
            logger.info("writeback disabled by configuration")
            report.skipped = len(plan)
            return report

        client = self.client()
        self._define_properties(client, report)

        for change in plan.changes:
            try:
                self._apply_change(client, change)
                report.applied += 1
            except Exception as exc:
                logger.exception("failed to apply %s on %s", change.kind, change.entity_urn)
                report.failed += 1
                report.errors.append(f"{change.kind} {short_urn(change.entity_urn)}: {exc}")

        return report

    def _define_properties(self, client, report: WritebackReport) -> None:
        """Structured properties must be defined before values can be assigned to them."""
        if not self.settings.write_structured_properties:
            return

        from datahub.emitter.mcp import MetadataChangeProposalWrapper
        from datahub.metadata import schema_classes as S

        ns = self.settings.structured_property_namespace
        for name, value_type, description in PROPERTY_SPECS:
            qualified = f"{ns}.{name}"
            urn = f"urn:li:structuredProperty:{qualified}"
            definition = S.StructuredPropertyDefinitionClass(
                qualifiedName=qualified,
                displayName=f"Faultline {name.replace('_', ' ')}",
                valueType=f"urn:li:dataType:datahub.{value_type.lower()}",
                description=description,
                entityTypes=["urn:li:entityType:datahub.mlModel"],
                cardinality="MULTIPLE",
            )
            try:
                client.emit_mcp(
                    MetadataChangeProposalWrapper(entityUrn=urn, aspect=definition)
                )
            except Exception as exc:
                report.errors.append(f"define {qualified}: {exc}")

    def _apply_change(self, client, change: Change) -> None:
        from datahub.emitter.mcp import MetadataChangeProposalWrapper
        from datahub.metadata import schema_classes as S

        if change.kind == "tag":
            existing = client.get_aspect(change.entity_urn, S.GlobalTagsClass)
            tags = list(existing.tags) if existing else []
            tag_urn = f"urn:li:tag:{change.payload}"
            if any(t.tag == tag_urn for t in tags):
                return
            tags.append(S.TagAssociationClass(tag=tag_urn))
            client.emit_mcp(
                MetadataChangeProposalWrapper(
                    entityUrn=change.entity_urn, aspect=S.GlobalTagsClass(tags=tags)
                )
            )

        elif change.kind == "structured-property":
            assignments = [
                S.StructuredPropertyValueAssignmentClass(
                    propertyUrn=f"urn:li:structuredProperty:{key}", values=values
                )
                for key, values in change.payload.items()
            ]
            client.emit_mcp(
                MetadataChangeProposalWrapper(
                    entityUrn=change.entity_urn,
                    aspect=S.StructuredPropertiesClass(properties=assignments),
                )
            )

        elif change.kind == "incident":
            self._emit_incident(client, change.payload)

        elif change.kind == "link":
            existing = client.get_aspect(change.entity_urn, S.InstitutionalMemoryClass)
            elements = list(existing.elements) if existing else []
            if any(e.url == change.payload for e in elements):
                return
            stamp = S.AuditStampClass(
                time=int(datetime.now(timezone.utc).timestamp() * 1000),
                actor="urn:li:corpuser:faultline",
            )
            elements.append(
                S.InstitutionalMemoryMetadataClass(
                    url=change.payload,
                    description="Faultline scan report",
                    createStamp=stamp,
                )
            )
            client.emit_mcp(
                MetadataChangeProposalWrapper(
                    entityUrn=change.entity_urn,
                    aspect=S.InstitutionalMemoryClass(elements=elements),
                )
            )

        elif change.kind == "audit-run":
            self._emit_run(client, change.entity_urn, change.payload)

    def _emit_incident(self, client, finding: Finding) -> None:
        from datahub.emitter.mcp import MetadataChangeProposalWrapper
        from datahub.metadata import schema_classes as S

        urn = f"urn:li:incident:faultline-{finding.fingerprint}"
        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        stamp = S.AuditStampClass(time=now, actor="urn:li:corpuser:faultline")

        proof = finding.proofs[0].render(indent="  ") if finding.proofs else "(no proof)"
        body = (
            f"{finding.summary}\n\nProof:\n{proof}\n\n"
            f"Fix: {finding.remediation or 'see report'}"
        )

        # DataHub restricts IncidentInfo.entities to a fixed set of entity types; an mlModel
        # URN is rejected with a 422 ("not a valid destination for field path /entities/*").
        # The incident therefore hangs off the affected *dataset*, and names the model in its
        # title and body, which is where a reader looks anyway.
        entities = [u for u in (finding.dataset_urn, finding.model_urn) if u and _incidentable(u)]
        if not entities:
            logger.info(
                "skipping incident for %s: no entity DataHub accepts as an incident target",
                finding.fingerprint,
            )
            return
        info = S.IncidentInfoClass(
            type=S.IncidentTypeClass.CUSTOM,
            customType=f"faultline:{finding.kind.value.lower()}",
            title=(
                f"{finding.title} — affects {short_urn(finding.model_urn)}"
                if finding.model_urn
                else finding.title
            ),
            description=body,
            entities=entities,
            priority=_incident_priority(finding.severity),
            source=S.IncidentSourceClass(type="MANUAL"),
            status=S.IncidentStatusClass(
                state=S.IncidentStateClass.ACTIVE,
                message=f"Opened by Faultline · {finding.fingerprint}",
                lastUpdated=stamp,
            ),
            created=stamp,
            startedAt=now,
        )
        client.emit_mcp(MetadataChangeProposalWrapper(entityUrn=urn, aspect=info))

    def _emit_run(self, client, urn: str, result: ScanResult) -> None:
        """Register the scan itself, so the graph carries an audit trail of every check."""
        from datahub.emitter.mcp import MetadataChangeProposalWrapper
        from datahub.metadata import schema_classes as S

        counts = result.severity_counts()
        props = S.DataProcessInstancePropertiesClass(
            name=f"Faultline scan · {result.graph_source}",
            type=S.DataProcessTypeClass.BATCH_SCHEDULED,
            created=S.AuditStampClass(
                time=int(result.started_at.timestamp() * 1000),
                actor="urn:li:corpuser:faultline",
            ),
            customProperties={
                "detectors": ",".join(result.detectors_run),
                "models_scanned": str(len(result.scanned_models)),
                "features_scanned": str(len(result.scanned_features)),
                "findings": str(len(result.findings)),
                "graph_source": result.graph_source,
                **{f"count_{k.lower()}": str(v) for k, v in counts.items()},
            },
        )
        client.emit_mcp(MetadataChangeProposalWrapper(entityUrn=urn, aspect=props))


#: Entity types DataHub accepts in IncidentInfo.entities. ML entities are not among them,
#: so an incident about a model is attached to the dataset the defect originates in.
INCIDENTABLE_PREFIXES = (
    "urn:li:dataset:",
    "urn:li:dataJob:",
    "urn:li:dataFlow:",
    "urn:li:dashboard:",
    "urn:li:chart:",
    "urn:li:container:",
)


def _incidentable(urn: str) -> bool:
    return urn.startswith(INCIDENTABLE_PREFIXES)


def _incident_priority(severity: Severity) -> int:
    """DataHub incident priority: 0 is highest."""
    return {
        Severity.CRITICAL: 0,
        Severity.HIGH: 1,
        Severity.MEDIUM: 2,
        Severity.LOW: 3,
        Severity.INFO: 3,
    }[severity]
