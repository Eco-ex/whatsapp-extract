# whatsapp-extract

A configurable Python CLI agent that extracts structured data from WhatsApp
chat exports. The agent parses raw chat text into JSON, filters noise,
classifies messages with the Claude API using few-shot prompting, and
outputs a deduplicated table (CSV/JSON).

The agent's core logic — parsing, filtering, batching, API orchestration,
consolidation — is universal. Everything project-specific — output schema,
URL handling rules, classification rules, few-shot examples — is defined
in a YAML config file and a training data markdown file. Different users
can point the agent at their own chat exports with their own schemas and
rules without modifying any code.

## Install

```cmd
python -m venv venv
venv\Scripts\activate.bat
pip install -e .
```

Then create a `.env` file and set your API key:

```
ANTHROPIC_API_KEY=sk-ant-...
```

## Usage

```cmd
:: Full pipeline
whatsapp-extract run ^
  --config configs\<your_project>\config.yaml ^
  --input data\raw\chat.txt

:: Parse + filter only (free, no API calls)
whatsapp-extract run ^
  --config configs\<your_project>\config.yaml ^
  --input data\raw\chat.txt ^
  --dry-run

:: Individual steps (handy for debugging or re-running)
whatsapp-extract parse       --config configs\<your_project>\config.yaml --input data\raw\chat.txt
whatsapp-extract filter      --config configs\<your_project>\config.yaml
whatsapp-extract batch       --config configs\<your_project>\config.yaml
whatsapp-extract classify    --config configs\<your_project>\config.yaml
whatsapp-extract consolidate --config configs\<your_project>\config.yaml
```

`classify` is resumable — if a `result_NNN.json` already exists for a batch,
it is skipped. Stop and restart without re-paying for finished batches.

## How it works

The pipeline runs in five stages. Each stage reads from disk, writes to disk,
and can be re-run independently. Intermediate JSON files are kept so you can
inspect (or hand-edit) anything between steps.

```
chat.txt  ->  parse  ->  filter  ->  batch  ->  classify  ->  consolidate  ->  output
              (regex)   (rules)    (chunks)   (Claude API)   (dedup+export)
```

### 1. Parse — raw text to structured messages

The raw export is a flat `.txt` file where each message starts with a
timestamp header. The parser handles three header variants seen in real
exports (iOS/macOS bracketed, Android dash-separated, and system events
with no sender), normalises invisible control characters (LRM/RLM,
narrow no-break spaces, BOM) that otherwise break naive regex, and tries
several common `dd/mm/yyyy` date formats.

Lines that don't start with a recognised header are treated as
continuations of the previous message (so multi-line messages survive
intact). The output is `messages.json` — one record per message with a
stable numeric `id`, ISO datetime, sender, body, and the original raw
line(s) for debugging.

### 2. Filter — drop messages that can't contain useful data

Every parsed message runs through a series of checks in this order:

1. **Empty body** — discarded.
2. **System patterns** (config) — joins/leaves, group renames, security
   notices. Discarded.
3. **Media patterns** (config) — `<Media omitted>`, attachment placeholders.
   Discarded.
4. **Custom discard patterns** (config) — project-specific noise (e.g.
   recurring announcements). Discarded.
5. **Emoji-only** — body collapses to nothing after stripping pictographic
   characters. Discarded.
6. **URL handling** — if the message contains a URL:
   - URL + meaningful surrounding text → kept (the LLM decides).
   - URL-only and any URL matches a `profile_pattern` (a link the user
     considers content-bearing, e.g. a business profile page) → kept.
   - URL-only and *all* URLs match a `disposable_pattern` (link
     shorteners, generic news/social URLs) → discarded.
   - URL-only and unmatched → kept (let the LLM decide rather than guess).
7. **Length check** — messages shorter than `min_message_length` are
   discarded.

The filter is project-agnostic in its mechanics but project-tunable in its
rules. The first reason a message matches wins, and the discard reason is
counted so you can audit the breakdown after each run.

### 3. Batch — chunk messages for API calls

Filtered messages are split into fixed-size chunks (`llm.batch_size`).
Each batch carries the last *N* messages from the previous batch as
`context_messages` (`llm.batch_overlap`) so the model can resolve
references that span a chunk boundary (e.g. "the one I mentioned
yesterday"). Context messages are shown to the model but explicitly
flagged as *do not extract from these*.

Each batch is written as `batch_NNN.json` with its message-id range, date
range, context, and payload. Old batch files are cleared on each run so a
re-batch never leaves stale chunks behind.

### 4. Classify — Claude API extraction

For every batch:

1. The system prompt is assembled by `prompt_builder` from
   `config.yaml` + `TRAINING_DATA.md`. The schema table, allowed
   categories, partner types, and tag vocabulary are auto-generated from
   the config — you don't repeat them in the training file.
2. The user message lists the batch's messages (and any context messages,
   clearly separated) and asks for a JSON array.
