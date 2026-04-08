---
name: wayback-archive
description: >
  Recover complete product databases (catalog data + images) from defunct
  e-commerce websites using the Wayback Machine and CommonCrawl. Use this
  skill whenever the user mentions recovering products from a dead website,
  archiving a defunct online store, rebuilding a product catalog from web
  archives, finding products from a closed Shopify/e-commerce store, or
  scraping Wayback Machine for product data. Also trigger when working with
  CDX dumps, WARC files, CommonCrawl indexes, or Shopify CDN URLs for
  archival purposes — even if the user doesn't explicitly say "wayback" or
  "archive." Trigger on: "archive", "recover images", "CDX dump", "wayback",
  "CommonCrawl", "WARC", "product database", "defunct site", "dead site",
  "HAR file", "Playwright scrape". Battle-tested: recovered 1,229 products
  with 3,578 images (1.1 GB) from yeezysupply.com across a 9-year,
  multi-platform store lifecycle.
---

# Wayback Archive Pipeline

Three-phase pipeline for recovering product databases from defunct e-commerce
sites via the Wayback Machine, CommonCrawl, and live CDN persistence. Each
phase has clear data contracts and checkpoint/resume support.

This skill was forged in real recoveries of yeezysupply.com (2015-2024),
yeezy.com, and yeezygap.com — stores that migrated across multiple Shopify
instances, Adidas platforms, Swell Commerce, and Fourthwall. Every rule below
exists because its opposite was tried and failed.

**Architecture**: Three phases in strict order — discovery must complete
before extraction begins. This is enforced because the original Yeezy Supply
recovery skipped exhaustive discovery and missed 663 products.

```
Phase 1: DISCOVERY       -> Find what existed (fast, cheap, exhaustive)
Phase 2: EXTRACTION      -> Get product data (slow, requires triage)
Phase 3: ASSET DOWNLOAD  -> Get images/media (CDN-first)
```

---

## The Two Rules That Matter Most

> **Rule 1: Always query BOTH Wayback Machine AND CommonCrawl.**
>
> They have independent crawl schedules and different coverage. In the Yeezy
> Supply recovery, CommonCrawl yielded **135 products** (76% success) while
> Wayback HTML yielded only **3 products** (2.4% success). CommonCrawl was
> the single best content source — yet the original recovery never queried
> it at all, missing 663 products.
>
> **Rule 2: For HTML content, prefer CommonCrawl WARCs over Wayback HTML.**
>
> Wayback serves HTML through a JavaScript replay framework. `curl` gets a
> ~3KB shell. Playwright renders the Wayback toolbar, not the archived page.
> Even when Playwright works, ~80% of captures are anti-bot (Akamai) pages.
>
> CommonCrawl WARCs contain the **raw HTTP response** — the exact bytes the
> crawler received. No JavaScript wrapper, no replay framework, no anti-bot
> redirect. Product data is directly in the HTML.
>
> ```bash
> # CommonCrawl WARC fetch — raw content, no wrapper
> curl -s -H "Range: bytes={offset}-{offset+length-1}" \
>   "https://data.commoncrawl.org/{warc_file}" | gunzip
> ```
>
> **Extraction method hierarchy** (in order of preference):
> 1. Existing local data (free, instant)
> 2. Wayback `id_` direct (no proxy — fastest, cheapest)
> 3. CommonCrawl WARCs (raw HTML, no JS wrapper — best for HTML when direct fails)
> 4. Wayback `id_` via ISP proxy (fallback when direct is rate-limited)
> 5. Playwright with network interception (last resort, mostly fails)
>
> **Rule 3: Filter the CDX dump before fetching anything.**
>
> Raw CDX dumps contain 90%+ junk — redirects, JavaScript files, favicons,
> tracking endpoints, `robots.txt`, checkout pages, Shopify variant checkers.
> Run `filter_cdx.py` on every CDX dump before feeding URLs to the fetcher.
> The yeezygap.com CDX dump went from 4,246 to 231 URLs (94.6% reduction)
> with zero product data lost.

---

## Scripts

### fetch_archive.py — Multi-Strategy Archived Page Fetcher

