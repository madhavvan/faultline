"""Training/serving skew, detected from the shape of the pipeline rather than the data.

The classic production failure: one logical feature is computed twice -- once in a batch job
for training, once in a serving path for inference -- and the two definitions drift apart.
Someone edits the batch transform and not its online twin. Nothing errors. Both feature
values stay inside their historical ranges, so distribution monitors see nothing. The model
is simply, quietly wrong on live traffic.

Faultline finds it without looking at a single value: it walks the derivation path of each
feature and compares them. Two paths that should be identical, and are not, is the defect.
The fork point is reported so the fix is unambiguous.
"""

from __future__ import annotations

from ..config import DetectorSettings
from ..graph.entities import Entity
from ..graph.graph import MetadataGraph
from ..models import (
    Finding,
    FindingKind,
    LineageHop,
    NodeKind,
    ProofPath,
    Severity,
    short_urn,
)
from .base import Detector, register

OFFLINE = "offline"
ONLINE = "online"


@register
class TrainServeSkewDetector(Detector):
    name = "train-serve-skew"
    kind = FindingKind.TRAIN_SERVE_SKEW
    description = "Proves a feature's offline and online derivation paths have diverged"

    def run(self, graph: MetadataGraph, settings: DetectorSettings, baseline=None) -> list[Finding]:
        pairs = self._pair_features(graph, settings)
        findings: list[Finding] = []

        for logical_name, (offline, online) in sorted(pairs.items()):
            finding = self._compare(graph, logical_name, offline, online, settings)
            if finding:
                findings.append(finding)

        return findings

    # -- pairing -------------------------------------------------------------------------

    def _pair_features(
        self, graph: MetadataGraph, settings: DetectorSettings
    ) -> dict[str, tuple[Entity, Entity]]:
        """Group features by logical name, keeping those with both an offline and online twin."""
        buckets: dict[str, dict[str, Entity]] = {}

        for feature in graph.features():
            mode = self._serving_mode(graph, feature, settings)
            if mode is None:
                continue
            logical = self._logical_name(feature)
            if not logical:
                continue
            buckets.setdefault(logical, {})[mode] = feature

        return {
            name: (sides[OFFLINE], sides[ONLINE])
            for name, sides in buckets.items()
            if OFFLINE in sides and ONLINE in sides
        }

    def _serving_mode(
        self, graph: MetadataGraph, feature: Entity, settings: DetectorSettings
    ) -> str | None:
        """Classify a feature as offline or online.

        Checked in order of explicitness: a declared property, then tags, then the name of
        the owning feature table (the convention most feature stores fall into naturally).
        """
        declared = (feature.custom_properties.get(settings.serving_property) or "").lower()
        if declared:
            if any(m in declared for m in settings.offline_markers):
                return OFFLINE
            if any(m in declared for m in settings.online_markers):
                return ONLINE

        haystacks = [t.lower() for t in feature.tags]
        haystacks.extend(self._table_names(graph, feature))
        blob = " ".join(haystacks)

        # Check online first: "customer_features_online" contains neither marker ambiguously,
        # but a table named "offline_online_bridge" should not silently classify as offline.
        if any(m in blob for m in settings.online_markers):
            return ONLINE
        if any(m in blob for m in settings.offline_markers):
            return OFFLINE
        return None

    @staticmethod
    def _table_names(graph: MetadataGraph, feature: Entity) -> list[str]:
        names: list[str] = []
        urn = feature.urn
        if urn.startswith("urn:li:mlFeature:"):
            inner = urn.split(":", 3)[-1].strip("()")
            parts = [p.strip() for p in inner.split(",")]
            if parts:
                names.append(parts[0].lower())
        for hop in graph.downstream_hops(feature.urn):
            table = graph.entity(hop.downstream)
            if table and table.kind is NodeKind.ML_FEATURE_TABLE and table.name:
                names.append(table.name.lower())
        return names

    @staticmethod
    def _logical_name(feature: Entity) -> str | None:
        """The feature's name, stripped of offline/online decoration."""
        name = feature.name
        if not name:
            urn = feature.urn
            if urn.startswith("urn:li:mlFeature:"):
                inner = urn.split(":", 3)[-1].strip("()")
                parts = [p.strip() for p in inner.split(",")]
                name = parts[-1] if len(parts) >= 2 else None
        if not name:
            return None
        lowered = name.lower()
        for suffix in ("_online", "_offline", "_batch", "_realtime", "_serving", "_training"):
            if lowered.endswith(suffix):
                lowered = lowered[: -len(suffix)]
        return lowered

    # -- comparison ----------------------------------------------------------------------

    def _compare(
        self,
        graph: MetadataGraph,
        logical_name: str,
        offline: Entity,
        online: Entity,
        settings: DetectorSettings,
    ) -> Finding | None:
        offline_paths = self._column_ancestry(graph, offline, settings)
        online_paths = self._column_ancestry(graph, online, settings)

        model = self._consuming_model(graph, offline) or self._consuming_model(graph, online)

        shared = set(offline_paths) & set(online_paths)

        if not shared:
            if offline_paths and online_paths:
                return self._disjoint_finding(
                    graph, logical_name, offline, online, offline_paths,
                    online_paths, model, settings,
                )
            return None

        # Compare from the shared node furthest upstream first: the longer the path, the
        # more of the derivation the proof covers.
        ordered = sorted(shared, key=lambda u: (-offline_paths[u].length, u))
        for root in ordered:
            off_path, on_path = offline_paths[root], online_paths[root]
            if off_path.signature() != on_path.signature():
                return self._divergence_finding(
                    graph, logical_name, offline, online, root, off_path,
                    on_path, model, settings,
                )

        # Source asymmetry is compared over *root* columns only. Every intermediate table
        # differs by construction -- the offline path materialises through offline tables and
        # the online path through online ones -- so comparing all ancestors would flag every
        # correctly-built feature pair. An extra upstream join shows up as an extra root.
        offline_roots = self._root_columns(graph, offline_paths)
        online_roots = self._root_columns(graph, online_paths)
        offline_only = offline_roots - online_roots
        online_only = online_roots - offline_roots

        if offline_only or online_only:
            return self._source_asymmetry_finding(
                graph, logical_name, offline, online, offline_only,
                online_only, offline_paths, online_paths, model, settings,
            )

        return None

    @staticmethod
    def _root_columns(graph: MetadataGraph, paths: dict[str, ProofPath]) -> set[str]:
        """Ancestry columns that are true sources -- nothing upstream feeds them."""
        roots: set[str] = set()
        for column_urn in paths:
            upstream = [
                h for h in graph.upstream_hops(column_urn)
                if h.upstream.startswith("urn:li:schemaField:")
            ]
            if not upstream:
                roots.add(column_urn)
        return roots

    def _column_ancestry(
        self, graph: MetadataGraph, feature: Entity, settings: DetectorSettings
    ) -> dict[str, ProofPath]:
        ancestors = graph.ancestors(feature.urn, max_depth=settings.max_depth)
        return {
            urn: path
            for urn, path in ancestors.items()
            if urn.startswith("urn:li:schemaField:")
        }

    @staticmethod
    def _consuming_model(graph: MetadataGraph, feature: Entity) -> Entity | None:
        for hop in graph.downstream_hops(feature.urn):
            entity = graph.entity(hop.downstream)
            if entity and entity.kind is NodeKind.ML_MODEL:
                return entity
        return None

    # -- finding construction ------------------------------------------------------------

    def _divergence_finding(
        self, graph, logical_name, offline, online, root, off_path, on_path, model, settings
    ) -> Finding:
        fork = _first_divergence(off_path, on_path)
        proofs = self.trim([off_path, on_path], settings)

        if fork:
            index, off_hop, on_hop = fork
            fork_desc = (
                f"The paths agree for {index} hop(s) from `{short_urn(root)}`, then split: "
                f"the offline path applies `{off_hop.transform or 'an untransformed copy'}` "
                f"while the online path applies `{on_hop.transform or 'an untransformed copy'}`."
            )
            fork_evidence = {
                "fork_after_hops": index,
                "offline_transform": off_hop.transform,
                "online_transform": on_hop.transform,
                "offline_next_node": short_urn(off_hop.downstream),
                "online_next_node": short_urn(on_hop.downstream),
            }
        else:
            fork_desc = (
                f"Both paths start at `{short_urn(root)}` but their transformation chains "
                f"differ in length or shape."
            )
            fork_evidence = {"fork_after_hops": None}

        return Finding(
            kind=self.kind,
            severity=self.severity_for(Severity.HIGH, model, proofs, settings),
            title=f"Offline and online `{logical_name}` are computed differently",
            summary=(
                f"Feature `{logical_name}` is derived one way for training and another way for "
                f"serving. {fork_desc} The model was trained on one definition and is scored "
                f"against the other, so every live prediction is made on inputs the model "
                f"never saw during training."
            ),
            model_urn=model.urn if model else None,
            feature_urn=offline.urn,
            column_urn=root,
            dataset_urn=graph.dataset_of_column(root),
            proofs=proofs,
            evidence={
                "logical_feature": logical_name,
                "offline_feature": offline.urn,
                "online_feature": online.urn,
                "shared_root": root,
                "offline_signature": off_path.signature(),
                "online_signature": on_path.signature(),
                "model_deployed": bool(model and model.is_deployed()),
                **fork_evidence,
            },
            remediation=(
                f"Reconcile the two definitions of `{logical_name}` at the fork point, then "
                f"backfill or retrain. Longer term, compute the feature once and materialise "
                f"both stores from that single definition so the paths cannot drift again."
            ),
        )

    def _disjoint_finding(
        self, graph, logical_name, offline, online, offline_paths, online_paths, model, settings
    ) -> Finding:
        proofs = self.trim(
            [next(iter(offline_paths.values())), next(iter(online_paths.values()))], settings
        )
        return Finding(
            kind=self.kind,
            severity=self.severity_for(Severity.CRITICAL, model, proofs, settings),
            title=f"Offline and online `{logical_name}` share no common source",
            summary=(
                f"The training and serving copies of `{logical_name}` have entirely separate "
                f"upstream lineage -- not one shared source column. They are two unrelated "
                f"quantities wearing the same name, which means the served feature has no "
                f"defined relationship to what the model learned."
            ),
            model_urn=model.urn if model else None,
            feature_urn=offline.urn,
            proofs=proofs,
            evidence={
                "logical_feature": logical_name,
                "offline_feature": offline.urn,
                "online_feature": online.urn,
                "offline_roots": sorted(short_urn(u) for u in offline_paths),
                "online_roots": sorted(short_urn(u) for u in online_paths),
                "model_deployed": bool(model and model.is_deployed()),
            },
            remediation=(
                f"Confirm which source is authoritative for `{logical_name}`, then rebuild the "
                f"other path from it. If both are intentional, rename one -- a shared name "
                f"across unrelated definitions guarantees this recurs."
            ),
        )

    def _source_asymmetry_finding(
        self, graph, logical_name, offline, online, offline_only, online_only,
        offline_paths, online_paths, model, settings,
    ) -> Finding:
        sample = list(offline_only or online_only)[0]
        source = offline_paths if offline_only else online_paths
        proofs = self.trim([source[sample]], settings)
        side = "offline" if offline_only else "online"
        other = "online" if offline_only else "offline"

        return Finding(
            kind=self.kind,
            severity=self.severity_for(Severity.MEDIUM, model, proofs, settings),
            title=f"`{logical_name}` reads extra columns on the {side} path",
            summary=(
                f"The {side} derivation of `{logical_name}` depends on "
                f"{len(offline_only or online_only)} column(s) the {other} path never touches "
                f"(for example `{short_urn(sample)}`). An upstream join or filter exists on one "
                f"side only, so the two definitions can disagree whenever that column does."
            ),
            model_urn=model.urn if model else None,
            feature_urn=offline.urn,
            column_urn=sample,
            dataset_urn=graph.dataset_of_column(sample),
            proofs=proofs,
            evidence={
                "logical_feature": logical_name,
                "offline_only_columns": sorted(short_urn(u) for u in offline_only),
                "online_only_columns": sorted(short_urn(u) for u in online_only),
                "model_deployed": bool(model and model.is_deployed()),
            },
            remediation=(
                "Apply the same joins and filters on both paths, or move the shared logic "
                "upstream of the split so neither path can diverge independently."
            ),
        )


def _first_divergence(
    a: ProofPath, b: ProofPath
) -> tuple[int, LineageHop, LineageHop] | None:
    """Index of the first hop where two paths stop agreeing, with both hops."""
    for i, (hop_a, hop_b) in enumerate(zip(a.hops, b.hops, strict=False)):
        same = (
            hop_a.downstream == hop_b.downstream
            and hop_a.transform == hop_b.transform
            and hop_a.edge == hop_b.edge
        )
        if not same:
            return i, hop_a, hop_b
    return None