3. The call uses **prompt caching** on the system block, so subsequent
   batches in the same run pay a fraction of the input cost.
4. Transient errors (rate limits, connection drops, 5xx) retry with
   exponential backoff up to `llm.max_retries`.
5. The model output is parsed as JSON. Code fences are stripped if
   present, and as a fallback the largest `[...]` substring is extracted
   (handles truncation at `max_tokens` gracefully).
6. Each returned record is validated against the schema: missing optional
   fields get their `default`, missing required fields are kept as `null`
   so the consolidator can flag them rather than silently dropping the row.
7. The result is written to `result_NNN.json` alongside the token usage
   for that batch. If parsing fails entirely, the raw response is saved
   to `<batch_id>.error.txt` for inspection.

Because results are written per-batch, `classify` is resumable: re-running
skips any batch whose result file already exists.

### 5. Consolidate — merge, deduplicate, export

All `result_*.json` files are concatenated into a single record list, then
deduplicated using fuzzy matching on a configurable field
(`consolidation.dedup_field`, typically a name). Two records are grouped
together if `rapidfuzz.token_set_ratio` on the dedup field meets
`dedup_threshold`. Within each group, a single winner is picked by
strategy:

- `earliest` — keep the record with the earliest timestamp.
- `latest` — keep the most recent.
- `most_complete` — keep the record with the most non-null fields
  (ties broken by timestamp).

The deduplicated set is sorted by `consolidation.sort_by`, written to the
configured `output_formats` (`csv`, `json`, or both), and a quality
report is printed: raw vs. deduped count, breakdown by category and
partner type, optional-field population rate, top-N by dedup field, and
the count of records missing required fields.

## Project layout

```
whatsapp_extract/             # Universal agent code
  config_loader.py
  parser.py                   # raw .txt → messages.json
  filter.py                   # discard noise per config rules
  batcher.py                  # split into API-sized chunks
  prompt_builder.py           # build system prompt from config + training data
  classifier.py               # Claude API orchestration
  consolidator.py             # dedup + export
  cli.py

configs/                      # gitignored — per-project private data
  <your_project>/
    config.yaml               # schema, categories, filter rules
    TRAINING_DATA.md          # few-shot examples + classification rules

data/                         # gitignored — runtime data
  raw/                        # input .txt exports
  parsed/   filtered/   batches/   results/

output/                       # gitignored — final CSV/JSON
```

`configs/` is gitignored because each project's schema, training examples,
and ground-truth data are typically private to that project. Keep them
locally and back them up separately.

## Setting up a project

Create `configs/<your_project>/config.yaml` and `TRAINING_DATA.md`. Define
your own schema, categories, filter rules, and few-shot examples.

### `config.yaml` — what each block controls

| Block | Purpose |
|-------|---------|
| `project_name`, `description` | Identifies the run; the description is passed into the system prompt. |
| `schema.fields` | Output columns. Each field has `name`, `type`, `required`, `description`, optional `default`. Must include a `name` field and at least one required field. |
| `categories` | Closed vocabulary for the `category` field. If empty, the model is told it may use a freeform value. |
| `partner_types` | Closed vocabulary for the `partner_type` field. Same fallback rules. |
| `tags` | Preferred tag vocabulary. The model is instructed to reuse these verbatim before inventing new ones. |
| `filters.system_patterns` | Regexes that mark system events to discard. |
| `filters.media_patterns` | Regexes for media-attachment placeholders. |
| `filters.custom_discard_patterns` | Anything else you want to drop pre-API. |
| `filters.url_rules.profile_patterns` | URLs that ARE content (kept even when alone). |
| `filters.url_rules.disposable_patterns` | URLs that AREN'T content (discarded when alone). |
| `filters.min_message_length` | Minimum body length for a non-URL message to survive the filter. |
| `llm.model` | Claude model ID. |
| `llm.batch_size`, `llm.batch_overlap` | Chunking. Overlap is the trailing N messages shown as context to the next batch. |
| `llm.max_tokens`, `llm.delay_between_calls`, `llm.max_retries` | Per-call cost/throughput knobs. |
| `consolidation.dedup_field` | Field used for fuzzy grouping (typically `name`). Must exist in the schema. |
| `consolidation.dedup_threshold` | Fuzz score (0–100) above which two records are considered duplicates. |
| `consolidation.dedup_strategy` | `earliest`, `latest`, or `most_complete`. |
| `consolidation.sort_by` | List of field names used as the final sort key. |
| `consolidation.output_formats` | Any combination of `csv` and `json`. |

### `TRAINING_DATA.md` — few-shot rules and examples

This file is appended verbatim into the system prompt. Use it to spell out
project-specific classification rules ("if a message mentions X, treat it as
Y"), edge cases, and a handful of input → output examples in the same JSON
shape your schema produces. The schema table, category list, partner-type
list, and tag list are auto-generated from `config.yaml`, so don't repeat
them here.

Run the same CLI pointed at your new config — no code changes needed.
