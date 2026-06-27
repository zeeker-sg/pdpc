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