The primary fetching tool. Reads a filtered `links.txt` (one Wayback URL per
line) and fetches each page using a tiered cascade with Oxylabs ISP/DC proxy
rotation and CommonCrawl WARC lookups:

```
All tiers:  Direct Wayback id_ first (no proxy, fastest)
  ↓ if rate-limited or fails
Tier 1: oEmbed / Atom / JSON  →  proxy fallback
Tier 2: HTML product pages    →  CommonCrawl WARC lookup, then proxy fallback
Tier 3: Collection / homepage →  CommonCrawl WARC lookup, then proxy fallback
```

The script tries direct Wayback `id_` first for every URL (cheapest, fastest).
If that fails or gets rate-limited, HTML pages fall back to CommonCrawl WARC
lookup (queries up to 4 crawl indices per URL with domain-level negative
caching), then proxy. Structured endpoints fall back directly to proxy.

Proxied requests rotate round-robin across Oxylabs ISP ports 8001-8020 (20
distinct residential IPs). CommonCrawl WARC fetches go direct to S3 (no proxy
needed, no rate limiting). Content is validated post-fetch: anti-bot signature
detection, Wayback wrapper rejection, size gating.

```bash
# Standard run
python fetch_archive.py links.txt

# Resume after interruption (skips existing files)
python fetch_archive.py links.txt --resume

# Use datacenter proxies instead of ISP
python fetch_archive.py links.txt --proxy dc

# Dry run — show fetch plan without downloading
python fetch_archive.py links.txt --dry-run
```

### filter_cdx.py — CDX Dump URL Filter

Transforms a raw CDX dump into a clean URL list. Six filter layers:

1. **Status whitelist**: Keep only HTTP 200 responses
2. **MIME blacklist**: Drop JS, CSS, fonts, images, video, `warc/revisit`
3. **Junk path patterns**: Drop `robots.txt`, `favicon.ico`, checkout pages,
   tracking endpoints, size charts, sitemaps, etc.
4. **Static asset extensions**: Drop `.js`, `.css`, `.map`, `.woff`, `.ico`
5. **Variant noise**: Drop Shopify `/variants/?section_id=store-availability`
6. **Deduplication**: One URL per unique path, preferring latest 200-status
   snapshot. Output sorted by value tier (structured data first).

```bash
python filter_cdx.py yeezygap_com_wayback.txt > links.txt
```

### Three-step workflow

```bash
# Step 1: Build CDX dump (your existing tool)
python wayback_domain_dump.py --domain yeezygap.com --output raw_cdx.txt

# Step 2: Filter junk
python filter_cdx.py raw_cdx.txt > links.txt

# Step 3: Fetch pages
python fetch_archive.py links.txt --resume
```

---

## Library

All deterministic logic lives in `~/lib/wayback_archiver/`. Import via:
```python
import sys; sys.path.insert(0, os.path.expanduser("~/lib"))
from wayback_archiver import normalize, cdx, extract, metadata, match, download, checkpoint
from wayback_archiver.site_config import load_config
```

---

## Pipeline Stages

### Phase 1: DISCOVERY (Exhaustive Handle Finding)

Run four independent discovery vectors in parallel. The CDX dump is your
roadmap — every successful fetch starts with "what does the CDX say about
this URL?" Never fetch blind.

**1A. Wayback CDX — Product URLs** (paginated, all subdomains)
```bash
curl -s "https://web.archive.org/cdx/search/cdx?\
url={domain}/products/*&output=json&fl=timestamp,original,statuscode,mimetype&\
collapse=urlkey&limit=5000"
```
Query ALL subdomain variants (bare, www., shop.). If any query returns
exactly `limit` results, **paginate using `resumeKey`** — failing to
paginate was the #1 discovery failure in the original recovery (hit a
500-result cap silently).

**1B. CommonCrawl Index — Handle Discovery**
```bash
# Returns NDJSON — parse line by line, not as JSON array
curl -s "https://index.commoncrawl.org/CC-MAIN-{CRAWL}-index?\
url={domain}/products/*&output=json"
```
Query all crawls covering the site's active years (~6 crawls/year). Save
WARC coordinates (filename, offset, length) for Phase 2. Rate-limit 2s
between queries. Retry timeouts once after 10s.

