"""Configuration, loaded from ``.faultline.yml`` with environment overrides.

Every threshold a detector uses lives here rather than inline, so a team can tune Faultline
to their own conventions (how they mark labels, which terms count as restricted, how deep to
traverse) without editing detector code.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from .models import Severity

DEFAULT_CONFIG_FILENAMES = (".faultline.yml", ".faultline.yaml", "faultline.yml")


class DataHubConfig(BaseModel):
    """Connection details for a DataHub GMS instance."""

    server: str = "http://localhost:8080"
    token: str | None = None
    timeout_seconds: float = 30.0
    max_retries: int = 3
    #: Verify TLS. Only disable against a local instance with a self-signed cert.
    verify_ssl: bool = True

    def resolved_token(self) -> str | None:
        return self.token or os.environ.get("DATAHUB_GMS_TOKEN")

    def resolved_server(self) -> str:
        return os.environ.get("DATAHUB_GMS_URL", self.server)


class DetectorSettings(BaseModel):
    """Knobs shared across detectors, plus per-detector specifics."""

    #: Glossary term marking a column as the prediction target.
    label_term: str = "urn:li:glossaryTerm:Label"
    #: Custom property on an MLModel naming its label column.
    label_property: str = "faultline.label_column"

    #: Terms/tags that mark a column as restricted (PII, PHI, regulated).
    restricted_terms: list[str] = Field(
        default_factory=lambda: [
            "urn:li:glossaryTerm:PII",
            "urn:li:glossaryTerm:PHI",
            "urn:li:glossaryTerm:Sensitive",
            "urn:li:glossaryTerm:Confidential",
        ]
    )
    restricted_tags: list[str] = Field(
        default_factory=lambda: ["urn:li:tag:PII", "urn:li:tag:Sensitive"]
    )

    #: How a feature declares which serving path it belongs to.
    serving_property: str = "faultline.serving"
    offline_markers: list[str] = Field(default_factory=lambda: ["offline", "batch", "training"])
    online_markers: list[str] = Field(default_factory=lambda: ["online", "realtime", "serving"])

    #: Traversal bounds. Deep enough for real warehouses, bounded enough for CI.
    max_depth: int = 16
    max_paths_per_finding: int = 4

    #: A finding whose proof rests only on low-confidence bridging is capped at this severity,
    #: because Faultline cannot prove column-level precision for it.
    low_confidence_threshold: float = 0.5
    low_confidence_max_severity: Severity = Severity.MEDIUM

    #: Detectors to run. Empty means all registered detectors.
    enabled: list[str] = Field(default_factory=list)
    disabled: list[str] = Field(default_factory=list)


class WritebackConfig(BaseModel):
    """What Faultline contributes back to the DataHub graph."""

    enabled: bool = True
    dry_run: bool = False

    tag_prefix: str = "Faultline"
    structured_property_namespace: str = "io.faultline"

    write_tags: bool = True
    write_structured_properties: bool = True
    write_documents: bool = True
    write_incidents: bool = True
    #: Record each scan as a DataProcessInstance so the graph carries an audit trail.
    write_run_audit: bool = True


class CIConfig(BaseModel):
    """How the CI gate decides pass/fail."""

    fail_on: Severity = Severity.HIGH
    #: Only fail on findings absent from the baseline file, so existing debt does not block.
    baseline_path: str | None = ".faultline-baseline.json"
    fail_on_new_only: bool = True
    post_pr_comment: bool = True


class AgentConfig(BaseModel):
    """The Claude-powered triage and explanation layer."""

    enabled: bool = True
    model: str = "claude-opus-5"
    effort: str = "high"
    max_tokens: int = 16000
    #: Path/command for the DataHub MCP server. The agent reads DataHub through this.
    mcp_command: str | None = "uvx"
    mcp_args: list[str] = Field(default_factory=lambda: ["mcp-server-datahub"])
    #: Fall back to a direct-graph agent if the MCP server cannot be reached.
    require_mcp: bool = False


class FaultlineConfig(BaseModel):
    """Top-level configuration."""

    datahub: DataHubConfig = Field(default_factory=DataHubConfig)
    detectors: DetectorSettings = Field(default_factory=DetectorSettings)
    writeback: WritebackConfig = Field(default_factory=WritebackConfig)
    ci: CIConfig = Field(default_factory=CIConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)

    #: Restrict a scan to models matching these URN substrings. Empty means all.
    model_filter: list[str] = Field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path | None = None) -> FaultlineConfig:
        """Load from an explicit path, a discovered config file, or defaults."""
        candidate = Path(path) if path else _discover_config()
        if candidate and candidate.exists():
            raw = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                raise ValueError(f"{candidate} must contain a YAML mapping at the top level")
            return cls.model_validate(raw)
        return cls()

    def to_yaml(self) -> str:
        return yaml.safe_dump(_jsonify(self.model_dump(mode="json")), sort_keys=False)


def _discover_config(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` looking for a config file, like git does for .gitignore."""
    current = (start or Path.cwd()).resolve()
    for directory in [current, *current.parents]:
        for name in DEFAULT_CONFIG_FILENAMES:
            candidate = directory / name
            if candidate.exists():
                return candidate
    return None


def _jsonify(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonify(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonify(v) for v in value]
    return value
