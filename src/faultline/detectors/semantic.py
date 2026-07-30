"""Silent semantic change: an upstream column's meaning shifts beneath a live model.

The dangerous change is not the one that breaks a query -- that fails loudly and gets fixed
within the hour. It is the one where the pipeline keeps running, the values stay in range,
and the meaning is different: a currency basis switched from USD to EUR, a duration column
that used to be milliseconds and is now seconds, an enum silently recoded.

Nothing downstream errors. The model keeps scoring. It is just wrong now.

This detector diffs the current graph against a captured baseline and reports changed
columns that can still reach a model. Deletions are deliberately out of scope: a dropped
column breaks jobs immediately and does not need a detector. The silent case is the column
that is still there and no longer means what it did.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from ..config import DetectorSettings
from ..graph.entities import SchemaField
from ..graph.graph import MetadataGraph
from ..models import Finding, FindingKind, Severity, short_urn
from .base import Detector, register

#: Tokens whose appearance or disappearance in a column's documentation implies the values
#: themselves changed meaning -- the highest-signal class of "silent" change.
UNIT_TOKENS = {
    "currency": {"usd", "eur", "gbp", "jpy", "inr", "cad", "aud", "chf", "cny", "brl"},
    "magnitude": {"cents", "dollars", "pennies", "thousands", "millions", "basis points", "bps"},
    "time": {"ms", "milliseconds", "seconds", "secs", "minutes", "hours", "days", "epoch"},
    "mass": {"kg", "kilograms", "lbs", "pounds", "grams", "oz", "ounces"},
    "distance": {"km", "kilometres", "kilometers", "miles", "metres", "meters", "feet"},
    "ratio": {"percent", "percentage", "fraction", "ratio", "proportion"},
}

_WORD = re.compile(r"[a-z]+")


def _tokens(text: str) -> set[str]:
    """Unigrams *and* adjacent bigrams from a description.

    Both are needed: unit names are usually single words (``usd``, ``ms``) but a few are
    two ("basis points"). A regex that greedily pairs adjacent words would tokenise
    "order total in usd" as {"order total", "in usd"} and never see ``usd`` at all.
    """
    words = _WORD.findall(text.lower())
    grams = set(words)
    grams.update(f"{a} {b}" for a, b in zip(words, words[1:], strict=False))
    return grams


class BaselineColumn(BaseModel):
    """What a column looked like when the baseline was captured."""

    signature: str
    native_type: str | None = None
    description: str | None = None
    nullable: bool = True
    terms: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class SemanticBaseline(BaseModel):
    """A point-in-time capture of every column's semantics."""

    columns: dict[str, BaselineColumn] = Field(default_factory=dict)
    captured_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    datahub_server: str | None = None

    @classmethod
    def capture(cls, graph: MetadataGraph, server: str | None = None) -> SemanticBaseline:
        columns: dict[str, BaselineColumn] = {}
        for entity in graph.entities.values():
            if entity.removed:
                continue
            for field in entity.fields:
                columns[field.urn] = BaselineColumn(
                    signature=field.semantic_signature(),
                    native_type=field.native_type,
                    description=field.description,
                    nullable=field.nullable,
                    terms=list(field.terms),
                    tags=list(field.tags),
                )
        return cls(columns=columns, datahub_server=server)