**1C. Collection/Category Pages** — Atom feeds (`.atom`), oEmbed (`.oembed`),
and HTML collection pages contain product handles. Fetch with `id_` modifier:
```bash
curl -s -L --compressed "https://web.archive.org/web/{ts}id_/{domain}/collections/{name}.atom"
```

**1D. CDN Image Filenames** — Query CDX for `cdn.shopify.com/s/files/*/products/*`
across ALL known store IDs. Image filenames reveal products even when pages
weren't captured (reverse-discovery vector).

**Verification gate** before proceeding:
- [ ] All subdomains queried
- [ ] No queries hit result cap without pagination
- [ ] All CommonCrawl crawls attempted
- [ ] Handles deduplicated against existing catalog (exact + fuzzy match)
- [ ] CDX dump filtered through `filter_cdx.py` (expect ~90-95% reduction)
- [ ] Junk removed: redirects, JS/CSS, favicons, robots.txt, checkouts, tracking, variant checkers
- [ ] Output sorted by value tier: structured data first, then HTML pages

- Library: `wayback_archiver.cdx.parse_cdx()`, `wayback_archiver.cdx.find_content_pages()`
- Output: `{name}_products_index.json`, `recovery/new_handles.json`

### Phase 2: EXTRACTION (Content + Metadata)

For each new handle, extract product metadata. **Triage first** — classify
handles by era/platform to choose the right extraction method:

| Handle type | Method | Why |
|------------|--------|-----|
| Server-rendered (Shopify-era) | CommonCrawl WARC first | Raw HTML has product data inline |
| SPA/API (Adidas/React) | API JSON via curl | HTML is empty app shell |
| Structured endpoints | curl with `id_` | .json/.atom/.oembed don't need JS |
| Anti-bot blocked | Skip, log | No workaround except HAR from human browsing |

**The extraction hierarchy** (in order of preference):

1. **Existing local data** — Parse files you already have (API JSONs, HTML pages, CSV exports). This recovered 57 products for free in the YS recovery.

2. **Wayback `id_` direct** — Try every URL directly first with the `id_` modifier and no proxy. This is the fastest and cheapest method. Works well for structured endpoints (JSON/Atom/oEmbed) and for HTML when Wayback isn't rate-limiting your IP.

3. **CommonCrawl WARCs** — When direct fails for HTML pages, `fetch_archive.py` queries up to 4 crawl indices per URL (with domain-level negative caching — if a domain misses 3 times, CC is skipped entirely for that domain). Raw WARC records fetched via HTTP Range requests to S3. No proxy needed, no rate limiting. Raw HTML with no JS wrapper, no anti-bot redirect. 76% success rate in testing.

4. **Wayback `id_` via ISP proxy** — Final automated fallback. The proxy provides IP rotation (20 residential IPs across ports 8001-8020) to avoid Wayback's 60 req/min rate limit and escalating IP blocks.

5. **Playwright with network interception** — Last resort. Use `domcontentloaded` (NOT `networkidle`), wait 8s, intercept network requests for CDN URLs. Accept most pages will fail. See `references/playwright-wayback.md` for the pattern.

**The `fetch_archive.py` script implements tiers 2-4 automatically.** It reads a filtered `links.txt`, classifies each URL by tier, and runs the cascade (direct → CC WARC → proxy) with async concurrency, three separate semaphores (direct: 10, proxy: workers, CC: 4), content validation (anti-bot detection, Wayback wrapper rejection), and resume support.

**The 90% rule**: If an extraction method fails on >90% of its first batch, the method is wrong for this era/platform. Stop and switch — don't retry.

- Library: `wayback_archiver.extract.*`, `wayback_archiver.metadata.*`
- Output: `{name}_metadata.json` + `links/{slug}.txt`

### Phase 2.5: MATCH + DEDUP

Fuzzy-match slug-based products to SKU-based products. Deduplicate cross-source
entries. Remove noise: colorways already covered, draft/test entries, aliases.
~40% of "missing" products are noise — always dedup before declaring gaps.

- Library: `wayback_archiver.match.match_products()`
- Output: Updated metadata with `matched_sku` fields

