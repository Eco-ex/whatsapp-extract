"""Step 2: filter out messages that cannot contain useful data."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from .config_loader import Config

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
# Emoji / symbol detection — covers most pictographic ranges. Used to detect
# messages that consist only of emoji + whitespace.
_NON_TEXT_RE = re.compile(
    r"[☀-➿"
    r"\U0001F300-\U0001F9FF"
    r"\U0001FA00-\U0001FAFF"
    r"\U0001F000-\U0001F02F"
    r"‍️]"
)


class FilterEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.system_res = [re.compile(p) for p in cfg.filters.system_patterns]
        self.media_res = [re.compile(p) for p in cfg.filters.media_patterns]
        self.disposable_res = [re.compile(p) for p in cfg.filters.url_rules.disposable_patterns]
        self.profile_res = [re.compile(p) for p in cfg.filters.url_rules.profile_patterns]
        self.custom_res = [re.compile(p) for p in cfg.filters.custom_discard_patterns]

    def classify(self, message: dict) -> tuple[bool, str]:
        """Return (keep, reason)."""
        body = (message.get("body") or "").strip()

        if not body:
            return False, "empty"

        # System messages — match against raw or body
        for r in self.system_res:
            if r.search(body) or r.search(message.get("raw", "")):
                return False, "system"

        # Media placeholders
        for r in self.media_res:
            if r.search(body):
                return False, "media"

        # Custom discard patterns (project-specific)
        for r in self.custom_res:
            if r.search(body):
                return False, "custom"

        # Emoji/symbol-only messages (built-in)
        text_only = _NON_TEXT_RE.sub("", body).strip()
        if not text_only:
            return False, "emoji_only"

        # URL handling
        urls = _URL_RE.findall(body)
        if urls:
            non_url_text = _URL_RE.sub("", body).strip()
            # Has meaningful text alongside the URL → keep, LLM decides
            if len(non_url_text) >= self.cfg.filters.min_message_length:
                return True, "url_with_text"

            # URL-only (or trivial text). Walk URLs:
            # - if any matches a profile pattern → keep
            # - elif all match disposable patterns → discard
            # - else → keep (unknown URL type, let LLM decide)
            if any(any(r.search(u) for r in self.profile_res) for u in urls):
                return True, "url_profile"
            if urls and all(any(r.search(u) for r in self.disposable_res) for u in urls):
                return False, "url_disposable"
            return True, "url_unknown"

        # Length check (only after URL handling, since URL-only messages are
        # short by nature and we've already decided their fate above)
        if len(body) < self.cfg.filters.min_message_length:
            return False, "too_short"

        return True, "kept"


def run(cfg: Config) -> Path:
    """Filter parsed messages and write filtered_messages.json."""
    parsed_path = cfg.parsed_dir / "messages.json"
    if not parsed_path.exists():
        raise FileNotFoundError(
            f"Parsed messages not found at {parsed_path}. Run `whatsapp-extract parse` first."
        )

    with parsed_path.open("r", encoding="utf-8") as f:
        messages: list[dict] = json.load(f)

    engine = FilterEngine(cfg)
    kept: list[dict] = []
    reasons: Counter = Counter()

    for m in messages:
        keep, reason = engine.classify(m)
        reasons[reason] += 1
        if keep:
            kept.append(m)

    out_dir = cfg.filtered_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "filtered_messages.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(kept, f, ensure_ascii=False, indent=2)

    _print_stats(len(messages), kept, reasons, out_path)
    return out_path


def _print_stats(total: int, kept: list[dict], reasons: Counter, out: Path) -> None:
    print(f"[filter] Input: {total} messages | Kept: {len(kept)} | Discarded: {total - len(kept)}")
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        prefix = "kept" if reason in {"kept", "url_with_text", "url_profile", "url_unknown"} else "discarded"
        print(f"  {prefix:>9}: {reason:<20} {count}")
    print(f"[filter] Wrote {out}")