@register
class SilentSemanticChangeDetector(Detector):
    name = "silent-semantic-change"
    kind = FindingKind.SILENT_SEMANTIC_CHANGE
    description = "Diffs a baseline to find upstream meaning changes that reach a live model"

    def run(
        self,
        graph: MetadataGraph,
        settings: DetectorSettings,
        baseline: SemanticBaseline | None = None,
    ) -> list[Finding]:
        if baseline is None or not baseline.columns:
            # No baseline means no notion of "changed". Say nothing rather than guess.
            return []

        findings: list[Finding] = []

        for column_urn, before in baseline.columns.items():
            field = graph.field_of_column(column_urn)
            if field is None:
                continue  # removed: loud by nature, out of scope (see module docstring)
            if field.semantic_signature() == before.signature:
                continue

            affected = graph.models_depending_on(column_urn, max_depth=settings.max_depth)
            if not affected:
                continue  # a change nothing consumes is not a risk worth paging anyone about

            change_kind, base_severity, detail = self._classify(before, field)

            for model_urn, path in sorted(affected.items()):
                model = graph.entity(model_urn)
                proofs = self.trim([path], settings)
                findings.append(
                    Finding(
                        kind=self.kind,
                        severity=self.severity_for(base_severity, model, proofs, settings),
                        title=(
                            f"`{short_urn(column_urn)}` changed ({change_kind}) beneath "
                            f"`{short_urn(model_urn)}`"
                        ),
                        summary=(
                            f"{detail} Model `{short_urn(model_urn)}` depends on this column "
                            f"through {path.length} lineage hop(s). The pipeline did not fail "
                            f"and the values remain plausible, so nothing else will flag this."
                        ),
                        model_urn=model_urn,
                        column_urn=column_urn,
                        dataset_urn=graph.dataset_of_column(column_urn),
                        proofs=proofs,
                        evidence={
                            "change_kind": change_kind,
                            "before": before.model_dump(exclude={"signature"}),
                            "after": {
                                "native_type": field.native_type,
                                "description": field.description,
                                "nullable": field.nullable,
                                "terms": field.terms,
                                "tags": field.tags,
                            },
                            "baseline_captured_at": baseline.captured_at,
                            "model_deployed": bool(model and model.is_deployed()),
                        },
                        remediation=(
                            f"Confirm whether `{short_urn(model_urn)}` was retrained after this "
                            f"change. If not, its training distribution no longer matches its "
                            f"inputs -- retrain, or revert the upstream change."
                        ),
                    )
                )

        return findings

    # -- change classification ------------------------------------------------------------

    def _classify(
        self, before: BaselineColumn, after: SchemaField
    ) -> tuple[str, Severity, str]:
        """Name the change, rank it, and describe it in one sentence."""
        if before.native_type != after.native_type:
            return (
                "type change",
                Severity.HIGH,
                f"Column type changed from `{before.native_type}` to `{after.native_type}`.",
            )

        unit_shift = self._unit_shift(before.description, after.description)
        if unit_shift:
            category, lost, gained = unit_shift
            return (
                f"{category} change",
                Severity.CRITICAL,
                (
                    f"The documented {category} changed from `{lost or 'unspecified'}` to "
                    f"`{gained or 'unspecified'}`. Values in this column now mean something "
                    f"different while remaining numerically plausible."
                ),
            )

        if before.nullable != after.nullable:
            direction = "now nullable" if after.nullable else "no longer nullable"
            return (
                "nullability change",
                Severity.MEDIUM,
                f"Column is {direction}.",
            )

        if set(before.terms) != set(after.terms):
            added = sorted(set(after.terms) - set(before.terms))
            removed = sorted(set(before.terms) - set(after.terms))
            return (
                "governance change",
                Severity.MEDIUM,
                (
                    f"Glossary terms changed (added: {[short_urn(t) for t in added] or 'none'}, "
                    f"removed: {[short_urn(t) for t in removed] or 'none'})."
                ),
            )

        if set(before.tags) != set(after.tags):
            return ("tag change", Severity.LOW, "Tags on the column changed.")

        return (
            "documentation change",
            Severity.LOW,
            (
                f"Documentation changed from `{(before.description or '')[:60]}` to "
                f"`{(after.description or '')[:60]}`."
            ),
        )

    @staticmethod
    def _unit_shift(
        before: str | None, after: str | None
    ) -> tuple[str, str | None, str | None] | None:
        """Detect a unit or currency swap between two descriptions.

        A description edit is usually a harmless clarification. The exception is when the
        *unit* named in it changes -- that says the numbers themselves changed basis, which
        is the single most damaging silent change a model can inherit.
        """
        b_words = _tokens(before or "")
        a_words = _tokens(after or "")

        for category, tokens in UNIT_TOKENS.items():
            b_hit = b_words & tokens
            a_hit = a_words & tokens
            if b_hit != a_hit and (b_hit or a_hit):
                return (
                    category,
                    ", ".join(sorted(b_hit)) or None,
                    ", ".join(sorted(a_hit)) or None,
                )
        return None
