"""Step 5: merge per-batch results, deduplicate, and export."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from .config_loader import Config


def _completeness(rec: dict, cfg: Config) -> int:
    """Number of non-null fields. Used by the most_complete dedup strategy."""
    score = 0
    for f in cfg.schema.fields:
        v = rec.get(f.name)
        if v not in (None, "", [], {}):
            score += 1
    return score


def _timestamp_key(rec: dict) -> str:
    return rec.get("timestamp") or ""


def _pick_winner(group: list[dict], strategy: str, cfg: Config) -> dict:
    if strategy == "earliest":
        return min(group, key=_timestamp_key)
    if strategy == "latest":
        return max(group, key=_timestamp_key)
    # most_complete
    return max(group, key=lambda r: (_completeness(r, cfg), _timestamp_key(r)))


def _dedup(records: list[dict], cfg: Config) -> list[dict]:
    field = cfg.consolidation.dedup_field
    threshold = cfg.consolidation.dedup_threshold
    strategy = cfg.consolidation.dedup_strategy

    groups: list[list[dict]] = []
    for rec in records:
        key = (rec.get(field) or "").strip().lower()
        if not key:
            groups.append([rec])
            continue
        placed = False
        for g in groups:
            existing_key = (g[0].get(field) or "").strip().lower()
            if existing_key and fuzz.token_set_ratio(key, existing_key) >= threshold:
                g.append(rec)
                placed = True
                break
        if not placed:
            groups.append([rec])

    return [_pick_winner(g, strategy, cfg) for g in groups]


def _sort_records(records: list[dict], cfg: Config) -> list[dict]:
    keys = cfg.consolidation.sort_by

    def sort_key(r: dict) -> tuple:
        return tuple((r.get(k) or "") for k in keys)

    return sorted(records, key=sort_key)


def _export(records: list[dict], cfg: Config, basename: str | None = None) -> list[Path]:
    import pandas as pd

    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    out_paths: list[Path] = []

    field_order = [f.name for f in cfg.schema.fields]
    df = pd.DataFrame(records, columns=field_order)
    basename = basename or cfg.project_name

    if "csv" in cfg.consolidation.output_formats:
        out = cfg.output_dir / f"{basename}.csv"
        df.to_csv(out, index=False)
        out_paths.append(out)
    if "json" in cfg.consolidation.output_formats:
        out = cfg.output_dir / f"{basename}.json"
        with out.open("w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        out_paths.append(out)
    return out_paths


def _quality_report(raw_count: int, deduped: list[dict], cfg: Config) -> None:
    print(f"[consolidate] Raw records: {raw_count} -> Deduplicated: {len(deduped)}")

    cats = Counter(r.get("category") for r in deduped if r.get("category"))
    if cats:
        print("[consolidate] By category:")
        for c, n in cats.most_common():
            print(f"  {c}: {n}")

    types = Counter(r.get("partner_type") for r in deduped if r.get("partner_type"))
    if types:
        print("[consolidate] By partner_type:")
        for t, n in types.most_common():
            print(f"  {t}: {n}")

    optional_fields = [f.name for f in cfg.schema.fields if not f.required]
    if optional_fields:
        print("[consolidate] Optional field population:")
        for fname in optional_fields:
            n = sum(1 for r in deduped if r.get(fname) not in (None, "", [], {}))
            print(f"  {fname}: {n}/{len(deduped)}")

    name_field = cfg.consolidation.dedup_field
    name_counts = Counter(
        (r.get(name_field) or "").strip()
        for r in deduped
        if (r.get(name_field) or "").strip()
    )
    if name_counts:
        print(f"[consolidate] Top 10 by {name_field}:")
        for name, n in name_counts.most_common(10):
            print(f"  {name}: {n}")

    missing_required: list[dict] = []
    for r in deduped:
        for f in cfg.schema.fields:
            if f.required and r.get(f.name) in (None, "", [], {}):
                missing_required.append(r)
                break
    if missing_required:
        print(f"[consolidate] {len(missing_required)} records missing required fields (review needed)")


def run(cfg: Config) -> list[Path]:
    """Consolidate, deduplicate, and export. Returns list of output paths."""
    result_files = sorted(cfg.results_dir.glob("result_*.json"))
    if not result_files:
        raise FileNotFoundError(
            f"No result files found in {cfg.results_dir}. Run `whatsapp-extract classify` first."
        )

    all_records: list[dict] = []
    for rf in result_files:
        with rf.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        all_records.extend(payload.get("records") or [])

    raw_count = len(all_records)
    sorted_raw = _sort_records(all_records, cfg)
    deduped = _dedup(all_records, cfg)
    sorted_records = _sort_records(deduped, cfg)

    out_paths = _export(sorted_records, cfg)
    out_paths.extend(_export(sorted_raw, cfg, basename=f"{cfg.project_name}_no_deduplication"))
    _quality_report(raw_count, sorted_records, cfg)
    for p in out_paths:
        print(f"[consolidate] Wrote {p}")
    return out_paths
