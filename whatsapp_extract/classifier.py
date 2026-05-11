"""Step 4: send each batch to the Claude API and collect structured JSON responses."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from tqdm import tqdm

from .config_loader import Config
from .prompt_builder import build_system_prompt


class ClassifierError(RuntimeError):
    pass


def _format_batch_for_user(batch: dict) -> str:
    """Format a batch payload as the user message sent to the model."""
    parts: list[str] = []

    ctx = batch.get("context_messages") or []
    if ctx:
        parts.append("CONTEXT (do not extract from these — they appeared in a previous batch):")
        for m in ctx:
            parts.append(f"- [{m['datetime']}] {m['sender']}: {m['body']}")
        parts.append("")

    parts.append("MESSAGES TO PROCESS:")
    for m in batch["messages"]:
        parts.append(f"- id={m['id']} [{m['datetime']}] {m['sender']}: {m['body']}")

    parts.append("")
    parts.append(
        "Return a JSON array of extracted records. If no extractable data is present, return []."
    )
    return "\n".join(parts)


_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


def _parse_response_text(text: str) -> list[dict]:
    """Parse the model output. Accept bare JSON or JSON inside text."""
    stripped = text.strip()
    # Strip code fences if present
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\n?", "", stripped)
        stripped = re.sub(r"\n?```$", "", stripped)
        stripped = stripped.strip()

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        m = _JSON_ARRAY_RE.search(stripped)
        if not m:
            raise ClassifierError(f"Model response is not valid JSON: {text[:300]!r}")
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError as e:
            raise ClassifierError(f"Model response JSON is malformed (likely truncated at max_tokens): {e}")

    if not isinstance(parsed, list):
        raise ClassifierError(f"Expected JSON array, got {type(parsed).__name__}")
    return parsed


def _validate_record(rec: dict, cfg: Config) -> dict:
    """Apply schema defaults and check required fields. Returns the record (possibly with defaults filled)."""
    if not isinstance(rec, dict):
        raise ClassifierError(f"Record is not an object: {rec!r}")

    out = dict(rec)
    for f in cfg.schema.fields:
        if f.name not in out or out[f.name] is None:
            if f.default is not None:
                out[f.name] = f.default
            elif f.required:
                # Don't drop the record — let consolidator flag it. But warn.
                out.setdefault(f.name, None)
    return out


def _call_with_retry(client, *, model: str, system: str, user: str, max_tokens: int, max_retries: int):
    """Call the Claude API with exponential backoff on transient errors."""
    import anthropic

    backoff = 2.0
    last_err: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user}],
            )
        except (anthropic.RateLimitError, anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            last_err = e
            if attempt == max_retries:
                break
            wait = backoff ** attempt
            print(f"  [retry {attempt}/{max_retries}] {type(e).__name__}: sleeping {wait:.1f}s")
            time.sleep(wait)
    raise ClassifierError(f"API call failed after {max_retries} retries: {last_err}")


def run(cfg: Config) -> Path:
    """Classify every batch and write per-batch result files. Returns the results dir."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ClassifierError(
            "ANTHROPIC_API_KEY not set. Add it to your .env file or environment."
        )

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    batches_dir = cfg.batches_dir
    results_dir = cfg.results_dir
    results_dir.mkdir(parents=True, exist_ok=True)

    batch_files = sorted(batches_dir.glob("batch_*.json"))
    if not batch_files:
        raise ClassifierError(
            f"No batch files found in {batches_dir}. Run `whatsapp-extract batch` first."
        )

    system_prompt = build_system_prompt(cfg)

    total_input = 0
    total_output = 0
    skipped = 0
    failed = 0

    pbar = tqdm(batch_files, desc="classify", unit="batch")
    for batch_path in pbar:
        result_path = results_dir / batch_path.name.replace("batch_", "result_")
        if result_path.exists():
            skipped += 1
            pbar.set_postfix(skipped=skipped, failed=failed)
            continue

        with batch_path.open("r", encoding="utf-8") as f:
            batch = json.load(f)

        user_message = _format_batch_for_user(batch)
        try:
            response = _call_with_retry(
                client,
                model=cfg.llm.model,
                system=system_prompt,
                user=user_message,
                max_tokens=cfg.llm.max_tokens,
                max_retries=cfg.llm.max_retries,
            )
        except ClassifierError as e:
            failed += 1
            print(f"\n[classify] {batch['batch_id']} failed: {e}")
            pbar.set_postfix(skipped=skipped, failed=failed)
            time.sleep(cfg.llm.delay_between_calls)
            continue

        # Extract text from response
        text_parts = [
            block.text for block in response.content if getattr(block, "type", None) == "text"
        ]
        text = "\n".join(text_parts)

        try:
            records = _parse_response_text(text)
            records = [_validate_record(r, cfg) for r in records]
        except ClassifierError as e:
            failed += 1
            print(f"\n[classify] {batch['batch_id']} parse failed: {e}")
            # Still save the raw response for debugging
            with (results_dir / f"{batch['batch_id']}.error.txt").open("w", encoding="utf-8") as f:
                f.write(text)
            pbar.set_postfix(skipped=skipped, failed=failed)
            time.sleep(cfg.llm.delay_between_calls)
            continue

        usage: dict[str, Any] = {}
        if response.usage:
            usage = {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
                "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            }
            total_input += response.usage.input_tokens
            total_output += response.usage.output_tokens

        payload = {
            "batch_id": batch["batch_id"],
            "message_range": batch["message_range"],
            "usage": usage,
            "records": records,
        }
        with result_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        pbar.set_postfix(
            in_tok=total_input,
            out_tok=total_output,
            skipped=skipped,
            failed=failed,
        )
        time.sleep(cfg.llm.delay_between_calls)

    print(
        f"[classify] Done. Skipped {skipped} (already done), failed {failed}. "
        f"Tokens: in={total_input}, out={total_output}"
    )
    return results_dir
