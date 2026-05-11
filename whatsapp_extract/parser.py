"""Step 1: parse a raw WhatsApp .txt export into a list of structured messages."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable

# WhatsApp on iOS/macOS: [DD/MM/YYYY, HH:MM:SS] Sender: body
# Some exports use HH:MM (no seconds). Some include narrow no-break spaces (U+202F)
# or left-to-right marks (U+200E) inside the brackets. We strip control chars first.
_HEADER = re.compile(
    r"^\[(?P<date>\d{1,2}/\d{1,2}/\d{2,4}),\s+(?P<time>\d{1,2}:\d{2}(?::\d{2})?)\]\s+"
    r"(?P<sender>.+?):\s?(?P<body>.*)$"
)

# Some Android exports omit the brackets:
# DD/MM/YYYY, HH:MM - Sender: body
_HEADER_ANDROID = re.compile(
    r"^(?P<date>\d{1,2}/\d{1,2}/\d{2,4}),\s+(?P<time>\d{1,2}:\d{2}(?::\d{2})?)\s+-\s+"
    r"(?P<sender>.+?):\s?(?P<body>.*)$"
)

# A system message has no "Sender: body" — just "Sender event description".
# We also detect it via the absence of the colon. We treat the entire trailing
# segment as the body and leave sender empty for system events that don't have
# a clear actor (the filter step will discard them).
_HEADER_SYSTEM = re.compile(
    r"^\[(?P<date>\d{1,2}/\d{1,2}/\d{2,4}),\s+(?P<time>\d{1,2}:\d{2}(?::\d{2})?)\]\s+(?P<body>.+)$"
)


def _strip_control(s: str) -> str:
    # Remove LRM (U+200E), RLM (U+200F), narrow no-break space (U+202F),
    # and zero-width spaces. These appear in WhatsApp exports and break naive regex.
    return s.translate({0x200E: None, 0x200F: None, 0x202F: 0x20, 0xFEFF: None})


def _parse_datetime(date_str: str, time_str: str) -> datetime:
    # Try a few common WhatsApp date/time formats.
    formats = [
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%y %H:%M:%S",
        "%d/%m/%y %H:%M",
    ]
    raw = f"{date_str} {time_str}"
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError(f"Could not parse datetime: {raw!r}")


def _iter_messages(lines: Iterable[str]) -> Iterable[dict]:
    current: dict | None = None
    raw_buffer: list[str] = []

    def flush() -> dict | None:
        if current is None:
            return None
        current["body"] = current["body"].rstrip()
        current["raw"] = "\n".join(raw_buffer).rstrip()
        return current

    next_id = 1
    for line in lines:
        cleaned = _strip_control(line.rstrip("\r\n"))

        m = _HEADER.match(cleaned) or _HEADER_ANDROID.match(cleaned)
        if m:
            # Emit previous, start new
            done = flush()
            if done is not None:
                yield done
            try:
                dt = _parse_datetime(m.group("date"), m.group("time"))
            except ValueError:
                # Malformed header — treat as continuation of previous
                if current is not None:
                    current["body"] += "\n" + cleaned
                    raw_buffer.append(cleaned)
                continue
            current = {
                "id": next_id,
                "datetime": dt.isoformat(),
                "date": dt.strftime("%Y-%m-%d"),
                "time": dt.strftime("%H:%M:%S"),
                "sender": m.group("sender").strip(),
                "body": m.group("body"),
                "raw": "",
            }
            raw_buffer = [cleaned]
            next_id += 1
            continue

        m_sys = _HEADER_SYSTEM.match(cleaned)
        if m_sys:
            done = flush()
            if done is not None:
                yield done
            try:
                dt = _parse_datetime(m_sys.group("date"), m_sys.group("time"))
            except ValueError:
                if current is not None:
                    current["body"] += "\n" + cleaned
                    raw_buffer.append(cleaned)
                continue
            current = {
                "id": next_id,
                "datetime": dt.isoformat(),
                "date": dt.strftime("%Y-%m-%d"),
                "time": dt.strftime("%H:%M:%S"),
                "sender": "",
                "body": m_sys.group("body").strip(),
                "raw": "",
            }
            raw_buffer = [cleaned]
            next_id += 1
            continue

        # Continuation line
        if current is not None:
            current["body"] += "\n" + cleaned
            raw_buffer.append(cleaned)
        # Otherwise: stray line before any message header — drop it.

    done = flush()
    if done is not None:
        yield done


def parse_file(input_path: Path) -> list[dict]:
    """Parse a WhatsApp .txt export into a list of message dicts."""
    text = input_path.read_text(encoding="utf-8", errors="replace")
    return list(_iter_messages(text.splitlines()))


def write_messages(messages: list[dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(messages, f, ensure_ascii=False, indent=2)


def run(input_path: Path, output_dir: Path) -> Path:
    """Parse the input file and write messages.json. Returns the output path."""
    messages = parse_file(input_path)
    out = output_dir / "messages.json"
    write_messages(messages, out)
    _print_stats(messages, out)
    return out


def _print_stats(messages: list[dict], out: Path) -> None:
    if not messages:
        print(f"[parse] No messages parsed. Output: {out}")
        return
    senders = {m["sender"] for m in messages if m["sender"]}
    first_dt = messages[0]["datetime"]
    last_dt = messages[-1]["datetime"]
    print(
        f"[parse] {len(messages)} messages | {len(senders)} unique senders | "
        f"{first_dt} -> {last_dt}"
    )
    print(f"[parse] Wrote {out}")