### Phase 3: ASSET DOWNLOAD

**Test live CDN first** — Shopify CDNs persist years after store closure.
Direct download is faster, higher quality, and not rate-limited.

```bash
# Test liveness
curl -sI -o /dev/null -w '%{http_code}' "{cdn_url}"

# Download via app.sh for best quality (probes PNG > JPG > WEBP)
PROBE_DELAY=1 ~/Downloads/cdn/app.sh -o output_dir urls.txt
```

**CDN liveness is a ticking clock.** UUID-name files survive longer than
simple-name files. Download aggressively while CDNs are live.

For dead CDNs, fall back to Wayback:
```bash
# Find best capture (largest file)
curl -s "https://web.archive.org/cdx/search/cdx?\
url={cdn_url}&output=json&fl=timestamp,statuscode,length&limit=10"

# Download with im_ modifier (raw bytes)
curl -s -L -o output.jpg "https://web.archive.org/web/{ts}im_/{cdn_url}"
```

**Pre-download**: Filter non-product images and deduplicate size variants.
**Post-download**: Verify magic bytes (JPEG: `ff d8`, PNG: `89 50`).
Reject files <1KB (error pages masquerading as images).

- Library: `wayback_archiver.download.*`
- Cascade: live CDN → Wayback CDX best size → exhaustive snapshot
- Output: `products/{dirname}/` with images

### Phase 4: NORMALIZE + BUILD

Classify images, generate per-product metadata, compile final catalog JSON,
CSV exports, and stats report.

- Library: `wayback_archiver.normalize.*`
- Output: `catalog/catalog.json`, `catalog/products.csv`, `catalog/images.csv`

### GAP RECOVERY (when needed)

After all phases, compute what's still missing. Cross-reference against CDX.
For pages that automation can't reach (anti-bot, CAPTCHA), fall back to
**HAR-based recovery**: give the user Wayback URLs to browse manually, then
parse their HAR files for CDN URLs.

- Read `agents/har_processor.md` for the HAR recovery workflow

---

## How to Run

### New site setup
1. Create a YAML config in `configs/` (see `references/site-config-schema.md`)
2. Ensure CDX dump file exists at the configured path
3. Set proxy credentials: `PROXY_USERNAME` and `PROXY_PASSWORD` env vars (or use defaults in `fetch_archive.py`)
4. Verify scripts: `filter_cdx.py` and `fetch_archive.py` available in workspace
5. Install Playwright: `playwright install chromium` (last-resort fallback only)
6. Run stages in order with dry-run first

### Orchestration flow
```
Phase 0: VALIDATE
  Check CDX file exists, tools installed, paths valid.
  Verify filter_cdx.py and fetch_archive.py are available.
  Confirm proxy credentials (ISP or DC) are set.

Phase 1: DISCOVERY (run 4 vectors in parallel)
  1A: Wayback CDX handles (paginated, all subdomains)
  1B: CommonCrawl index handles (all relevant crawls)
  1C: Collection/category page handles
  1D: CDN image filename reverse-discovery
  -> DEDUP against existing catalog -> new_handles.json

═══ CDX FILTERING GATE ═══
  python filter_cdx.py raw_cdx.txt > links.txt
  Expect ~90-95% reduction. Verify zero product URLs lost.
  Output sorted: structured (oEmbed/Atom/JSON) first, then HTML.
  Review stderr stats before proceeding.

═══ VERIFICATION GATE ═══
No queries hit result cap. All subdomains/crawls attempted.
Handles deduplicated and classified. Show counts to user.

Phase 2: EXTRACTION (use fetch_archive.py)
  python fetch_archive.py links.txt --resume
  The script handles the full cascade automatically:
    All URLs: Direct Wayback id_ first (no proxy cost)
    ↓ fallback on rate-limit or failure:
    Structured: proxy fallback
    HTML/Collections: CommonCrawl WARC → proxy fallback
  Content validated post-fetch: anti-bot detection, wrapper rejection.
  For SPA-era sites, also fetch API JSON endpoints directly (2D).
  -> MATCH + DEDUP cross-source entries

═══ USER CONFIRMATION GATE ═══
Show metadata coverage + image URL counts. Ask before downloading.

Phase 3: ASSET DOWNLOAD
  Test CDN liveness -> download live URLs via app.sh
  Dead CDNs -> Wayback im_ fallback
  POST-VALIDATE: check magic bytes on all downloads

Phase 4: NORMALIZE + BUILD
  Rename images, compile catalog JSON + CSVs + report

GAP RECOVERY (if needed):
  Re-run fetch_archive.py with --proxy dc (datacenter fallback)
  Playwright + network interception for CDN URL discovery
  HAR-based recovery for anti-bot blocked pages
```

