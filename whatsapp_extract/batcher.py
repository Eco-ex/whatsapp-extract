"""Step 3: split filtered messages into chunks sized for a single API call."""

from __future__ import annotations

import json
from pathlib import Path

from .config_loader import Config

# Fields the classifier actually reads from each message. Other fields parsed
# upstream (date, time, raw) are dropped at batch-write time to slim the prompt
# input and the on-disk batch files. `raw` is still needed by filter.py for
# system-pattern matching, so this trim happens here, not in parser.py.
_BATCH_FIELDS = ("id", "datetime", "sender", "body")


def _slim(message: dict) -> dict:
    return {k: message[k] for k in _BATCH_FIELDS if k in message}


def run(cfg: Config) -> Path:
    """Read filtered messages and write batch_NNN.json files. Returns the batch dir."""
    filtered_path = cfg.filtered_dir / "filtered_messages.json"
    if not filtered_path.exists():
        raise FileNotFoundError(
            f"Filtered messages not found at {filtered_path}. Run `whatsapp-extract filter` first."
        )

    with filtered_path.open("r", encoding="utf-8") as f:
        messages: list[dict] = json.load(f)

    batch_size = cfg.llm.batch_size
    overlap = max(0, cfg.llm.batch_overlap)

    out_dir = cfg.batches_dir
    # Clear old batch files so a re-run doesn't leave stale ones around.
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("batch_*.json"):
        old.unlink()

    if not messages:
        print("[batch] No messages to batch.")
        return out_dir

    batches: list[list[dict]] = []
    contexts: list[list[dict]] = []
    i = 0
    n = len(messages)
    prev_tail: list[dict] = []
    while i < n:
        chunk = messages[i : i + batch_size]
        batches.append(chunk)
        contexts.append(prev_tail)
        prev_tail = chunk[-overlap:] if overlap else []
        i += batch_size

    digits = max(3, len(str(len(batches))))
    for idx, (chunk, ctx) in enumerate(zip(batches, contexts), start=1):
        batch_id = f"batch_{idx:0{digits}d}"
        payload = {
            "batch_id": batch_id,
            "message_range": {
                "from_id": chunk[0]["id"],
                "to_id": chunk[-1]["id"],
            },
            "date_range": {
                "from": chunk[0]["date"],
                "to": chunk[-1]["date"],
            },
            "context_messages": [_slim(m) for m in ctx],
            "messages": [_slim(m) for m in chunk],
        }
        out_path = out_dir / f"{batch_id}.json"
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    avg = len(messages) / len(batches) if batches else 0
    print(
        f"[batch] {len(batches)} batches written to {out_dir} "
        f"(avg {avg:.1f} msgs/batch, overlap {overlap})"
    )
    return out_dir
