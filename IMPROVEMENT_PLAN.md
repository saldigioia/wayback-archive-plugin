# Wayback Archive Pipeline — Improvement Plan

**Date:** April 8, 2026
**Scope:** Diagnose persistent scraping failures, improve Wayback Machine fetching, integrate alternative archives, and implement URL filtering guardrails.

---

## Part 1: Root Cause Diagnosis — Why the Agent Consistently Fails

After a thorough review of every file in the codebase (SKILL.md, all 7 agent specs, 5 reference docs, 3 configs, and both scripts), the pipeline's scraping failures trace to **five structural problems** that compound each other.

### 1.1 The Fetcher Agent Contradicts the SKILL.md

This is the single most damaging inconsistency in the codebase.

The **SKILL.md** (the pipeline's master document) explicitly states:

> *"Rule 2: For HTML content, prefer CommonCrawl WARCs over Wayback HTML."*
> *"Wayback serves HTML through a JavaScript replay framework. curl gets a ~3KB shell. Playwright renders the Wayback toolbar, not the archived page. Even when Playwright works, ~80% of captures are anti-bot (Akamai) pages."*

It ranks the extraction method hierarchy as:
1. Existing local data
2. Structured endpoints (curl with `id_`)
3. **CommonCrawl WARCs** (raw HTML)
4. Wayback API JSON (curl with `id_`)
5. Playwright with network interception (last resort)

But the **fetcher agent** (`agents/fetcher.md`) tells a completely different story:

> *"Never use curl to fetch HTML pages from the Wayback Machine."*
> *"Use Playwright (headless browser) for ALL HTML pages."*

The fetcher's process flow is: Phase A (JSON APIs) → Phase B (Collection Pages via Playwright) → Phase C (Individual Product Pages via Playwright). **CommonCrawl WARCs are never mentioned in the fetcher agent at all.** The word "CommonCrawl" does not appear in the fetcher spec.

This means the agent that actually does the HTML fetching has been instructed to use Playwright as its primary HTML strategy — the very approach that the SKILL.md's own data shows has an ~80% failure rate on anti-bot pages, suffers from `networkidle` hangs, and mostly captures the Wayback toolbar rather than archived content.

The `playwright-wayback.md` reference doc opens with: *"This is the last-resort extraction method. Use CommonCrawl WARCs first."* Then immediately explains *"Why Playwright Mostly Fails on Wayback."* Yet the fetcher agent doesn't know CommonCrawl WARCs exist.

**Impact:** The fetcher skips the method with 76% success rate (CommonCrawl WARCs) and goes straight to the method the pipeline's own documentation calls "mostly fails." This single contradiction explains the majority of scraping failures.

### 1.2 No CommonCrawl WARC Fetch Stage Exists in the Pipeline Runner

The `run_stage.py` script has six stages: `index`, `fetch`, `match`, `download`, `normalize`, `build`. The `run_fetch` function uses a "transport" layer to fetch pages, but there is no CommonCrawl WARC fetching logic anywhere in the code. The SKILL.md describes CommonCrawl WARC fetching with `curl` and HTTP Range headers, and Phase 1B describes CommonCrawl index querying, but no stage in `run_stage.py` implements either of these.

CommonCrawl is documented as a concept but never wired into the executable pipeline.

### 1.3 The Discovery Phase (1B) Has No Implementation

Phase 1B (CommonCrawl Index — Handle Discovery) is described in the SKILL.md but has no corresponding code path. The `run_index` function in `run_stage.py` only calls `parse_cdx()` on local CDX dump files. It never queries `index.commoncrawl.org` for WARC coordinates. This means the pipeline never discovers which pages have CommonCrawl captures, and therefore can never use those captures during extraction.

### 1.4 The Wayback Landscape Has Shifted Dramatically (2025-2026)

The pipeline was "battle-tested" on yeezysupply.com, but conditions have changed:

- **Publisher blocking surged in 2025:** 241 news sites from 9 countries now block Internet Archive crawlers via robots.txt. Page captures among news publications dropped 87% between May and October 2025. Major platforms (Reddit, NYT, Guardian) actively hard-block archive.org crawlers.
- **CDX rate limits tightened:** CDX requests are now limited to 60/minute average, with 429 responses leading to 1-hour IP blocks that double on each subsequent violation. The pipeline's `run_fetch` stage has no rate-limiting logic beyond a `CDX_MAX_RPS=1` environment variable.
- **CDX result ceiling:** The CDX server silently drops results beyond ~150,000 records. The SKILL.md warns about pagination but the `run_index` stage doesn't implement pagination.
- **CommonCrawl raised truncation limits:** As of March 2025 (CC-MAIN-2025-13), the WARC record truncation threshold increased from 1 MiB to 5 MiB, meaning more complete page captures are now available in CommonCrawl.

### 1.5 No URL Filtering Before Fetch Attempts

The `junk_patterns` in the config catch some malformed URLs, but the pipeline has no pre-fetch filter that removes:
- `robots.txt` captures
- `.js` and `.css` file captures
- Tracking/analytics endpoints (`wpm@`, `sandbox/modern`)
- Redirect/error captures (30x, 40x status codes leaking through)
- Duplicate content across snapshots (no `collapse=digest` in CDX queries)

The indexer's `parse_cdx()` applies `url_rules` and `junk_patterns`, but these are pattern-matches on path structure. There is no content-type or MIME-type filter that operates at the CDX query level (e.g., `filter=mimetype:text/html`), and no CDX-level deduplication (`collapse=digest` or `collapse=urlkey`).

---

## Part 2: Effective Methods for Scraping the Wayback Machine

### 2.1 Use the `edgi-govdata-archiving/wayback` Python Library

This is a mature, purpose-built Python API for the Wayback Machine with built-in rate limiting. Key features:

- **Adjustable rate limits:** Default 1 call/second for CDX search, 30 calls/second for memento retrieval. Both are configurable — set to 0 to disable, or share a single `RateLimit` instance across sessions.
- **Automatic retry on 429s:** The library delays retries for 60 seconds when rate-limited.
- **Memento API support:** `client.get_memento(record)` fetches the actual archived content with proper handling of redirects and Wayback-specific response formats.
- **CDX search pagination:** Built-in handling of paginated CDX results.

**Integration point:** Replace the custom transport layer in `run_fetch` with the `wayback` library's `WaybackClient`. This gives rate limiting, retry logic, and CDX pagination for free.

```python
import wayback
client = wayback.WaybackClient()
for record in client.search(url, from_date=start, to_date=end):
    memento = client.get_memento(record)
    # memento contains the raw archived content
```

### 2.2 Use `cdx_toolkit` for Unified CDX + WARC Access

The `cdx_toolkit` library (maintained by Common Crawl) provides a single interface to both Wayback Machine and CommonCrawl CDX indices. Critical capability: it can fetch WARC records directly.

- **Cross-archive queries:** `cdx_toolkit.CDXFetcher(source='cc')` for CommonCrawl, `cdx_toolkit.CDXFetcher(source='ia')` for Internet Archive.
- **WARC extraction:** `record.fetch_warc_record()` returns the raw WARC record using HTTP Range headers.
- **Crawl knitting:** Automatically queries across all CommonCrawl monthly indices, treating them as a single virtual index.

**Integration point:** Add a new discovery sub-stage (1B) that uses `cdx_toolkit` with `source='cc'` to query all CommonCrawl crawls for the target domain. Save WARC coordinates. Then in Phase 2, use `fetch_warc_record()` to retrieve raw HTML.

### 2.3 Prefer `id_` + Raw Content Over Playwright

For structured data (JSON, XML, Atom, oEmbed), `curl` with `id_` is reliable and the pipeline already documents this. But for HTML, the `id_` modifier does NOT bypass the JavaScript replay wrapper — this is correctly noted in the SKILL.md.

The hierarchy should be enforced in code:
1. **CommonCrawl WARC** (raw HTTP response, no wrapper, no anti-bot)
2. **Wayback `id_` for structured endpoints** (JSON, Atom, oEmbed)
3. **Playwright with network interception** (last resort, for CDN URL discovery only)

### 2.4 Implement Proper Rate Limiting

The current pipeline has `CDX_MAX_RPS=1` as an environment variable but no enforcement in the fetch loop. Implement:

- **CDX queries:** Max 0.5 requests/second (below the 1/second threshold that triggers 429s)
- **Memento/page fetches:** Max 2 requests/second with exponential backoff on failure
- **CommonCrawl data.commoncrawl.org:** Max 1 request/second (their stated limit)
- **Global circuit breaker:** If 3 consecutive requests return 429 or connection refused, pause for 120 seconds before resuming

### 2.5 Batch CDX Queries with Server-Side Filtering

Instead of fetching all CDX records and filtering locally, push filters to the CDX server:

```bash
curl -s "https://web.archive.org/cdx/search/cdx?\
url={domain}/products/*&output=json&\
fl=timestamp,original,statuscode,mimetype,digest&\
collapse=digest&\
filter=statuscode:200&\
filter=!mimetype:application/javascript&\
filter=!mimetype:text/css&\
limit=5000&page=0"
```

Key parameters:
- `collapse=digest` — deduplicates by content hash (eliminates identical snapshots)
- `filter=statuscode:200` — only successful captures
- `filter=!mimetype:application/javascript` — exclude JS files
- `page=N` — proper pagination

---

## Part 3: Alternative Internet Archives

### 3.1 Common Crawl (Primary Alternative — Already Documented, Not Implemented)

Common Crawl is the pipeline's most important alternative source, and its advantages over Wayback for HTML content are well-documented in the SKILL.md (76% vs 2.4% success rate). But it has never been implemented.

**What Common Crawl provides:**
- Raw HTTP responses in WARC format (no JavaScript wrapper)
- Independent crawl schedule (~6 crawls/year)
- No rate-limiting concerns for data access (hosted on AWS S3)
- WARC truncation limit increased to 5 MiB as of March 2025

**How to fetch a single page from Common Crawl:**
1. Query the CDX index: `https://index.commoncrawl.org/CC-MAIN-{CRAWL}-index?url={url}&output=json`
2. Extract `filename`, `offset`, `length` from the response
3. Fetch the WARC record: `curl -sH "Range: bytes={offset}-{offset+length-1}" "https://data.commoncrawl.org/{filename}" | gunzip`
4. Parse the HTTP response body as HTML

**Optimal integration point:** Between the CDX index stage and the Playwright fetch stage. Query Common Crawl for all handles discovered in Phase 1A. For any handle with a Common Crawl capture, fetch the WARC record instead of using Playwright.

**Cost optimization:** Access from `us-east-1` to avoid inter-region S3 transfer fees.

### 3.2 Archive.today (Supplementary — On-Demand Captures)

Archive.today captures pages on demand and does not obey robots.txt. It uses the Memento API for programmatic access. An unofficial Node.js/TypeScript library (`archivetoday`) supports creating and fetching snapshots.

**When to use:** For high-value pages that Wayback Machine doesn't have and CommonCrawl didn't crawl. Particularly useful for pages blocked by robots.txt on other archives.

**Limitation:** The Memento Project was disestablished in September 2025 at LANL, but archive.today's own Memento endpoint may still function. Verify before depending on it.

**Integration point:** Add as a Phase 2 fallback after CommonCrawl WARCs fail and before Playwright. Query `https://archive.today/timemap/json/{url}` for available captures.

### 3.3 Memento Time Travel (Meta-Search Aggregator)

Memento Time Travel (`timetravel.mementoweb.org`) queries dozens of web archives simultaneously and returns results from whichever archive has a snapshot closest to a target date.

**When to use:** For gap recovery. After Wayback + CommonCrawl + archive.today have been exhausted, a Memento Time Travel query can surface captures from niche archives (national libraries, university archives, domain-specific collections).

**Integration point:** Add as a final discovery vector in Phase 1. For handles with zero captures across primary sources, query Memento for any archive that has them.

### 3.4 Perma.cc (Academic/Legal Archives)

Perma.cc creates permanent, unchangeable links to web pages. Used by universities and legal institutions. Limited to curated captures, but may have snapshots of pages that were cited in academic or legal contexts.

**When to use:** For very high-value pages that appear in academic citations or legal filings.

### 3.5 Bright Data Web Archive API (Commercial)

Bright Data offers a commercial web archive API with access to billions of cached pages. Pay-per-query model.

**When to use:** Last resort for commercially valuable recoveries where free sources have been exhausted.

---

## Part 4: URL Filtering Guardrails

### 4.1 Pre-Discovery Filter (CDX Query Level)

Apply these filters at the CDX API query level so junk never enters the pipeline:

```python
CDX_QUERY_FILTERS = {
    "status_whitelist": ["200"],       # Only successful captures
    "mime_blacklist": [
        "application/javascript",       # .js files
        "text/css",                     # .css files
        "image/x-icon",                # favicons
        "application/x-javascript",     # legacy JS mime
        "text/javascript",              # another JS mime
        "application/octet-stream",     # binary blobs
    ],
    "collapse": "digest",              # Deduplicate by content hash
}
```

### 4.2 Post-Discovery Filter (URL Pattern Level)

After CDX results are returned, apply a multi-layer URL filter before any fetch attempts:

```python
JUNK_URL_PATTERNS = [
    # Infrastructure files
    r'/robots\.txt$',
    r'/sitemap\.xml$',
    r'/favicon\.ico$',
    r'/apple-touch-icon',
    r'\.js(\?|$)',
    r'\.css(\?|$)',
    r'\.map(\?|$)',              # Source maps
    r'\.woff2?(\?|$)',           # Web fonts
    r'\.ttf(\?|$)',
    r'\.eot(\?|$)',

    # Tracking and analytics
    r'/wpm@',
    r'/sandbox/modern',
    r'/cdn-cgi/',                # Cloudflare infrastructure
    r'/__cf_chl_',              # Cloudflare challenge
    r'/gtag/',                  # Google Tag Manager
    r'/analytics',
    r'/_vercel/',               # Vercel infrastructure

    # Shopify infrastructure (not product data)
    r'/shopifycloud/',
    r'/monorail/',
    r'/web-pixels-manager',
    r'/checkouts/',             # Checkout pages (no product data)
    r'/cart$',                  # Cart page (not cart.json API)
    r'/account/',               # Account pages

    # URL-encoded garbage
    r'%22|%3[CcEe]|%7[Bb]|%5[Bb]|%0[Aa]',
    r'undefined|:productId|\[insert',

    # Image optimization params leaked into paths
    r'=center|=fast|=pad',

    # Common non-product paths
    r'/admin/',
    r'/apps/',
    r'/services/',
    r'/policies/',
    r'/pages/(?!products)',     # /pages/ that aren't product-related
]

REQUIRED_PATH_PATTERNS = [
    # At least one of these must match for a URL to be kept
    r'/products/',
    r'/collections/',
    r'/api/products',
    r'/products\.json',
    r'\.atom$',
    r'\.oembed$',
    r'/__data\.json',
    r'/cdn/shop/',
    r'cdn\.shopify\.com.*/products/',
    r'cdn\.swell\.store/',
    r'imgproxy\.fourthwall\.com/',
]
```

### 4.3 Content Validation Filter (Post-Fetch)

After fetching, validate that the content is actually product data:

```python
def is_valid_product_content(content: bytes, content_type: str) -> bool:
    """Reject fetched content that isn't product data."""

    # Size check: Wayback JS wrapper is ~3KB, real pages are 30-80KB
    if len(content) < 5000 and content_type == "text/html":
        return False  # Likely Wayback wrapper

    text = content.decode("utf-8", errors="ignore")

    # Anti-bot detection
    ANTIBOT_SIGNATURES = [
        "Access Denied",
        "Akamai Technologies",
        "akamaized.net",
        "cf-browser-verification",
        "challenge-platform",
        "Checking your browser",
        "Just a moment",           # Cloudflare
        "Enable JavaScript and cookies",
    ]
    for sig in ANTIBOT_SIGNATURES:
        if sig in text:
            return False

    # Wayback wrapper detection
    if "_wm.wombat" in text or "WBWombatInit" in text:
        if len(text) < 10000:  # Small wrapper with no real content
            return False

    return True
```

### 4.4 Guardrail Enforcement Points in the Pipeline

| Pipeline Stage | Guardrail | Purpose |
|----------------|-----------|---------|
| Phase 1A (CDX Query) | Server-side `filter=` and `collapse=` | Reduce result volume, eliminate duplicates |
| Phase 1A (Post-Query) | `JUNK_URL_PATTERNS` filter | Remove infrastructure/tracking URLs |
| Phase 1A (Post-Query) | `REQUIRED_PATH_PATTERNS` check | Ensure URLs are product-related |
| Phase 1B (CommonCrawl) | Same URL filters | Consistent filtering across sources |
| Phase 2 (Pre-Fetch) | CDX cross-reference | Don't fetch URLs with no captures |
| Phase 2 (Post-Fetch) | `is_valid_product_content()` | Reject anti-bot pages and Wayback wrappers |
| Phase 2 (Post-Fetch) | Size threshold (< 5KB for HTML) | Catch Wayback JS wrapper responses |
| Phase 3 (Pre-Download) | `is_product_image()` filter | Remove favicons, logos, tracking pixels |
| Phase 3 (Pre-Download) | `canonicalize_image_url()` dedup | Eliminate size variant duplicates |
| Phase 3 (Post-Download) | Magic byte validation | Catch HTML masquerading as images |

---

## Part 5: Implementation Roadmap

### Priority 1: Fix the Fetcher-SKILL.md Contradiction (Critical) ✅ COMPLETED

**What:** Rewrite `agents/fetcher.md` to align with the SKILL.md's extraction hierarchy. CommonCrawl WARCs must be the primary HTML extraction method, not Playwright.

**Status:** Completed April 8, 2026. The fetcher agent has been fully rewritten:
1. Replaced "Never use curl to fetch HTML pages" with "CommonCrawl WARCs for HTML, Proxy for Structured Data"
2. Added `fetch_archive.py` as the primary fetch tool with the full CC WARC → proxy fallback cascade
3. Demoted Playwright to Phase D (last resort, for CDN URL discovery only)
4. Added `filter_cdx.py` as a mandatory pre-fetch step
5. Updated Playwright guidance: `domcontentloaded` instead of `networkidle`, 8s wait
6. All cross-references to SKILL.md extraction hierarchy are now consistent

### Priority 2: Implement CommonCrawl Discovery + Fetching (Critical) ✅ COMPLETED

**What:** Add the missing Phase 1B (CommonCrawl Index query) and Phase 2C (CommonCrawl WARC fetch).

**Status:** Completed April 8, 2026 via `fetch_archive.py`:
1. `cc_index_lookup()` queries up to 4 CommonCrawl crawl indices per URL (from 24 available, CC-MAIN-2022-05 through CC-MAIN-2026-09) with domain-level negative caching (skip CC after 3 misses for a domain)
2. `fetch_cc_warc()` fetches raw WARC records via HTTP Range requests to S3, decompresses, and parses HTTP response body
3. Three-step cascade: direct Wayback `id_` first (no proxy) → CC WARC (HTML only) → ISP proxy fallback → content validation
4. Three separate semaphores: direct (10), proxy (configurable workers, default 5), CC (4)
5. Async with resume support, dry-run mode, ISP/DC proxy selection
6. Proxy rotation across Oxylabs ISP ports 8001-8020 (20 distinct residential IPs)

### Priority 3: Integrate the `wayback` Python Library (High)

**What:** Replace the custom transport layer with `edgi-govdata-archiving/wayback` for built-in rate limiting, retry logic, and CDX pagination.

**Changes:**
1. Install the `wayback` library
2. Replace `build_transport(AppConfig.from_env())` in `run_fetch()` with `wayback.WaybackClient()`
3. Use `client.search()` for CDX queries (gets pagination for free)
4. Use `client.get_memento()` for page fetching (gets rate limiting for free)
5. Configure shared `RateLimit` instance across all pipeline stages

### Priority 4: Implement URL Filtering Guardrails (High) ✅ COMPLETED

**What:** Add the multi-layer filtering system described in Part 4 to prevent junk from entering the pipeline.

**Status:** Completed April 8, 2026 via `filter_cdx.py`:
1. Six-layer filter: status whitelist (200 only), MIME blacklist, junk path regex (robots.txt, sitemaps, checkouts, tracking, size charts, etc.), static asset extension regex, Shopify variant noise regex, query param stripping + dedup
2. Tested on yeezygap.com CDX dump: 4,246 → 231 URLs (94.6% reduction, zero product data lost)
3. Output sorted by value tier: oEmbed (0) → Atom (1) → JSON (2) → Collection (3) → Product (4) → Other (5)
4. Stats to stderr, clean URLs to stdout — integrates cleanly into the three-step workflow
5. Post-fetch validation implemented in `fetch_archive.py`: anti-bot signature detection (Akamai, Cloudflare), Wayback wrapper rejection, size gating (<5KB HTML = wrapper)

### Priority 5: Add Alternative Archive Sources (Medium)

**What:** Add archive.today and Memento Time Travel as supplementary discovery and extraction sources.

**Changes:**
1. Add an `archive_today_discovery()` function that queries archive.today's timemap API
2. Add a `memento_discovery()` function that queries Memento Time Travel for gap handles
3. Add these as optional discovery vectors in Phase 1, controllable via config
4. Update the site config schema with `alternative_archives:` section

### Priority 6: Harden Rate Limiting and Error Handling (Medium) — Partially Addressed

**What:** Implement proper rate limiting, circuit breakers, and error classification across the pipeline.

**Partial progress (April 8, 2026):** `fetch_archive.py` implements:
- Oxylabs ISP proxy rotation (20 IPs via ports 8001-8020) for Wayback rate-limit avoidance
- Exponential backoff with port rotation on failure (3 retries per URL)
- Content validation that detects anti-bot pages and Wayback wrappers
- Async concurrency control (configurable workers, default 5)

**Remaining:**
1. Global circuit breaker (3 consecutive failures → pause) — not yet implemented
2. Per-domain rate limiter class — not yet implemented (proxy rotation handles this implicitly)
3. `--max-retries` and `--backoff-factor` CLI arguments — not yet exposed
4. Integration with `run_stage.py` pipeline runner — `fetch_archive.py` runs standalone

### Priority 7: Pipeline Observability (Low)

**What:** Add logging and metrics to understand where the pipeline spends its time and where it fails.

**Changes:**
1. Add per-stage timing metrics
2. Add per-method success/failure counters (CommonCrawl WARC, Wayback JSON, Playwright)
3. Add a `--verbose` flag that logs every fetch attempt with method, URL, result, and duration
4. Update `status_report.py` to show method-level success rates

---

## Appendix: Key Research Sources

- [edgi-govdata-archiving/wayback](https://github.com/edgi-govdata-archiving/wayback) — Python Wayback API with rate limiting
- [cdx_toolkit](https://github.com/commoncrawl/cdx_toolkit) — Unified CDX + WARC access for CommonCrawl and Wayback
- [Wayback CDX Server API](https://github.com/internetarchive/wayback/tree/master/wayback-cdx-server) — Official CDX API documentation
- [Common Crawl Get Started](https://commoncrawl.org/get-started) — CommonCrawl data access guide
- [Common Crawl January 2026 crawl](https://commoncrawl.org/blog/january-2026-crawl-archive-now-available) — Latest crawl archive
- [Publishers blocking Wayback Machine (2026)](https://www.niemanlab.org/2026/01/news-publishers-limit-internet-archive-access-due-to-ai-scraping-concerns/) — 241 sites now block IA crawlers
- [Wayback Machine APIs](https://archive.org/help/wayback_api.php) — Official API documentation
- [archivetoday (unofficial API)](https://github.com/HRDepartment/archivetoday) — Programmatic access to archive.today
- [CDX rate limiting details](https://github.com/edgi-govdata-archiving/wayback/issues/137) — 60 req/min limit, 1-hour IP blocks
- [Restoring Wayback Machine HTML](https://skeptric.com/restoring-wayback-html/) — id_ modifier documentation