### Running a single stage
```python
# Example: run the INDEX stage
from wayback_archiver.site_config import load_config
from wayback_archiver.cdx import parse_cdx, find_content_pages

config = load_config(Path("configs/yeezysupply.yaml"))
products = parse_cdx(
    config.cdx_paths[0],
    config.url_rules,
    config.era_rules,
    config.compiled_junk,
    config.type_priority,
)
content_pages = find_content_pages(config.cdx_paths[0], config.domains)
```

---

## Tool Selection Quick Reference

| Task | Use This | Not This | Why |
|------|----------|----------|-----|
| **Full page fetch** | **`fetch_archive.py`** | Manual curl/Playwright | Handles entire cascade: CC WARC → proxy id_ → validation |
| HTML page content | **CommonCrawl WARC** (via fetch_archive.py) | Wayback HTML | WARC has raw HTML; Wayback has JS wrapper + anti-bot |
| HTML (no CC capture) | **Wayback `id_` via proxy** | curl without proxy | Proxy rotates IPs to avoid 60 req/min rate limit |
| HTML (last resort) | **Playwright + network intercept** | curl | For CDN URL discovery when content isn't needed |
| JSON API endpoints | **Wayback `id_` via proxy** | Playwright | JSON doesn't need JS rendering |
| Atom/oEmbed feeds | **Wayback `id_` via proxy** | Playwright | Structured data, no JS needed |
| CDN image downloads | **curl (live CDN)** | Wayback | Live CDN = higher quality, no rate limits |
| Dead CDN images | **Wayback `im_`** | Playwright | `im_` returns raw bytes without toolbar |
| Handle discovery | **CDX API + CC index** | Manual browsing | Automated, exhaustive, paginated |
| **CDX dump filtering** | **`filter_cdx.py`** | Manual grep | 6-layer filter: status, MIME, junk paths, dedup (94%+ reduction) |
| CDX dump analysis | **grep / Python** | CDX API | Local dump is instant; API is rate-limited |
| HAR file processing | **Python json** | Manual inspection | Programmatic extraction is 100x faster |

---

## Principles

1. **Two-source discovery**: Always query BOTH Wayback AND CommonCrawl — they have independent coverage
2. **Discovery before extraction**: Complete Phase 1 exhaustively before starting Phase 2
3. **CommonCrawl WARCs for HTML**: Raw HTTP responses beat Wayback's JS replay (76% vs 2.4% success)
4. **JSON APIs first**: `products.json` is the holy grail — complete catalog in one request
5. **Triage by era**: Classify handles by platform before choosing extraction method
6. **Test live CDN before Wayback**: Shopify CDNs persist years after store closure
7. **Paginate past caps**: CDX defaults to 500 results — always check and paginate
8. **Batch CDX queries**: Query `products/*` once, not per-handle (avoids rate limiting)
9. **Dedup before gap-chasing**: ~40% of "missing" products are noise — audit first
10. **Checkpoint/resume**: Every phase saves progress and is resumable
11. **Quality maximization**: Probe for best format (TIF > PNG > JPG > WEBP) via app.sh
12. **Validate after download**: Check magic bytes to catch error pages masquerading as images
13. **The 90% rule**: If a method fails >90% in its first batch, the technique is wrong — pivot
14. **No outside sources**: Maintain original CDN provenance — don't mix in reseller images
15. **HAR fallback**: When automation fails, human browsing + automated extraction wins

---

## Wayback URL Modifiers

