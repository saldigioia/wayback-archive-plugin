# Implementation Prompt

Copy everything below the line into Claude Code from inside this directory.

---

You are inside the `wayback-archive` skill — a pipeline for recovering product databases from defunct e-commerce sites using the Wayback Machine and CommonCrawl. Read `SKILL.md` first to understand the full architecture, then read `IMPROVEMENT_PLAN.md` to understand what's been done and what remains.

## Context

Three standalone scripts now exist that work but aren't wired into the automated pipeline:

- **`fetch_archive.py`** — Multi-strategy page fetcher. Three-step cascade: direct Wayback `id_` → CommonCrawl WARC (HTML only) → ISP proxy fallback. Async, resume-capable, content-validated.
- **`filter_cdx.py`** — CDX dump cleaner. Six-layer filter that reduces raw CDX dumps by ~95% with zero product data loss.
- **`wayback_domain_dump.py`** — CDX dump builder (user's existing tool, imports `wayback_cdx.cli`).

The automated pipeline runner is `scripts/run_stage.py`. Its `run_fetch()` function still uses an old `build_transport()` layer that doesn't know about CommonCrawl WARCs, proxy rotation, or the direct-first cascade. The `run_index()` function doesn't call `filter_cdx.py` or query CommonCrawl indices.

## Your task

Implement the remaining priorities from `IMPROVEMENT_PLAN.md` in four phases. Do NOT modify `fetch_archive.py` or `filter_cdx.py` — they are tested and working. Build around them.

### Phase 1: Wire `run_stage.py` to use the new scripts

**Goal**: Make `python3 run_stage.py fetch` use `fetch_archive.py`'s cascade instead of the old transport layer.

1. Read `scripts/run_stage.py`, `fetch_archive.py`, and `filter_cdx.py` to understand the interfaces.
2. Add a new `run_filter` stage (or integrate into `run_index`) that:
   - Takes the CDX dump paths from the site config
   - Runs `filter_cdx.py` on each, producing a filtered `links.txt`
   - Logs the before/after URL counts
3. Rewrite `run_fetch()` to:
   - Import and call `fetch_archive.py`'s `run()` coroutine directly (it's in the same repo — import it, don't subprocess it)
   - Pass the filtered `links.txt` as input
   - Map the downloaded HTML files back to the existing metadata extraction flow (`extract_image_urls`, `extract_shopify_metadata`, etc.)
   - Preserve checkpoint/resume behavior via the existing `StageCheckpoint`
4. Update the `stages` dict and CLI to include the new stage ordering.
5. Keep backward compatibility: if the old `transport_pkg` config key exists, warn that it's deprecated but don't crash.

### Phase 2: Add CommonCrawl discovery to `run_index`

**Goal**: Implement IMPROVEMENT_PLAN Priority 3's discovery piece — query CommonCrawl indices during the index stage so we know which URLs have WARC captures before fetching.

1. Read `IMPROVEMENT_PLAN.md` Part 2 (sections 2.1 and 2.2) for the `wayback` library and `cdx_toolkit` approaches.
2. Add a `run_cc_discovery()` function that:
   - Takes the domain list from the site config
   - Queries `index.commoncrawl.org` for `/products/*`, `/collections/*`, and root paths across all domains
   - Uses the same `CC_CRAWLS` list from `fetch_archive.py` (import it, don't duplicate)
   - Rate-limits to 1 req/s against the CC index
   - Saves results to `{name}_commoncrawl_index.json` with WARC coordinates (filename, offset, length, crawl)
   - Merges discovered handles into the product index (dedup against existing)
3. Wire this into `run_index` as a second pass after the local CDX parse.

### Phase 3: Circuit breaker and observability

**Goal**: Implement IMPROVEMENT_PLAN Priorities 6 (remaining) and 7.

1. Add a `CircuitBreaker` class to `fetch_archive.py`'s parent module (or a new `lib/wayback_archiver/resilience.py`):
   - Track consecutive failures per domain
   - After 3 consecutive failures → pause 120 seconds
   - After 6 → pause 300 seconds
   - After 10 → skip domain entirely, log warning
   - Expose `--max-retries` and `--backoff-factor` as CLI args in `run_stage.py`
2. Add per-stage timing and method-level success counters:
   - Each stage logs wall time on completion
   - `run_fetch` logs success/failure counts broken down by method (direct, commoncrawl, proxy, playwright)
   - Results written to `{name}_fetch_stats.json`
3. Update `scripts/status_report.py` to read and display `_fetch_stats.json` if it exists.

### Phase 4: Alternative archive sources

**Goal**: Implement IMPROVEMENT_PLAN Priority 5.

1. Add `archive_today_lookup(url)` — queries `https://archive.ph/timemap/json/{url}` for available captures. Returns a list of snapshot URLs.
2. Add `memento_lookup(url)` — queries `https://timetravel.mementoweb.org/timemap/json/{url}` for cross-archive captures.
3. Wire both into `fetch_archive.py`'s cascade as Step 4 (after proxy, before giving up) — but do this via a plugin/hook pattern so `fetch_archive.py` itself doesn't need modification. For example, add a `--fallback-archives` flag to `run_stage.py` that wraps `fetch_one()`.
4. Add `alternative_archives:` section to `references/site-config-schema.md`.
5. Update `configs/yeezygap.yaml` as an example.

## Ground rules

- Read before writing. Read every file you're about to modify. Read the SKILL.md extraction hierarchy before making architectural decisions.
- Don't duplicate logic. Import from `fetch_archive.py` and `filter_cdx.py` rather than copying their code.
- Don't break what works. The three standalone scripts are tested. `run_stage.py`'s other stages (match, download, normalize, build) must keep working unchanged.
- Checkpoint everything. Every new stage must save progress and be resumable after interruption.
- Log generously. Every network request should log its URL, method, result, and timing at DEBUG level. Summaries at INFO.
- Test with dry-run. Every new stage must support `--dry-run` that shows what it would do without side effects.
- Update docs. After each phase, update IMPROVEMENT_PLAN.md to mark the priority as completed. Update SKILL.md if the orchestration flow changes.
- Commit after each phase. Four separate commits, one per phase.
