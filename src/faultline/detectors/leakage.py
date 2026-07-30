"""Target leakage: a feature that is computed, however indirectly, from the label.

This is the finding that statistical tooling cannot produce. A leaked feature is not
out-of-distribution, not drifting, and not null -- it looks perfect, and it makes the model
look perfect too, right up until production traffic arrives without the answer baked in.

Lineage settles it. If a path exists from the label column down to a feature the model
consumes, the feature descends from the thing being predicted. That is not a heuristic or a
correlation; it is a chain of transformations that either exists in the graph or does not.
"""

from __future__ import annotations

from ..config import DetectorSettings
from ..graph.graph import MetadataGraph
from ..models import Finding, FindingKind, Severity, short_urn
from .base import Detector, register


@register
class TargetLeakageDetector(Detector):
    name = "target-leakage"
    kind = FindingKind.TARGET_LEAKAGE
    description = "Proves a model feature derives from its own prediction target"

    def run(self, graph: MetadataGraph, settings: DetectorSettings, baseline=None) -> list[Finding]:
        findings: list[Finding] = []

        for model in graph.models():
            labels = self.label_columns(graph, model, settings)
            if not labels:
                # Without a known target there is nothing to prove. Silence beats guessing.
                continue

            for feature in graph.model_features(model.urn):
                bridges = graph.feature_source_columns(feature.urn)
                bridge_columns = {b.column_urn for b in bridges}

                for label_urn, how in labels.items():
                    # The blunt case: the label is literally wired in as a feature.
                    if label_urn in bridge_columns:
                        findings.append(
                            self._direct_finding(graph, model, feature, label_urn, how, settings)
                        )
                        continue

                    # The subtle case: the label flows into the feature through transforms.
                    paths = graph.paths_between(
                        label_urn,
                        feature.urn,
                        max_depth=settings.max_depth,
                        max_paths=settings.max_paths_per_finding * 2,
                    )
                    if paths:
                        findings.append(
                            self._derived_finding(
                                graph, model, feature, label_urn, how, paths, settings
                            )
                        )

        return findings

    # -- finding construction ------------------------------------------------------------

    def _direct_finding(self, graph, model, feature, label_urn, how, settings) -> Finding:
        proofs = self.trim(
            graph.paths_between(label_urn, feature.urn, max_depth=2, max_paths=2), settings
        )
        label_name = short_urn(label_urn)
        feature_name = short_urn(feature.urn)

        return Finding(
            kind=self.kind,
            severity=self.severity_for(Severity.CRITICAL, model, proofs, settings),
            title=f"Feature `{feature_name}` is the label `{label_name}`",
            summary=(
                f"Model `{short_urn(model.urn)}` consumes `{feature_name}`, which resolves to "
                f"the same column as its prediction target `{label_name}`. Any offline metric "
                f"for this model is measuring its ability to copy the answer."
            ),
            model_urn=model.urn,
            feature_urn=feature.urn,
            column_urn=label_urn,
            dataset_urn=graph.dataset_of_column(label_urn),
            proofs=proofs,
            evidence={
                "label_identified_by": how,
                "relationship": "feature_is_label",
                "model_deployed": model.is_deployed(),
                "bridge_method": graph.bridge_method_for(feature.urn),
            },
            remediation=(
                f"Remove `{feature_name}` from the feature set of `{short_urn(model.urn)}` and "
                f"retrain. Treat every prior evaluation of this model as invalid."
            ),
        )

    def _derived_finding(self, graph, model, feature, label_urn, how, paths, settings) -> Finding:
        proofs = self.trim(paths, settings)
        label_name = short_urn(label_urn)
        feature_name = short_urn(feature.urn)
        hops = proofs[0].length if proofs else 0
        transforms = proofs[0].transforms() if proofs else []

        return Finding(
            kind=self.kind,
            severity=self.severity_for(Severity.CRITICAL, model, proofs, settings),
            title=f"Feature `{feature_name}` derives from label `{label_name}` ({hops} hops)",
            summary=(
                f"There is a lineage path from the prediction target `{label_name}` to "
                f"`{feature_name}`, a feature of model `{short_urn(model.urn)}`. The feature "
                f"encodes the answer, so offline accuracy is inflated and will not survive "
                f"contact with production data, where the label is not yet known."
            ),
            model_urn=model.urn,
            feature_urn=feature.urn,
            column_urn=label_urn,
            dataset_urn=graph.dataset_of_column(label_urn),
            proofs=proofs,
            evidence={
                "label_identified_by": how,
                "relationship": "feature_derives_from_label",
                "hops": hops,
                "transforms": transforms,
                "path_count": len(paths),
                "model_deployed": model.is_deployed(),
                "bridge_method": graph.bridge_method_for(feature.urn),
            },
            remediation=(
                f"Break the dependency: recompute `{feature_name}` from inputs that exclude "
                f"`{label_name}`, or drop the feature. Re-evaluate the model afterwards -- the "
                f"current metrics are not a measurement of predictive skill."
            ),
        )
