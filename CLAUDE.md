# CLAUDE.md — pdpc-zeeker

## Project Overview

**Project Name:** pdpc-zeeker
**Database:** pdpc.db
**Purpose:** PDPC enforcement decisions (Commission's Decisions and Voluntary Undertakings) from the Personal Data Protection Commission Singapore.

## Development Environment

Uses **uv** for dependency management. All commands prefixed with `uv run`.

```bash
uv sync                                        # Install dependencies
uv run zeeker build                            # Build database from all resources
uv run zeeker build enforcement_decisions      # Build specific resource
uv run zeeker build --sync-from-s3             # Incremental build (download existing DB first)
uv run zeeker deploy                           # Deploy to S3
```

## Resources

### `enforcement_decisions` Resource

**Source:** https://www.pdpc.gov.sg/organisations/regulations-decisions/enforcement-decisions

**Scraping strategy:**
- Listing via JSON API: `GET /api/listing-api?listingtype=enforcement_decisions&...`
- Detail pages: Next.js RSC stream — row 22 = date, row 24 = `{"content": "<html>"}` + PDF asset link
- PDF extraction via docling server: bytes fetched through Tailscale proxy (PDPC CloudFront blocks DC IPs), then POSTed to `DOCLING_SERVE_URL/v1/convert/file`
- Fragments: PDF markdown text only, chunked at 1200 chars with 150-char overlap

**Cadence:** Weekly (PDPC decisions published infrequently). Workflow: `.github/workflows/sync-enforcement-decisions.yml`

**Environment variables required:**
- `TAILSCALE_PROXY` — SOCKS5 proxy for CloudFront bypass (e.g. `socks5h://172.17.0.1:1055`)
- `DOCLING_SERVE_URL` — Docling server URL (e.g. `http://host.docker.internal:5001`)
- `S3_BUCKET`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `S3_ENDPOINT_URL` — S3 deployment

**Optional backfill env vars (shared by all 3 resources):**
- `PDPC_BACKFILL_RETRY_AFTER` — quarantine TTL in seconds (default `1209600` = 14 days).
  Quarantined items (failure count at/over the retry cap) become eligible for another
  backfill attempt once their `last_attempt` is older than this.
- `PDPC_BACKFILL_RETRY_QUARANTINED=1` — manual flush: ALL quarantined items become
  eligible this run regardless of TTL.
- Per-resource knobs: `PDPC_BACKFILL_BATCH_SIZE` / `PDPC_BACKFILL_MAX_RETRIES`
  (enforcement_decisions), `PDPC_GUIDANCE_BACKFILL_*` (guidance_by_topic),
  `PDPC_REGGUIDANCE_BACKFILL_*` (regulatory_guidance).

**Fragment generation note (zeeker >= 0.9.0):**
`fetch_data()` runs ONCE per build; zeeker threads its raw output into
`fetch_fragments_data()` as `main_data_context`. New records carry the extracted
document text in an internal `_pdf_text` field; `transform_data()` strips it before
the rows are inserted (zeeker deepcopies the pre-transform data for the fragments
phase), and `fetch_fragments_data()` chunks it into `{uuid}_chunk_{seq}` fragments.
Each resource's `migrate_schema()` also drops `_pdf_text` from the inferred schema,
because zeeker's schema sample sees the pre-transform rows. Steady-state builds
(0 new rows) get their fragments from the backfill side effect inside `fetch_data()`.
When a listing fetch fails outright, `fetch_data()` raises
`Skip(reason, kind="blocked")` (after the `ABORTED` stderr line) so the skip is
machine-readable and the `_zeeker_updates` freshness marker is not advanced.
Backfill/dedupe counters are surfaced via the module-level `__zeeker_report__` dict
(`fragments_ok`, `fragments_failed`, `still_pending`, `quarantined_total`,
`dedupe_removed`).

**De-duplication note:**
`decision_url` is the natural key for a decision. PDPC occasionally republishes a
decision under a new listing UUID (same URL/date, common in same-date Voluntary
Undertaking batches), which the `id`-based skip in `fetch_data()` misses. Two
guards handle this: `fetch_data()` skips listing items whose `decision_url` is
already known, and `_dedupe_existing_decisions()` (run as a build side effect)
removes existing rows sharing a `decision_url`, keeping the earliest-imported one
and dropping orphaned fragments.

**Fragment backfill & quarantine TTL note:**
All three resources run a fragment backfill inside every build (a small batch per
run) that extracts PDF text via Docling. Failures are tracked per item in a JSON
checkpoint (`checkpoint_pdpc_fragments.json`, `checkpoint_guidance_fragments.json`,
`checkpoint_regulatory_guidance_fragments.json`) as
`{id: {failures, last_error, last_attempt}}`. Items reaching the retry cap
(`PDPC_*_BACKFILL_MAX_RETRIES`, default 3) are **quarantined with a TTL**, not
forever: once `last_attempt` is older than `PDPC_BACKFILL_RETRY_AFTER` seconds
(default `1209600` = 14 days ≈ two weekly builds) the item becomes eligible again.
The failure count is preserved, so a still-broken item re-quarantines after a
single failed re-attempt and waits another TTL window. Set
`PDPC_BACKFILL_RETRY_QUARANTINED=1` to make ALL quarantined items eligible on that
run (manual flush). A missing or malformed `last_attempt` fails open (the item is
retried) so bad checkpoint data can never strand an item. Shared logic lives in
`resources/_backfill.py` — a plain top-level `import _backfill` in each resource,
which works because zeeker >= 0.9.0 puts `resources/` on `sys.path` while a
resource module loads (top-level sibling imports only; lazy in-function imports
would fail). Its pure functions are unit-tested in `tests/test_backfill.py`
(`uv run pytest`).
`fetch_data()` applies the same <50-chars extraction-success threshold as the
backfill: a too-short Docling result is treated as a failed extraction (logged to
stderr) and left for the backfill to retry, instead of being silently stored.

**Full-text search note:**
FTS fields are declared in `zeeker.toml` (`fts_fields`, `fragments_fts_fields`).
As of zeeker 0.9.0, `zeeker build --setup-fts` is idempotent (safe when the FTS
table already exists), so both sync workflows pass `--setup-fts` on every build —
fresh or incremental — and the former `scripts/setup_fts.py` workaround has been
deleted. Without FTS setup the PDPC tables are absent from the upstream FTS index
and the MCP `search` tool silently skips them.

**Schema:** `enforcement_decisions` table
- `id` — PDPC internal UUID (from listing API)
- `title`, `organisation`, `decision_type` — decision metadata
- `decision_date` — ISO 8601 date
- `decision_url`, `pdf_url` — URLs to decision page and PDF
- `penalty_amount` — SGD float or null
- `summary` — brief HTML blurb from the decision page (2–4 sentences)
- `imported_on` — ISO 8601 timestamp

**Fragments schema:** `enforcement_decisions_fragments` table
- `id` = `{decision_uuid}_chunk_{seq}`
- `parent_id` — links to `enforcement_decisions.id`
- `text`, `sequence`, `content_type`, `char_count`

### `guidance_by_topic` Resource

**Source:** https://www.pdpc.gov.sg/organisations/resources/guidance-by-topic

**Scraping strategy:**
- Listing via JSON API: `GET /api/listing-api?listingtype=guidance_by_topic&...`
- Detail pages: Next.js RSC stream — dynamic `"data":{"content":"$XX"}` pattern (row varies, not hardcoded)
- Date from RSC row 23 (`page-banner__date` span)
- PDF extraction via docling server for pages with `/assets/` links; HTML-only pages store summary text
- Fragments: PDF markdown text only, chunked at 1200 chars with 150-char overlap

**Cadence:** Weekly (guidance updated infrequently). Workflow: `.github/workflows/sync-pdpc-guidance.yml`

**Schema:** `guidance_by_topic` table
- `id` — PDPC internal UUID (from listing API)
- `title` — guidance document title
- `topic` — Publications, Templates, Training Courses, or Tools
- `published_date` — ISO 8601 date
- `page_url` — URL to the guidance page on pdpc.gov.sg
- `pdf_url` — URL to the primary PDF document (empty for HTML-only pages)
- `summary` — plain text extracted from the page content
- `imported_on` — ISO 8601 timestamp

**Fragments schema:** `guidance_by_topic_fragments` table
- `id` = `{item_uuid}_chunk_{seq}`
- `parent_id` — links to `guidance_by_topic.id`
- `text`, `sequence`, `content_type`, `char_count`

### `regulatory_guidance` Resource

**Source:** https://www.pdpc.gov.sg/organisations/regulations-decisions/regulatory-guidance

**Scraping strategy:**
- Listing via JSON API: `GET /api/listing-api?listingtype=regulatory_guidance&...`
- Detail pages: Next.js RSC stream — dynamic `"data":{"content":"$XX"}` pattern (row varies, not hardcoded)
- Date from RSC row 23 (`page-banner__date` span)
- PDF extraction via docling server — most pages link to full PDF documents + annexes
- Fragments: PDF markdown text only, chunked at 1200 chars with 150-char overlap

**Cadence:** Weekly (guidance updated infrequently). Workflow: `.github/workflows/sync-pdpc-guidance.yml`

**Schema:** `regulatory_guidance` table
- `id` — PDPC internal UUID (from listing API)
- `title` — guideline title
- `topic` — Advisory Guidelines, Practical Guidance, Sector-Specific Guidelines, or Industry-led Guidelines
- `published_date` — ISO 8601 date
- `page_url` — URL to the guidance page on pdpc.gov.sg
- `pdf_url` — URL to the primary PDF document
- `summary` — plain text extracted from the page content
- `imported_on` — ISO 8601 timestamp

**Fragments schema:** `regulatory_guidance_fragments` table
- `id` = `{item_uuid}_chunk_{seq}`
- `parent_id` — links to `regulatory_guidance.id`
- `text`, `sequence`, `content_type`, `char_count`

## GitHub Secrets Required

Configure in `Settings > Secrets and variables > Actions`:

```
TAILSCALE_PROXY        # SOCKS5 proxy URL for CloudFront bypass
DOCLING_SERVE_URL      # Docling server URL
S3_BUCKET              # S3 bucket name
AWS_ACCESS_KEY_ID      # AWS credentials
AWS_SECRET_ACCESS_KEY
S3_ENDPOINT_URL        # Non-AWS S3 endpoint (Contabo, DigitalOcean, etc.)
```

---

## Build monitoring

Operational guidance for humans and AI monitoring agents — build schedule,
healthy vs failed log patterns, skip kinds, `__zeeker_report__` counters,
quarantine/checkpoint behaviour, and SQL backlog queries — lives in
**`RUNBOOK.md`** (generated by `zeeker runbook`, then hand-maintained; do not
regenerate with `--force` without merging manually). This file stays focused on
development: resource internals, schema notes, and environment setup.
