"""Domain model for Faultline.

The central idea: a Faultline finding is *not* an LLM opinion. Every finding carries a
:class:`ProofPath` -- an ordered chain of lineage edges, each of which exists in the DataHub
graph. A finding can therefore be re-verified against the graph at any later time, and a
reviewer can read the proof without trusting the tool.

Detectors are deterministic graph algorithms. The agent layer explains and triages what the
detectors prove; it never invents a finding.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_serializer

# --------------------------------------------------------------------------------------
# Severity
# --------------------------------------------------------------------------------------


class Severity(str, Enum):
    """Ordered severity. ``CRITICAL`` and ``HIGH`` block a PR by default."""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]

    def __lt__(self, other: object) -> bool:  # type: ignore[override]
        if not isinstance(other, Severity):
            return NotImplemented
        return self.rank < other.rank

    def __le__(self, other: object) -> bool:  # type: ignore[override]
        if not isinstance(other, Severity):
            return NotImplemented
        return self.rank <= other.rank


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}

#: Severities that fail a CI run unless explicitly baselined.
BLOCKING_SEVERITIES: frozenset[Severity] = frozenset({Severity.CRITICAL, Severity.HIGH})


class FindingKind(str, Enum):
    """The structural defect classes Faultline can prove from the graph alone."""

    TARGET_LEAKAGE = "TARGET_LEAKAGE"
    TRAIN_SERVE_SKEW = "TRAIN_SERVE_SKEW"
    SILENT_SEMANTIC_CHANGE = "SILENT_SEMANTIC_CHANGE"
    COMPLIANCE_PROPAGATION = "COMPLIANCE_PROPAGATION"
    ORPHANED_FEATURE = "ORPHANED_FEATURE"
    UNGOVERNED_UPSTREAM = "UNGOVERNED_UPSTREAM"


#: Human-facing one-liners, used in reports and the PR comment.
KIND_HEADLINES: dict[FindingKind, str] = {
    FindingKind.TARGET_LEAKAGE: "Feature derives from the prediction target",
    FindingKind.TRAIN_SERVE_SKEW: "Offline and online feature paths have diverged",
    FindingKind.SILENT_SEMANTIC_CHANGE: "Upstream semantics changed beneath a live model",
    FindingKind.COMPLIANCE_PROPAGATION: "Restricted data reaches a deployed model",
    FindingKind.ORPHANED_FEATURE: "Feature has no resolvable upstream source",
    FindingKind.UNGOVERNED_UPSTREAM: "Model depends on an unowned, untested source",
}


# --------------------------------------------------------------------------------------
# Proof paths
# --------------------------------------------------------------------------------------


class NodeKind(str, Enum):
    COLUMN = "COLUMN"
    DATASET = "DATASET"
    ML_FEATURE = "ML_FEATURE"
    ML_FEATURE_TABLE = "ML_FEATURE_TABLE"
    ML_MODEL = "ML_MODEL"
    ML_MODEL_GROUP = "ML_MODEL_GROUP"
    ML_MODEL_DEPLOYMENT = "ML_MODEL_DEPLOYMENT"
    DATA_JOB = "DATA_JOB"
    UNKNOWN = "UNKNOWN"


class EdgeKind(str, Enum):
    """How two nodes are connected in the DataHub graph.

    ``COLUMN_LINEAGE`` edges come from ``FineGrainedLineage``; the ``FEATURE_SOURCE`` and
    ``MODEL_FEATURE`` edges are the ML-entity links that Faultline bridges to column level.
    """

    COLUMN_LINEAGE = "COLUMN_LINEAGE"
    DATASET_LINEAGE = "DATASET_LINEAGE"
    COLUMN_OF = "COLUMN_OF"
    FEATURE_SOURCE = "FEATURE_SOURCE"
    FEATURE_OF_TABLE = "FEATURE_OF_TABLE"
    MODEL_FEATURE = "MODEL_FEATURE"
    MODEL_DEPLOYMENT = "MODEL_DEPLOYMENT"
    TRAINING_JOB = "TRAINING_JOB"


class LineageHop(BaseModel):
    """A single, individually verifiable edge of a proof path."""

    model_config = {"frozen": True}

    upstream: str
    downstream: str
    edge: EdgeKind
    transform: str | None = None
    confidence: float = 1.0
    query: str | None = None

    def describe(self) -> str:
        op = f" [{self.transform}]" if self.transform else ""
        return f"{short_urn(self.upstream)} -> {short_urn(self.downstream)}{op}"


#: Transforms that copy a value without changing it. Compared case-insensitively after
#: stripping punctuation, so "Identity", "identity()" and "IDENTITY" all match.
VALUE_PRESERVING_TRANSFORMS = frozenset(
    {"identity", "passthrough", "pass through", "copy", "select", "alias", "rename", "as is"}
)


def _is_value_preserving(hop: LineageHop) -> bool:
    """True when a hop moves a value without altering it.

    Only edges *within* the warehouse qualify. Structural edges (a column becoming a
    feature, a feature becoming a model input) are never dropped: they carry the shape of
    the ML chain, and eliding them would let genuinely different topologies compare equal.
    """
    if hop.edge is not EdgeKind.COLUMN_LINEAGE:
        return False
    if not hop.transform:
        return False
    normalised = hop.transform.strip().lower().rstrip("()").replace("_", " ")
    return normalised in VALUE_PRESERVING_TRANSFORMS


class ProofPath(BaseModel):
    """An ordered chain of hops. ``hops[i].downstream == hops[i+1].upstream`` always holds."""

    hops: list[LineageHop] = Field(default_factory=list)

    @property
    def nodes(self) -> list[str]:
        if not self.hops:
            return []
        return [self.hops[0].upstream] + [h.downstream for h in self.hops]

    @property
    def origin(self) -> str | None:
        return self.hops[0].upstream if self.hops else None

    @property
    def terminus(self) -> str | None:
        return self.hops[-1].downstream if self.hops else None

    @property
    def length(self) -> int:
        return len(self.hops)

    @property
    def confidence(self) -> float:
        """Weakest-link confidence across the chain."""
        return min((h.confidence for h in self.hops), default=1.0)

    def is_contiguous(self) -> bool:
        """Structural invariant: the chain must actually connect end to end."""
        return all(a.downstream == b.upstream for a, b in zip(self.hops, self.hops[1:], strict=False))

    def transforms(self) -> list[str]:
        return [h.transform for h in self.hops if h.transform]

    def signature(self) -> str:
        """Order-sensitive fingerprint of *how* the terminus is derived.

        Two normalisations make this comparable across paths, and both are load-bearing:

        * **Node identity is excluded.** A feature's offline and online copies necessarily
          live in different tables, so including node names would make every pair look
          divergent and skew detection would fire on everything.
        * **Value-preserving hops are dropped.** A column that is passed through a staging
          layer unchanged is the same value; one path routing through an extra mart while
          the other reads straight from staging is a difference in *plumbing*, not in
          meaning. Counting those would drown the real divergences in noise.

        What remains is the ordered chain of operations that actually transform the value.
        """
        parts: list[str] = []
        for hop in self.hops:
            if _is_value_preserving(hop):
                continue
            if hop.edge is EdgeKind.COLUMN_LINEAGE:
                parts.append(f"{hop.edge.value}:{hop.transform or '~'}")
            else:
                # Structural edges contribute their kind only. Their transform text is
                # Faultline's own description of *how it resolved the link* (e.g. which
                # bridge tier matched), which says nothing about the pipeline and must not
                # make two otherwise-identical derivations compare unequal.
                parts.append(hop.edge.value)
        return "|".join(parts)

    def node_signature(self) -> str:
        """Identity of the nodes traversed -- used where the exact route matters."""
        return "|".join(short_urn(n) for n in self.nodes)

    def render(self, indent: str = "  ") -> str:
        """Human-readable proof, as it appears in PR comments and the terminal."""
        if not self.hops:
            return f"{indent}(empty path)"
        lines = [f"{indent}{short_urn(self.hops[0].upstream)}"]
        for hop in self.hops:
            arrow = f"{indent}  ↓"
            if hop.transform:
                arrow += f"  {hop.transform}"
            if hop.confidence < 1.0:
                arrow += f"  (confidence {hop.confidence:.2f})"
            lines.append(arrow)
            lines.append(f"{indent}{short_urn(hop.downstream)}")
        return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------------------


class Finding(BaseModel):
    """One proven structural defect."""

    kind: FindingKind
    severity: Severity
    title: str
    summary: str

    # Primary subjects. Not every kind populates every field.
    model_urn: str | None = None
    feature_urn: str | None = None
    dataset_urn: str | None = None
    column_urn: str | None = None

    proofs: list[ProofPath] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    remediation: str | None = None

    #: Populated by the agent layer. Never used to create or suppress a finding.
    narrative: str | None = None

    detected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_serializer("detected_at")
    def _ser_dt(self, dt: datetime, _info: Any) -> str:
        return dt.astimezone(timezone.utc).isoformat()

    @property
    def fingerprint(self) -> str:
        """Stable identity across runs, so CI can distinguish new findings from known ones.

        Deliberately excludes severity, prose and timestamps: re-wording a summary or
        re-ranking severity must not resurrect a finding the team already baselined.
        """
        basis = "␟".join(
            [
                self.kind.value,
                self.model_urn or "",
                self.feature_urn or "",
                self.dataset_urn or "",
                self.column_urn or "",
                ";".join(sorted(f"{p.signature()}@{p.node_signature()}" for p in self.proofs)),
            ]
        )
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]

    @property
    def headline(self) -> str:
        return KIND_HEADLINES.get(self.kind, self.kind.value)

    @property
    def subject_urn(self) -> str | None:
        """The entity a reviewer should look at first."""
        return self.model_urn or self.feature_urn or self.dataset_urn or self.column_urn

    def is_blocking(self) -> bool:
        return self.severity in BLOCKING_SEVERITIES

    def validate_proofs(self) -> list[str]:
        """Self-check the structural invariants of the attached proofs.

        Returns a list of problems; empty means every proof is well-formed. A finding whose
        proof does not hold together is a bug in a detector, and the CLI surfaces it loudly
        rather than reporting a defect we cannot substantiate.
        """
        problems: list[str] = []
        for i, proof in enumerate(self.proofs):
            if not proof.hops:
                problems.append(f"proof[{i}] is empty")
            elif not proof.is_contiguous():
                problems.append(f"proof[{i}] is not contiguous")
        return problems


class ScanResult(BaseModel):
    """Everything one Faultline run produced."""

    findings: list[Finding] = Field(default_factory=list)
    scanned_models: list[str] = Field(default_factory=list)
    scanned_features: list[str] = Field(default_factory=list)
    detectors_run: list[str] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    graph_source: str = "live"
    stats: dict[str, Any] = Field(default_factory=dict)

    @field_serializer("started_at", "finished_at")
    def _ser_dt(self, dt: datetime | None, _info: Any) -> str | None:
        return dt.astimezone(timezone.utc).isoformat() if dt else None

    @property
    def duration_seconds(self) -> float | None:
        if not self.finished_at:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    def by_severity(self, severity: Severity) -> list[Finding]:
        return [f for f in self.findings if f.severity is severity]

    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.is_blocking()]

    def sorted_findings(self) -> list[Finding]:
        """Most severe first; ties broken by kind then title for deterministic output."""
        return sorted(
            self.findings,
            key=lambda f: (-f.severity.rank, f.kind.value, f.title),
        )

    def severity_counts(self) -> dict[str, int]:
        counts = {s.value: 0 for s in Severity}
        for f in self.findings:
            counts[f.severity.value] += 1
        return counts

    def fingerprints(self) -> set[str]:
        return {f.fingerprint for f in self.findings}

    def new_since(self, baseline: Iterable[str]) -> list[Finding]:
        """Findings absent from a baseline fingerprint set -- what CI actually gates on."""
        known = set(baseline)
        return [f for f in self.findings if f.fingerprint not in known]


# --------------------------------------------------------------------------------------
# URN display helpers
# --------------------------------------------------------------------------------------


def short_urn(urn: str) -> str:
    """Compact a DataHub URN down to something readable in a PR comment.

    ``urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:duckdb,analytics.orders,PROD),amount)``
    becomes ``analytics.orders.amount``.
    """
    if not urn or not urn.startswith("urn:li:"):
        return urn

    try:
        if urn.startswith("urn:li:schemaField:"):
            inner = urn[len("urn:li:schemaField:") :].strip("()")
            dataset_part, _, field = inner.rpartition(",")
            return f"{short_urn(dataset_part.strip())}.{field.strip()}"

        if urn.startswith("urn:li:dataset:"):
            inner = urn[len("urn:li:dataset:") :].strip("()")
            parts = _split_top_level(inner)
            if len(parts) >= 2:
                return parts[1]

        if urn.startswith("urn:li:mlFeature:"):
            inner = urn[len("urn:li:mlFeature:") :].strip("()")
            parts = _split_top_level(inner)
            return ".".join(parts) if parts else inner

        if urn.startswith("urn:li:mlPrimaryKey:"):
            inner = urn[len("urn:li:mlPrimaryKey:") :].strip("()")
            parts = _split_top_level(inner)
            return ".".join(parts) if parts else inner

        for prefix in ("urn:li:mlModel:", "urn:li:mlModelGroup:", "urn:li:mlModelDeployment:",
                       "urn:li:mlFeatureTable:"):
            if urn.startswith(prefix):
                inner = urn[len(prefix) :].strip("()")
                parts = _split_top_level(inner)
                return parts[1] if len(parts) >= 2 else inner

        if urn.startswith("urn:li:glossaryTerm:"):
            return urn[len("urn:li:glossaryTerm:") :]
        if urn.startswith("urn:li:tag:"):
            return urn[len("urn:li:tag:") :]
    except Exception:  # pragma: no cover - display helper must never break a scan
        return urn

    return urn


def _split_top_level(text: str) -> list[str]:
    """Split on commas that are not nested inside parentheses."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in text:
        if ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current).strip())
    return parts


def node_kind_of(urn: str) -> NodeKind:
    """Classify a URN so traversal and rendering can branch on entity type."""
    mapping = {
        "urn:li:schemaField:": NodeKind.COLUMN,
        "urn:li:dataset:": NodeKind.DATASET,
        "urn:li:mlFeature:": NodeKind.ML_FEATURE,
        "urn:li:mlPrimaryKey:": NodeKind.ML_FEATURE,
        "urn:li:mlFeatureTable:": NodeKind.ML_FEATURE_TABLE,
        "urn:li:mlModelDeployment:": NodeKind.ML_MODEL_DEPLOYMENT,
        "urn:li:mlModelGroup:": NodeKind.ML_MODEL_GROUP,
        "urn:li:mlModel:": NodeKind.ML_MODEL,
        "urn:li:dataJob:": NodeKind.DATA_JOB,
    }
    for prefix, kind in mapping.items():
        if urn.startswith(prefix):
            return kind
    return NodeKind.UNKNOWN
