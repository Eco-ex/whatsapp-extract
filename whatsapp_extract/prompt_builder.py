"""Build the system prompt by combining the config schema with the training data file."""

from __future__ import annotations

from .config_loader import Config


def _schema_table(cfg: Config) -> str:
    lines = [
        "| Field | Type | Required | Description |",
        "|-------|------|----------|-------------|",
    ]
    for f in cfg.schema.fields:
        req = "yes" if f.required else "no"
        desc = f.description.replace("|", "\\|")
        lines.append(f"| `{f.name}` | `{f.type}` | {req} | {desc} |")
    return "\n".join(lines)


def _categories_block(cfg: Config) -> str:
    if not cfg.categories:
        return "(No restricted category list — use a sensible category string.)"
    return "\n".join(f"- {c}" for c in cfg.categories)


def _partner_types_block(cfg: Config) -> str:
    if not cfg.partner_types:
        return "(No restricted partner_type list.)"
    return "\n".join(f"- {p}" for p in cfg.partner_types)


def _tags_block(cfg: Config) -> str:
    if not cfg.tags:
        return "(No preferred tag vocabulary — use freeform descriptors.)"
    return "\n".join(f"- {t}" for t in cfg.tags)


def build_system_prompt(cfg: Config) -> str:
    training = cfg.training_data_path.read_text(encoding="utf-8")
    schema_table = _schema_table(cfg)
    categories = _categories_block(cfg)
    partner_types = _partner_types_block(cfg)
    tags = _tags_block(cfg)
    description = cfg.description or "Extract structured records from WhatsApp chat messages."

    return f"""You are a data extraction assistant. Your task is to read WhatsApp group chat messages and identify any recommendations, mentions, or warnings about vendors, establishments, services, or tools.

Project: {cfg.project_name}
{description}

## Output Schema

{schema_table}

## Suggested `partner_type` Values

Pick one of the values below. If none of them fit, use `"Other"`. Do not invent any other value.

{partner_types}

## Suggested Categories

Pick one value from the list below. If none of them fit, use `"Other"`. Do not invent any other value.

{categories}

## Suggested Tags

Each bullet below is a single tag descriptor used for a previous record. Always reference this list FIRST and reuse existing tags verbatim (preserving case and spacing) whenever a suitable one exists.

- The `tags` field is an array of one or more individual tag strings, e.g. `["MAYFAIR", "ITALIAN", "PRIVATE DINING"]`.
- Pick every tag from this list that applies to the vendor.
- If no tag in this list captures a descriptor you need, use `"Other"` (either as the sole entry or alongside any tags that do partially apply). Do not invent new tag values.

{tags}

## Training Examples & Classification Rules

{training}

## Response Format

Respond ONLY with a valid JSON array. No markdown fencing, no preamble, no explanation. Each object in the array must conform to the schema above. Use null for unknown optional fields. If no recommendations are found in the batch, respond with [].
"""
