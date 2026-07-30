"""Detector framework.

A detector is a deterministic graph algorithm. It receives an indexed
:class:`~faultline.graph.graph.MetadataGraph` and returns findings, each carrying the proof
paths that substantiate it. Detectors never call an LLM and never guess: if the graph does
not contain the evidence, the detector stays silent.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import TYPE_CHECKING

from ..config import DetectorSettings
from ..graph.entities import Entity
from ..graph.graph import MetadataGraph
from ..models import Finding, FindingKind, ProofPath, Severity

if TYPE_CHECKING:  # avoids a cycle: semantic imports base to register itself
    from .semantic import SemanticBaseline

#: Registry of detector classes, keyed by their ``name``.
REGISTRY: dict[str, type[Detector]] = {}


def register(cls: type[Detector]) -> type[Detector]:
    """Class decorator that adds a detector to the registry."""
    if cls.name in REGISTRY:
        raise ValueError(f"duplicate detector name: {cls.name}")
    REGISTRY[cls.name] = cls
    return cls


class Detector(ABC):
    """Base class for all structural detectors."""

    #: Stable identifier, used in config and reports.
    name: str = ""
    #: The defect class this detector proves.
    kind: FindingKind
    #: One-line description shown in ``faultline detectors``.
    description: str = ""

    @abstractmethod
    def run(
        self,
        graph: MetadataGraph,
        settings: DetectorSettings,
        baseline: SemanticBaseline | None = None,
    ) -> list[Finding]:
        """Return every finding this detector can prove from ``graph``.

        ``baseline`` is a previously captured snapshot of column semantics. Only
        change-detecting detectors use it; the rest accept and ignore it so the engine can
        invoke every detector uniformly.
        """

    # -- shared helpers ----------------------------------------------------------------

    @staticmethod
    def severity_for(
        base: Severity,
        model: Entity | None,
        proofs: Iterable[ProofPath],
        settings: DetectorSettings,
    ) -> Severity:
        """Scale a detector's base severity by blast radius and evidence quality.

        Two adjustments, in order:

        1. A model that is *not* deployed drops one severity step -- the same structural
           defect is an emergency under a live endpoint and a warning in a sandbox.
        2. If the only evidence is low-confidence bridging (Faultline could not resolve the
           feature to specific columns), severity is capped. Reporting a blocking failure on
           evidence we cannot substantiate at column level would be exactly the kind of
           false confidence this tool exists to remove.
        """
        severity = base

        if model is not None and not model.is_deployed():
            severity = _step_down(severity)

        confidence = max((p.confidence for p in proofs), default=1.0)
        if confidence < settings.low_confidence_threshold:
            cap = settings.low_confidence_max_severity
            if severity.rank > cap.rank:
                severity = cap

        return severity

    @staticmethod
    def label_columns(
        graph: MetadataGraph, model: Entity, settings: DetectorSettings
    ) -> dict[str, str]:
        """Resolve this model's prediction-target column(s).

        Two independent sources, both scoped to the model so that a ``Label`` term on some
        unrelated table cannot produce a spurious finding:

        * an explicit ``faultline.label_column`` custom property naming a column, and
        * any column carrying the configured label glossary term that is genuinely upstream
          of the model.

        Returns a mapping of column URN -> how it was identified.
        """
        found: dict[str, str] = {}
        source_columns = graph.model_source_columns(model.urn, max_depth=settings.max_depth)

        declared = model.custom_properties.get(settings.label_property)
        if declared:
            wanted = {name.strip().lower() for name in declared.split(",") if name.strip()}
            for column_urn in source_columns:
                field = graph.field_of_column(column_urn)
                if field and field.field_path.lower() in wanted:
                    found[column_urn] = "declared"
            # A declared label may sit in a table that feeds training without being a model
            # input itself -- search the whole graph as a fallback.
            if not found:
                for entity in graph.entities.values():
                    for field in entity.fields:
                        if field.field_path.lower() in wanted:
                            found[field.urn] = "declared"

        for column_urn in graph.columns_with_term(settings.label_term):
            if column_urn in source_columns or column_urn in found:
                found.setdefault(column_urn, "glossary_term")
            else:
                # Not upstream of the model by column lineage, but it may still be this
                # model's label if it lives in a dataset the model draws from.
                dataset = graph.dataset_of_column(column_urn)
                if dataset and any(
                    graph.dataset_of_column(c) == dataset for c in source_columns
                ):
                    found.setdefault(column_urn, "glossary_term_same_dataset")

        return found

    @staticmethod
    def trim(proofs: list[ProofPath], settings: DetectorSettings) -> list[ProofPath]:
        """Keep the shortest, highest-confidence proofs -- reports stay readable."""
        ordered = sorted(proofs, key=lambda p: (p.length, -p.confidence))
        return ordered[: settings.max_paths_per_finding]


def _step_down(severity: Severity) -> Severity:
    ladder = [Severity.INFO, Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
    index = ladder.index(severity)
    return ladder[max(0, index - 1)]


def active_detectors(settings: DetectorSettings) -> list[Detector]:
    """Instantiate the detectors enabled by configuration, in a deterministic order."""
    names = sorted(REGISTRY)
    if settings.enabled:
        names = [n for n in names if n in set(settings.enabled)]
    if settings.disabled:
        names = [n for n in names if n not in set(settings.disabled)]
    return [REGISTRY[n]() for n in names]