| Modifier | Use | Example |
|----------|-----|---------|
| *(none)* | Full replay with toolbar + JS | `web.archive.org/web/{ts}/url` |
| `id_` | Raw content, no wrapper | `web.archive.org/web/{ts}id_/url` — for JSON, XML, Atom |
| `im_` | Raw image bytes | `web.archive.org/web/{ts}im_/url` — for image download |

```
GOOD: https://web.archive.org/web/20240101id_/https://example.com/api/data.json
BAD:  https://web.archive.org/web/20240101/https://example.com/api/data.json
```
Without `id_`, Wayback injects toolbar HTML, corrupting JSON and binary files.

For HTML pages, `id_` alone is NOT enough — Wayback still serves a JavaScript
replay wrapper. Prefer CommonCrawl WARCs for HTML content. If CommonCrawl
doesn't have the page, use Playwright with network interception as a fallback.

## CommonCrawl WARC Fetching

CommonCrawl stores raw HTTP responses in WARC format on S3. Fetch individual
records using HTTP Range headers:

```bash
curl -s -H "Range: bytes={offset}-{offset+length-1}" \
  "https://data.commoncrawl.org/{warc_filename}" | gunzip
```

The response contains HTTP headers + body. Parse the body as HTML. If `gunzip`
fails or the output lacks `<html`, the record is corrupt — skip it.

**Rate limit**: 1 request/second to `data.commoncrawl.org`.

---

## Multi-Platform Support

Sites migrate between commerce platforms over time. The pipeline detects and
handles:

| Platform | CDN | Detection | Key Endpoints |
|----------|-----|-----------|---------------|
| **Shopify** | `cdn.shopify.com` | `ShopifyAnalytics`, `/products/*.json` | `products.json?limit=1000`, `.atom`, `.oembed` |
| **Swell Commerce** | `cdn.swell.store/{store-id}/` | `__data.json`, SvelteKit | `__data.json` (requires `devalue` npm lib) |
| **Fourthwall** | `imgproxy.fourthwall.com` | checkout subdomains | Fourthwall API |
| **Adidas** | `assets.yeezysupply.com` | `/api/products/` | `/api/products/{SKU}`, `/products/bloom` |

### Shopify-Specific Notes
- `products.json?limit=1000` returns the complete catalog in one request
- CDN persists years after store closure (confirmed live in 2026 for stores closed in 2019)
- Multiple store IDs per domain — migration creates separate CDN namespaces
- `.atom` and `.oembed` endpoints exist for every collection and product
- UUID-hashed filenames on CDN prevent enumeration — discovery requires rendering pages

### Swell Commerce Notes
- SvelteKit-based: `__data.json` endpoints contain structured product data
- Requires `devalue` npm library to parse (manual flat-array parsing will break)
- CDN: `cdn.swell.store/{store-id}/...`

---

## Reference docs
- `references/data-contracts.md` — JSON schemas for all stage inputs/outputs
- `references/site-config-schema.md` — How to write a YAML config for a new site
- `references/lessons-learned.md` — Anti-patterns and best practices from completed projects

## Proven configs
- `configs/yeezysupply.yaml` — Yeezy Supply (1,229 products, multi-era, 3 Shopify store IDs + Adidas SPA)
- `configs/yeezygap.yaml` — Yeezy Gap (119 products, single Shopify store)
- `configs/yeezy.yaml` — YEEZY (300 products, multi-subdomain, multi-platform)

## Common Failure Modes

| Symptom | Cause | Fix |
|---------|-------|-----|
| curl returns 3KB HTML | Wayback JS replay wrapper | CommonCrawl WARC or Playwright |
| Playwright times out on `networkidle` | Dead CDN resources | Use `domcontentloaded` + 8s wait |
| Playwright renders Wayback toolbar | Replay framework as top-level | Network interception for CDN URLs |
| Most pages return "Access Denied" | Anti-bot captured instead of content | CommonCrawl or structured endpoints |
| CDX returns empty for sequential queries | Rate limiting (empty body, not 429) | Batch queries + exponential backoff |
| SPA pages render empty | Data loaded via API, not in HTML | Fetch API JSON directly |
| >90% of handles fail extraction | Wrong method for this era | Triage by era, switch techniques |
| Image download gets HTML | Wayback error as HTTP 200 | Verify magic bytes |
