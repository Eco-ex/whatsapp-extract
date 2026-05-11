"""Load and validate the per-project YAML config."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class FieldDef:
    name: str
    type: str
    required: bool = False
    description: str = ""
    default: Any = None


@dataclass
class SchemaDef:
    fields: list[FieldDef]

    @property
    def required(self) -> list[str]:
        return [f.name for f in self.fields if f.required]

    @property
    def names(self) -> list[str]:
        return [f.name for f in self.fields]


@dataclass
class UrlRules:
    disposable_patterns: list[str] = field(default_factory=list)
    profile_patterns: list[str] = field(default_factory=list)


@dataclass
class FilterRules:
    min_message_length: int = 15
    system_patterns: list[str] = field(default_factory=list)
    media_patterns: list[str] = field(default_factory=list)
    url_rules: UrlRules = field(default_factory=UrlRules)
    custom_discard_patterns: list[str] = field(default_factory=list)


@dataclass
class LlmSettings:
    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 4096
    batch_size: int = 80
    batch_overlap: int = 5
    delay_between_calls: float = 1.5
    max_retries: int = 3


@dataclass
class ConsolidationSettings:
    dedup_field: str = "name"
    dedup_threshold: int = 85
    dedup_strategy: str = "earliest"
    sort_by: list[str] = field(default_factory=lambda: ["timestamp"])
    output_formats: list[str] = field(default_factory=lambda: ["csv", "json"])


@dataclass
class Config:
    project_name: str
    description: str
    config_path: Path
    config_dir: Path
    training_data_path: Path
    output_dir: Path
    data_dir: Path
    schema: SchemaDef
    categories: list[str]
    partner_types: list[str]
    tags: list[str]
    filters: FilterRules
    llm: LlmSettings
    consolidation: ConsolidationSettings
    raw: dict[str, Any]

    # Convenience subdirectories under data_dir
    @property
    def parsed_dir(self) -> Path:
        return self.data_dir / "parsed"

    @property
    def filtered_dir(self) -> Path:
        return self.data_dir / "filtered"

    @property
    def batches_dir(self) -> Path:
        return self.data_dir / "batches"

    @property
    def results_dir(self) -> Path:
        return self.data_dir / "results"


def _resolve(path_str: str, base: Path) -> Path:
    p = Path(path_str)
    if not p.is_absolute():
        p = (base / p).resolve()
    return p


def _parse_fields(raw_fields: list[dict[str, Any]]) -> list[FieldDef]:
    parsed: list[FieldDef] = []
    for f in raw_fields:
        if "name" not in f or "type" not in f:
            raise ValueError(f"Schema field missing 'name' or 'type': {f}")
        parsed.append(
            FieldDef(
                name=f["name"],
                type=f["type"],
                required=bool(f.get("required", False)),
                description=f.get("description", ""),
                default=f.get("default"),
            )
        )
    return parsed


def load_config(config_path: str | Path) -> Config:
    """Load and validate a config.yaml. All relative paths are resolved
    relative to the config file's directory."""
    config_path = Path(config_path).resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    config_dir = config_path.parent

    # Required top-level
    project_name = raw.get("project_name")
    if not project_name:
        raise ValueError("Config must define 'project_name'.")

    schema_raw = raw.get("schema") or {}
    fields_raw = schema_raw.get("fields") or []
    if not fields_raw:
        raise ValueError("Config schema must define at least one field.")
    schema = SchemaDef(fields=_parse_fields(fields_raw))
    if not schema.required:
        raise ValueError("Config schema must have at least one required field.")
    if "name" not in schema.names:
        raise ValueError("Config schema must include a 'name' field.")

    training_data_raw = raw.get("training_data")
    if not training_data_raw:
        raise ValueError("Config must define 'training_data' (path to TRAINING_DATA.md).")
    training_data_path = _resolve(training_data_raw, config_dir)
    if not training_data_path.exists():
        raise FileNotFoundError(f"Training data file not found: {training_data_path}")

    output_dir = _resolve(raw.get("output_dir", "../../output"), config_dir)
    data_dir = _resolve(raw.get("data_dir", "../../data"), config_dir)

    # Filters
    filters_raw = raw.get("filters") or {}
    url_raw = filters_raw.get("url_rules") or {}
    filters = FilterRules(
        min_message_length=int(filters_raw.get("min_message_length", 15)),
        system_patterns=list(filters_raw.get("system_patterns") or []),
        media_patterns=list(filters_raw.get("media_patterns") or []),
        url_rules=UrlRules(
            disposable_patterns=list(url_raw.get("disposable_patterns") or []),
            profile_patterns=list(url_raw.get("profile_patterns") or []),
        ),
        custom_discard_patterns=list(filters_raw.get("custom_discard_patterns") or []),
    )

    # LLM
    llm_raw = raw.get("llm") or {}
    llm = LlmSettings(
        model=llm_raw.get("model", LlmSettings.model),
        max_tokens=int(llm_raw.get("max_tokens", LlmSettings.max_tokens)),
        batch_size=int(llm_raw.get("batch_size", LlmSettings.batch_size)),
        batch_overlap=int(llm_raw.get("batch_overlap", LlmSettings.batch_overlap)),
        delay_between_calls=float(llm_raw.get("delay_between_calls", LlmSettings.delay_between_calls)),
        max_retries=int(llm_raw.get("max_retries", LlmSettings.max_retries)),
    )

    # Consolidation
    cons_raw = raw.get("consolidation") or {}
    consolidation = ConsolidationSettings(
        dedup_field=cons_raw.get("dedup_field", ConsolidationSettings.dedup_field),
        dedup_threshold=int(cons_raw.get("dedup_threshold", ConsolidationSettings.dedup_threshold)),
        dedup_strategy=cons_raw.get("dedup_strategy", ConsolidationSettings.dedup_strategy),
        sort_by=list(cons_raw.get("sort_by") or ["timestamp"]),
        output_formats=list(cons_raw.get("output_formats") or ["csv", "json"]),
    )
    if consolidation.dedup_field not in schema.names:
        raise ValueError(
            f"consolidation.dedup_field '{consolidation.dedup_field}' is not in schema fields."
        )
    if consolidation.dedup_strategy not in {"earliest", "latest", "most_complete"}:
        raise ValueError(
            f"Invalid dedup_strategy: {consolidation.dedup_strategy} "
            "(must be earliest, latest, or most_complete)."
        )

    categories = list(raw.get("categories") or [])
    partner_types = list(raw.get("partner_types") or [])
    tags = list(raw.get("tags") or [])

    return Config(
        project_name=project_name,
        description=raw.get("description", ""),
        config_path=config_path,
        config_dir=config_dir,
        training_data_path=training_data_path,
        output_dir=output_dir,
        data_dir=data_dir,
        schema=schema,
        categories=categories,
        partner_types=partner_types,
        tags=tags,
        filters=filters,
        llm=llm,
        consolidation=consolidation,
        raw=raw,
    )
