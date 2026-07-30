"""The in-memory metadata graph and the traversal algorithms detectors run on.

Why a materialised graph rather than per-question API calls: a structural detector asks
thousands of reachability questions (every feature against every label column, every
offline path against its online twin). Answering those over the network is quadratic in
round-trips. Faultline loads once, indexes, and traverses locally -- which also makes the
offline replay fixture a drop-in substitute for a live instance.

The important piece of engineering here is :meth:`MetadataGraph.feature_source_columns`.
DataHub's ``MLFeatureProperties.sources`` points at *datasets*, while ``FineGrainedLineage``
operates on *columns*. Nothing in the open-source model joins those two granularities, so a
naive traversal loses column precision the moment it crosses into ML entities -- exactly
where ML-specific defects live. Faultline bridges the gap with a tiered resolver that
degrades confidence rather than silently guessing.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Iterator

from ..models import EdgeKind, LineageHop, NodeKind, ProofPath, _split_top_level, node_kind_of
from .entities import Entity, GraphSnapshot, SchemaField

#: Custom-property keys a feature may use to declare its source columns explicitly.
EXPLICIT_SOURCE_COLUMN_KEYS = (
    "faultline.source_columns",
    "source_columns",
    "source_column",
)

#: How a feature was resolved to columns, worst-to-best. Drives finding confidence.
BRIDGE_EXPLICIT = "explicit"
BRIDGE_NAME_MATCH = "name_match"
BRIDGE_DATASET_WIDE = "dataset_wide"

_BRIDGE_CONFIDENCE = {
    BRIDGE_EXPLICIT: 1.0,
    BRIDGE_NAME_MATCH: 0.9,
    BRIDGE_DATASET_WIDE: 0.4,
}


class ColumnBridge:
    """Result of resolving one MLFeature down to concrete columns."""

    __slots__ = ("column_urn", "method", "confidence")

    def __init__(self, column_urn: str, method: str) -> None:
        self.column_urn = column_urn
        self.method = method
        self.confidence = _BRIDGE_CONFIDENCE[method]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ColumnBridge({self.column_urn!r}, {self.method}, {self.confidence})"


class MetadataGraph:
    """Indexed, directed metadata graph spanning warehouse columns and ML entities."""

    def __init__(self, snapshot: GraphSnapshot) -> None:
        self.snapshot = snapshot
        self.entities: dict[str, Entity] = snapshot.entities

        self._out: dict[str, list[LineageHop]] = defaultdict(list)
        self._in: dict[str, list[LineageHop]] = defaultdict(list)
        self._field_index: dict[str, tuple[str, SchemaField]] = {}
        self._dataset_of_column: dict[str, str] = {}
        self._bridge_cache: dict[str, list[ColumnBridge]] = {}

        self._index_fields()
        self._index_lineage()
        self._index_ml_edges()

    # ---------------------------------------------------------------------------------
    # Construction
    # ---------------------------------------------------------------------------------

    def _index_fields(self) -> None:
        for urn, entity in self.entities.items():
            for field in entity.fields:
                self._field_index[field.urn] = (urn, field)
                self._dataset_of_column[field.urn] = urn

    def _add(self, hop: LineageHop) -> None:
        self._out[hop.upstream].append(hop)
        self._in[hop.downstream].append(hop)

    def _index_lineage(self) -> None:
        for edge in self.snapshot.column_edges:
            self._add(
                LineageHop(
                    upstream=edge.upstream,
                    downstream=edge.downstream,
                    edge=EdgeKind.COLUMN_LINEAGE,
                    transform=edge.transform,
                    confidence=edge.confidence,
                    query=edge.query,
                )
            )
        for edge in self.snapshot.dataset_edges:
            self._add(
                LineageHop(
                    upstream=edge.upstream,
                    downstream=edge.downstream,
                    edge=EdgeKind.DATASET_LINEAGE,
                    transform=edge.transform,
                    confidence=edge.confidence,
                )
            )

    def _index_ml_edges(self) -> None:
        """Synthesise the ML half of the chain so one traversal spans the whole path.

        column -> MLFeature -> MLModel -> MLModelDeployment
        """
        for urn, entity in self.entities.items():
            if entity.removed:
                continue

            if entity.kind is NodeKind.ML_FEATURE:
                for bridge in self.feature_source_columns(urn):
                    self._add(
                        LineageHop(
                            upstream=bridge.column_urn,
                            downstream=urn,
                            edge=EdgeKind.FEATURE_SOURCE,
                            transform=f"feature materialisation ({bridge.method})",
                            confidence=bridge.confidence,
                        )
                    )

            elif entity.kind is NodeKind.ML_FEATURE_TABLE:
                for feature_urn in entity.ml_features + entity.ml_primary_keys:
                    self._add(
                        LineageHop(
                            upstream=feature_urn,
                            downstream=urn,
                            edge=EdgeKind.FEATURE_OF_TABLE,
                        )
                    )

            elif entity.kind is NodeKind.ML_MODEL:
                for feature_urn in entity.ml_features:
                    self._add(
                        LineageHop(
                            upstream=feature_urn,
                            downstream=urn,
                            edge=EdgeKind.MODEL_FEATURE,
                            transform="model input",
                        )
                    )
                for deployment_urn in entity.deployments:
                    self._add(
                        LineageHop(
                            upstream=urn,
                            downstream=deployment_urn,
                            edge=EdgeKind.MODEL_DEPLOYMENT,
                            transform="deployed as",
                        )
                    )
                for job_urn in entity.training_jobs:
                    self._add(
                        LineageHop(
                            upstream=job_urn,
                            downstream=urn,
                            edge=EdgeKind.TRAINING_JOB,
                            transform="trained by",
                        )
                    )

    # ---------------------------------------------------------------------------------
    # The ML bridge
    # ---------------------------------------------------------------------------------

    def feature_source_columns(self, feature_urn: str) -> list[ColumnBridge]:
        """Resolve an MLFeature to the warehouse columns it is actually computed from.

        Three tiers, best first:

        1. **explicit** -- the feature declares ``faultline.source_columns`` (or
           ``source_column``) in its custom properties. Full confidence.
        2. **name match** -- a column in one of the feature's source datasets shares the
           feature's name. This is how feature stores that materialise from a table behave
           in practice, so it covers the common case without configuration.
        3. **dataset wide** -- no column-level signal, so the feature is attributed to every
           column of its source datasets at low confidence. Findings derived solely from
           this tier are reported, but never at blocking severity.
        """
        if feature_urn in self._bridge_cache:
            return self._bridge_cache[feature_urn]

        feature = self.entities.get(feature_urn)
        if feature is None:
            self._bridge_cache[feature_urn] = []
            return []

        sources = [s for s in feature.feature_sources if s in self.entities]
        resolved: list[ColumnBridge] = []

        for token in self._explicit_source_columns(feature):
            for column_urn in self._resolve_column_token(token, sources):
                resolved.append(ColumnBridge(column_urn, BRIDGE_EXPLICIT))
        if resolved:
            self._bridge_cache[feature_urn] = resolved
            return resolved

        feature_name = self._feature_name(feature)
        if feature_name:
            for dataset_urn in sources:
                dataset = self.entities[dataset_urn]
                for field in dataset.fields:
                    if field.field_path.lower() == feature_name.lower():
                        resolved.append(ColumnBridge(field.urn, BRIDGE_NAME_MATCH))
            if resolved:
                self._bridge_cache[feature_urn] = resolved
                return resolved

        for dataset_urn in sources:
            dataset = self.entities[dataset_urn]
            for field in dataset.fields:
                resolved.append(ColumnBridge(field.urn, BRIDGE_DATASET_WIDE))

        self._bridge_cache[feature_urn] = resolved
        return resolved

    @staticmethod
    def _explicit_source_columns(feature: Entity) -> list[str]:
        """Tokens from an explicit declaration.

        Split on top-level commas only: a ``schemaField`` URN embeds its parent dataset URN
        and therefore contains commas of its own, so a naive ``split(",")`` shreds it.
        """
        for key in EXPLICIT_SOURCE_COLUMN_KEYS:
            raw = feature.custom_properties.get(key)
            if raw:
                return [part for part in (p.strip() for p in _split_top_level(raw)) if part]
        return []

    def _resolve_column_token(self, token: str, sources: list[str]) -> list[str]:
        """Accept either a full ``schemaField`` URN or a bare column name.

        Bare names are the ergonomic form a human writes; they are resolved against the
        feature's declared source datasets. Unknown tokens resolve to nothing rather than
        fabricating a column that is not in the graph.
        """
        if token.startswith("urn:li:schemaField:"):
            return [token] if token in self._field_index else []

        hits: list[str] = []
        for dataset_urn in sources:
            dataset = self.entities.get(dataset_urn)
            if not dataset:
                continue
            for field in dataset.fields:
                if field.field_path.lower() == token.lower():
                    hits.append(field.urn)
        return hits

    @staticmethod
    def _feature_name(feature: Entity) -> str | None:
        """The bare feature name from ``urn:li:mlFeature:(table,name)``."""
        if feature.name:
            return feature.name
        urn = feature.urn
        if urn.startswith("urn:li:mlFeature:") or urn.startswith("urn:li:mlPrimaryKey:"):
            inner = urn.split(":", 3)[-1].strip("()")
            parts = [p.strip() for p in inner.split(",")]
            if len(parts) >= 2:
                return parts[-1]
        return None

    def bridge_method_for(self, feature_urn: str) -> str | None:
        bridges = self.feature_source_columns(feature_urn)
        return bridges[0].method if bridges else None

    # ---------------------------------------------------------------------------------
    # Basic accessors
    # ---------------------------------------------------------------------------------

    def entity(self, urn: str) -> Entity | None:
        return self.entities.get(urn)

    def dataset_of_column(self, column_urn: str) -> str | None:
        return self._dataset_of_column.get(column_urn)

    def field_of_column(self, column_urn: str) -> SchemaField | None:
        hit = self._field_index.get(column_urn)
        return hit[1] if hit else None

    def by_kind(self, kind: NodeKind) -> Iterator[Entity]:
        for entity in self.entities.values():
            if entity.kind is kind and not entity.removed:
                yield entity

    def models(self) -> list[Entity]:
        return list(self.by_kind(NodeKind.ML_MODEL))

    def features(self) -> list[Entity]:
        return list(self.by_kind(NodeKind.ML_FEATURE))

    def datasets(self) -> list[Entity]:
        return list(self.by_kind(NodeKind.DATASET))

    def model_features(self, model_urn: str) -> list[Entity]:
        model = self.entities.get(model_urn)
        if not model:
            return []
        return [self.entities[u] for u in model.ml_features if u in self.entities]

    def upstream_hops(self, urn: str) -> list[LineageHop]:
        return self._in.get(urn, [])

    def downstream_hops(self, urn: str) -> list[LineageHop]:
        return self._out.get(urn, [])

    # ---------------------------------------------------------------------------------
    # Traversal
    # ---------------------------------------------------------------------------------

    def ancestors(
        self,
        urn: str,
        max_depth: int = 16,
        edge_filter: frozenset[EdgeKind] | None = None,
    ) -> dict[str, ProofPath]:
        """Every node upstream of ``urn``, mapped to a shortest proof path *to* ``urn``.

        Paths are returned oriented upstream -> downstream, i.e. the direction data flows,
        so they read naturally in a report.
        """
        return self._bfs(urn, direction="up", max_depth=max_depth, edge_filter=edge_filter)

    def descendants(
        self,
        urn: str,
        max_depth: int = 16,
        edge_filter: frozenset[EdgeKind] | None = None,
    ) -> dict[str, ProofPath]:
        """Every node downstream of ``urn``, mapped to a shortest proof path from ``urn``."""
        return self._bfs(urn, direction="down", max_depth=max_depth, edge_filter=edge_filter)

    def _bfs(
        self,
        start: str,
        direction: str,
        max_depth: int,
        edge_filter: frozenset[EdgeKind] | None,
    ) -> dict[str, ProofPath]:
        adjacency = self._in if direction == "up" else self._out
        seen: dict[str, ProofPath] = {}
        queue: deque[tuple[str, list[LineageHop], int]] = deque([(start, [], 0)])
        visited = {start}

        while queue:
            node, trail, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for hop in adjacency.get(node, []):
                if edge_filter is not None and hop.edge not in edge_filter:
                    continue
                nxt = hop.upstream if direction == "up" else hop.downstream
                if self._is_removed(nxt):
                    continue
                # Trails are always stored data-flow oriented (upstream first).
                new_trail = [hop, *trail] if direction == "up" else [*trail, hop]
                if nxt not in seen:
                    seen[nxt] = ProofPath(hops=new_trail)
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append((nxt, new_trail, depth + 1))
        return seen

    def paths_between(
        self,
        upstream_urn: str,
        downstream_urn: str,
        max_depth: int = 16,
        max_paths: int = 8,
        edge_filter: frozenset[EdgeKind] | None = None,
    ) -> list[ProofPath]:
        """All simple data-flow paths from ``upstream_urn`` down to ``downstream_urn``.

        This is the local equivalent of DataHub's ``get_lineage_paths_between`` MCP tool,
        extended to traverse the synthesised ML edges. Bounded in both depth and count so a
        pathological graph cannot stall a CI run.
        """
        if upstream_urn == downstream_urn:
            return []

        results: list[ProofPath] = []

        def walk(node: str, trail: list[LineageHop], on_path: set[str], depth: int) -> None:
            if len(results) >= max_paths or depth > max_depth:
                return
            for hop in self._out.get(node, []):
                if edge_filter is not None and hop.edge not in edge_filter:
                    continue
                nxt = hop.downstream
                if nxt in on_path or self._is_removed(nxt):
                    continue
                trail.append(hop)
                if nxt == downstream_urn:
                    results.append(ProofPath(hops=list(trail)))
                    if len(results) >= max_paths:
                        trail.pop()
                        return
                else:
                    on_path.add(nxt)
                    walk(nxt, trail, on_path, depth + 1)
                    on_path.discard(nxt)
                trail.pop()

        walk(upstream_urn, [], {upstream_urn}, 0)
        results.sort(key=lambda p: (p.length, -p.confidence))
        return results

    def reaches(self, upstream_urn: str, downstream_urn: str, max_depth: int = 16) -> bool:
        """Cheap reachability check that stops at the first hit."""
        if upstream_urn == downstream_urn:
            return False
        queue: deque[tuple[str, int]] = deque([(upstream_urn, 0)])
        visited = {upstream_urn}
        while queue:
            node, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for hop in self._out.get(node, []):
                nxt = hop.downstream
                if nxt == downstream_urn:
                    return True
                if nxt not in visited and not self._is_removed(nxt):
                    visited.add(nxt)
                    queue.append((nxt, depth + 1))
        return False

    def _is_removed(self, urn: str) -> bool:
        entity = self.entities.get(urn)
        return bool(entity and entity.removed)

    # ---------------------------------------------------------------------------------
    # ML-specific convenience traversals
    # ---------------------------------------------------------------------------------

    def model_source_columns(self, model_urn: str, max_depth: int = 16) -> dict[str, ProofPath]:
        """Every warehouse column that feeds ``model_urn``, with the proof for each."""
        ancestors = self.ancestors(model_urn, max_depth=max_depth)
        return {
            urn: path
            for urn, path in ancestors.items()
            if node_kind_of(urn) is NodeKind.COLUMN
        }

    def models_depending_on(self, column_urn: str, max_depth: int = 16) -> dict[str, ProofPath]:
        """Every model reachable downstream of a column -- the ML blast radius."""
        descendants = self.descendants(column_urn, max_depth=max_depth)
        return {
            urn: path
            for urn, path in descendants.items()
            if node_kind_of(urn) is NodeKind.ML_MODEL
        }

    def columns_with_term(self, term_urn: str) -> list[str]:
        """All column URNs carrying a glossary term (how labels and PII are marked)."""
        hits: list[str] = []
        for entity in self.entities.values():
            if entity.removed:
                continue
            for field in entity.fields:
                if term_urn in field.terms:
                    hits.append(field.urn)
        return hits

    def columns_with_any_term(self, term_urns: Iterable[str]) -> dict[str, list[str]]:
        """Map column URN -> matched terms, for a set of terms of interest."""
        wanted = set(term_urns)
        hits: dict[str, list[str]] = {}
        for entity in self.entities.values():
            if entity.removed:
                continue
            for field in entity.fields:
                matched = [t for t in field.terms if t in wanted]
                if matched:
                    hits[field.urn] = matched
        return hits

    def stats(self) -> dict[str, int]:
        base = self.snapshot.stats()
        base["synthesised_ml_edges"] = sum(
            1
            for hops in self._out.values()
            for h in hops
            if h.edge
            in {
                EdgeKind.FEATURE_SOURCE,
                EdgeKind.MODEL_FEATURE,
                EdgeKind.MODEL_DEPLOYMENT,
                EdgeKind.FEATURE_OF_TABLE,
                EdgeKind.TRAINING_JOB,
            }
        )
        return base
