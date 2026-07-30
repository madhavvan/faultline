"""Restricted data reaching a deployed model.

Governance labels are applied to warehouse columns, but they stop at the warehouse boundary.
Nobody re-applies a PII term to the feature computed from that column, or to the model
trained on that feature, or to the endpoint serving it -- so a column correctly marked
restricted can end up inside a model deployed in a region where that is not permitted, with
every individual asset looking compliant on its own.

The violation is only visible end to end, which is exactly what the lineage graph is for.
"""

from __future__ import annotations

from ..config import DetectorSettings
from ..graph.graph import MetadataGraph
from ..models import Finding, FindingKind, Severity, short_urn
from .base import Detector, register


@register
class CompliancePropagationDetector(Detector):
    name = "compliance-propagation"
    kind = FindingKind.COMPLIANCE_PROPAGATION
    description = "Traces restricted columns into the models and endpoints that expose them"

    def run(self, graph: MetadataGraph, settings: DetectorSettings, baseline=None) -> list[Finding]:
        marked = self._restricted_columns(graph, settings)
        if not marked:
            return []

        findings: list[Finding] = []

        for column_urn, labels in sorted(marked.items()):
            affected = graph.models_depending_on(column_urn, max_depth=settings.max_depth)
            for model_urn, path in sorted(affected.items()):
                model = graph.entity(model_urn)
                if model is None:
                    continue

                # A model that already carries the same restriction is governed correctly --
                # the defect is the *unlabelled* propagation, not the data flow itself.
                if self._already_labelled(model, labels):
                    continue

                proofs = self.trim([path], settings)
                label_names = [short_urn(t) for t in labels]
                deployments = model.deployments

                findings.append(
                    Finding(
                        kind=self.kind,
                        severity=self.severity_for(Severity.HIGH, model, proofs, settings),
                        title=(
                            f"{'/'.join(label_names)} column reaches `{short_urn(model_urn)}`"
                        ),
                        summary=(
                            f"Column `{short_urn(column_urn)}` is classified "
                            f"{'/'.join(label_names)} and flows into model "
                            f"`{short_urn(model_urn)}`"
                            + (
                                f", which is served at "
                                f"{', '.join(short_urn(d) for d in deployments)}"
                                if deployments
                                else ""
                            )
                            + ". Neither the model nor its features carry that classification, "
                            "so the restriction is not visible to anyone reviewing them."
                        ),
                        model_urn=model_urn,
                        column_urn=column_urn,
                        dataset_urn=graph.dataset_of_column(column_urn),
                        proofs=proofs,
                        evidence={
                            "classifications": labels,
                            "hops": path.length,
                            "deployments": deployments,
                            "model_deployed": model.is_deployed(),
                            "model_existing_terms": model.terms,
                            "model_existing_tags": model.tags,
                        },
                        remediation=(
                            f"Either propagate {'/'.join(label_names)} onto the intermediate "
                            f"features and `{short_urn(model_urn)}` so the restriction travels "
                            f"with the data, or remove the column from the model's lineage. "
                            f"Faultline can apply the propagation for you with `--write-back`."
                        ),
                    )
                )

        return findings

    # -- helpers ---------------------------------------------------------------------------

    @staticmethod
    def _restricted_columns(
        graph: MetadataGraph, settings: DetectorSettings
    ) -> dict[str, list[str]]:
        """Columns carrying any configured restricted term or tag."""
        wanted_terms = set(settings.restricted_terms)
        wanted_tags = set(settings.restricted_tags)
        hits: dict[str, list[str]] = {}

        for entity in graph.entities.values():
            if entity.removed:
                continue
            for field in entity.fields:
                matched = sorted(
                    (set(field.terms) & wanted_terms) | (set(field.tags) & wanted_tags)
                )
                if matched:
                    hits[field.urn] = matched
        return hits

    @staticmethod
    def _already_labelled(model, labels: list[str]) -> bool:
        """True if the model already advertises every restriction it inherits."""
        carried = set(model.terms) | set(model.tags)
        return set(labels).issubset(carried)
