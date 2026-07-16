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

**Fragment generation note:**
Zeeker calls `fetch_data()` twice per build (once for main insert, once for fragment context).
The second call returns `[]` because all records are now "existing". A module-level cache
`_pending_for_fragments` bridges the two calls — populated on the first call, consumed by
`fetch_fragments_data()`, only updated when the call returns non-empty results.

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
`resources/_backfill.py` (imported by each resource via a `sys.path` sibling-module
shim, because zeeker loads resource files by path and bypasses package imports);
its pure functions are unit-tested in `tests/test_backfill.py` (`uv run pytest`).
`fetch_data()` applies the same <50-chars extraction-success threshold as the
backfill: a too-short Docling result is treated as a failed extraction (logged to
stderr) and left for the backfill to retry, instead of being silently stored.

**Full-text search note:**
FTS fields are declared in `zeeker.toml` (`fts_fields`, `fragments_fts_fields`).
The sync workflow rebuilds the FTS indexes on every run via `scripts/setup_fts.py`
(idempotent: `enable_fts(..., replace=True)` + triggers), because zeeker's
`--setup-fts` errors when the FTS table already exists and so can't run on
incremental builds. Without this step the PDPC tables are absent from the upstream
FTS index and the MCP `search` tool silently skips them.

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

## Build Monitoring Guide (for AI agents)

This section helps AI agents monitoring the build pipeline interpret log output correctly.

### Resources and what "no data returned" means

All 3 resources in this repo **require the Tailscale SOCKS5 proxy** (`socks5h://172.17.0.1:1055`) because PDPC uses CloudFront which blocks datacenter IPs. When the proxy is down (Tailscale exit node offline), ALL resources fail.

| Resource | Source | Normal "no data" cause | Abnormal "no data" cause |
|----------|--------|----------------------|------------------------|
| `enforcement_decisions` | PDPC listing API + detail pages | All decisions already imported (weekly cadence — 0 new most runs) | **ProxyError** fetching listing API or detail pages. Duration >30s. |
| `guidance_by_topic` | PDPC listing API + detail pages | All guidance items already imported | **ProxyError** — same as above. Duration >1000s (very slow). |
| `regulatory_guidance` | PDPC listing API + detail pages | All guidance items already imported | **ProxyError** — same as above |

### Fragment backfill

Each build also runs a **fragment backfill** — extracting PDF text via Docling for decisions/guidance that don't have fragments yet. Every log line is prefixed with the resource name (the three resources share workflow jobs, so unprefixed lines were unattributable). A run shows as:
```
enforcement_decisions: Fragment backfill: 14 pending (2 re-eligible after quarantine TTL), 114 quarantined total — processing 10 this run.
  enforcement_decisions: OK  a1b2c3d4 | Breach of the Protection Obligation by ... | 12 chunks
  enforcement_decisions: FAIL e5f6a7b8 | ReadTimeout: timed out
enforcement_decisions: Fragment backfill done: 4 OK, 6 failed, 8 still pending, 116 quarantined total (+2 this run).
```
- **OK** = PDF extracted and chunked successfully
- **failed** = Docling couldn't convert the PDF this batch (timeout, corrupt PDF, ReadTimeout). Failure lines always include the exception class (e.g. `ReadTimeout: ...`).
- **still pending** = un-attempted candidates PLUS this batch's failed-but-not-quarantined items — everything that WILL be retried next build
- **quarantined total** = items at/over the retry cap (default 3 failures), with the delta for this run in parentheses. Quarantine is **not permanent**: items are retried again once their last attempt is older than `PDPC_BACKFILL_RETRY_AFTER` (default 14 days ≈ two weekly builds); a re-failure re-quarantines them for another TTL window. `PDPC_BACKFILL_RETRY_QUARANTINED=1` flushes the whole quarantine for one run.

This is normal — some PDFs are large or complex. The backfill progresses slowly (a few per build), and quarantined items get periodic re-attempts via the TTL.

### Normal yield expectations

- **enforcement_decisions:** 0–2 new per week (PDPC publishes decisions infrequently)
- **guidance_by_topic:** 0 new most weeks (guidance is rarely updated)
- **regulatory_guidance:** 0–2 new per week
- **Build duration:** 5–45 minutes (dominated by PDF extraction via proxy + Docling)

### How to tell a healthy skip from a failure

Every log line is prefixed with the resource name (e.g. `enforcement_decisions: ...`), and every run ends with a terminal status line: `<resource>: Done: N new ...` on healthy completion, or `<resource>: ABORTED (listing fetch failed: <TypeName>: <msg>) — 0 new` (stderr) when even listing page 1 could not be fetched. Abnormal control-flow lines (`Too many failures — stopping.`, aborts) go to stderr.

- **Healthy skip:** Log shows `<resource>: All items on page 1 already known — stopping.` followed by `<resource>: Done: 0 new ...`. Duration 2–10s.
- **Failed skip (proxy):** Log shows `<resource>: Routing via socks5h://172.17.0.1:1055` followed by an `ABORTED (listing fetch failed: RetryError: ...)` terminal line, or repeated `Listing page N failed: <TypeName>: ...` lines. Duration 600–3000s (retrying with backoff).
- **Failed PDF extraction:** Log shows `<resource>: PDF extract failed for <uuid>: RetryError[...ReadTimeout]` or `<resource>: PDF extract too short for <uuid> (N chars < 50) — leaving for backfill`. Build continues; the item stays in the fragment-backfill queue and is retried on later builds. Failure messages always start with the exception class name.

### Current DB stats (as of Jul 2026)

- enforcement_decisions: ~371 rows
- guidance_by_topic: ~79 rows
- regulatory_guidance: ~42 rows
- **Total: ~492 rows**

### Build schedule

Weekly on Wednesdays at 11:05 SGT (03:05 UTC). The long interval means "no data returned" is the norm — most weeks have 0 new PDPC publications.
