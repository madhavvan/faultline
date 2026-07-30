"""Entity records materialised out of DataHub aspects.

These are deliberately *lossy* projections of DataHub's aspect model: Faultline only keeps
what a structural detector needs. Keeping them plain and serialisable is what makes the
replay fixtures (a captured graph on disk) byte-identical to a live load.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..models import NodeKind, node_kind_of


class SchemaField(BaseModel):
    """One column of a dataset."""

    field_path: str
    urn: str
    native_type: str | None = None
    type_class: str | None = None
    description: str | None = None
    nullable: bool = True
    tags: list[str] = Field(default_factory=list)
    terms: list[str] = Field(default_factory=list)

    def has_term(self, term_urn: str) -> bool:
        return term_urn in self.terms

    def semantic_signature(self) -> str:
        """What a *silent* change alters.

        Two versions of a column that agree on this string are semantically equivalent as
        far as a downstream model is concerned. Type, nullability, documented meaning and
        governance labels all change behaviour; ordinal position does not.

        ``type_class`` is deliberately excluded. It is a *derived* label whose spelling
        depends on where the column was read from -- dbt's catalogue reports ``BIGINT`` while
        DataHub reports ``NumberType`` for the very same column. Including it made every
        column look changed the moment a baseline captured from one source was compared
        against a graph read from the other. ``native_type`` is the authoritative value and
        survives the round trip intact.
        """
        return "␟".join(
            [
                (self.native_type or "").strip().upper(),
                "null" if self.nullable else "notnull",
                (self.description or "").strip().lower(),
                ",".join(sorted(self.terms)),
                ",".join(sorted(self.tags)),
            ]
        )


class Entity(BaseModel):
    """A node in the metadata graph."""

    urn: str
    kind: NodeKind = NodeKind.UNKNOWN
    name: str | None = None
    platform: str | None = None
    description: str | None = None

    tags: list[str] = Field(default_factory=list)
    terms: list[str] = Field(default_factory=list)
    owners: list[str] = Field(default_factory=list)
    domain: str | None = None

    custom_properties: dict[str, str] = Field(default_factory=dict)
    structured_properties: dict[str, list[Any]] = Field(default_factory=dict)

    # Datasets
    fields: list[SchemaField] = Field(default_factory=list)

    # ML entities -- mirrors MLModelProperties / MLFeatureProperties / MLFeatureTableProperties
    ml_features: list[str] = Field(default_factory=list)
    ml_primary_keys: list[str] = Field(default_factory=list)
    feature_sources: list[str] = Field(default_factory=list)
    deployments: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    training_jobs: list[str] = Field(default_factory=list)
    downstream_jobs: list[str] = Field(default_factory=list)
    hyper_parameters: dict[str, Any] = Field(default_factory=dict)
    training_metrics: dict[str, Any] = Field(default_factory=dict)
    online_metrics: dict[str, Any] = Field(default_factory=dict)

    #: Set when the entity is removed in DataHub; such nodes are excluded from traversal.
    removed: bool = False

    def model_post_init(self, __context: Any) -> None:
        if self.kind is NodeKind.UNKNOWN:
            object.__setattr__(self, "kind", node_kind_of(self.urn))

    # -- convenience ------------------------------------------------------------------

    def field(self, field_path: str) -> SchemaField | None:
        for f in self.fields:
            if f.field_path == field_path:
                return f
        return None

    def field_by_urn(self, urn: str) -> SchemaField | None:
        for f in self.fields:
            if f.urn == urn:
                return f
        return None

    def has_tag(self, tag_urn: str) -> bool:
        return tag_urn in self.tags

    def has_term(self, term_urn: str) -> bool:
        return term_urn in self.terms

    def prop(self, key: str, default: str | None = None) -> str | None:
        return self.custom_properties.get(key, default)

    def is_deployed(self) -> bool:
        """A model is 'in production' if it has at least one deployment or a PROD tag.

        Severity keys off this: the same structural defect is CRITICAL under a live
        endpoint and MEDIUM in a sandbox.
        """
        if self.deployments:
            return True
        env = (self.prop("env") or self.prop("environment") or "").upper()
        return env in {"PROD", "PRODUCTION"}


class LineageEdge(BaseModel):
    """A directed lineage edge, upstream -> downstream."""

    model_config = {"frozen": True}

    upstream: str
    downstream: str
    transform: str | None = None
    confidence: float = 1.0
    query: str | None = None


class GraphSnapshot(BaseModel):
    """A complete, serialisable capture of everything Faultline needs.

    This is exactly what gets written to a replay fixture, which is why the live path and
    the offline path cannot drift: both produce this object and detectors only ever see it.
    """

    entities: dict[str, Entity] = Field(default_factory=dict)
    column_edges: list[LineageEdge] = Field(default_factory=list)
    dataset_edges: list[LineageEdge] = Field(default_factory=list)

    captured_at: str | None = None
    source: str = "live"
    #: What this graph was when it was captured. A replayed fixture reports
    #: source="replay:<file>" but keeps its origin, so a report never loses the fact
    #: that the lineage was parsed from a real dbt run.
    origin: str | None = None
    datahub_version: str | None = None

    def stats(self) -> dict[str, int]:
        kinds: dict[str, int] = {}
        for e in self.entities.values():
            kinds[e.kind.value] = kinds.get(e.kind.value, 0) + 1
        return {
            "entities": len(self.entities),
            "column_edges": len(self.column_edges),
            "dataset_edges": len(self.dataset_edges),
            **kinds,
        }
