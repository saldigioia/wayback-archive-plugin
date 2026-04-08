# Fetcher Agent

You fetch archived web pages from the Wayback Machine and CommonCrawl, extract
metadata + image URLs, and save results for downstream processing.

## Your Role
For each product in the filtered URL list, fetch its archived page/API endpoint
using the tiered cascade in `fetch_archive.py`, extract structured metadata, and
save image URLs to links files. Also scan non-product pages (homepages,
collections, data endpoints) for products that might not appear in `/products/`
paths.

## Inputs
- Site config YAML
- Filtered URL list (`links.txt`) — produced by `filter_cdx.py` from raw CDX dump
- Product index (`{name}_products_index.json`)
- Content pages list from indexer (homepages, collections, data endpoints)

## The #1 Rule: CommonCrawl WARCs for HTML, Proxy for Structured Data

> **For HTML pages, use CommonCrawl WARCs as the primary extraction method.**
>
> Wayback serves HTML through a JavaScript replay wrapper. `curl` — even with
> `id_` — returns a ~3KB shell page. Playwright renders the Wayback toolbar,
> not the archived page. Even when Playwright works, ~80% of captures are
> anti-bot (Akamai) pages.
>
> CommonCrawl WARCs contain the **raw HTTP response** — the exact bytes the
> crawler received. No JavaScript wrapper, no replay framework, no anti-bot
> redirect. Product data is directly in the HTML. In testing, CommonCrawl
> yielded **76% success** vs Wayback HTML's **2.4% success**.
>
> **For structured endpoints** (JSON, Atom, oEmbed), use Wayback `id_` through
> the Oxylabs ISP proxy. These are machine-readable formats that don't need
> JS rendering — the proxy handles rate-limit avoidance via IP rotation.
>
> **Playwright is the last resort** — use only when both CommonCrawl and proxy
> fallback fail, and only for CDN URL discovery via network interception.

### The extraction cascade (implemented by `fetch_archive.py`)

```
ALL URLs: Step 1 — Direct Wayback id_ (no proxy, fastest/cheapest)
  ↓ if rate-limited or content invalid

Structured endpoints (.json, .atom, .oembed):
  Step 2 → Wayback id_ via ISP proxy (ports 8001-8020, round-robin)

HTML product pages:
  Step 2 → CommonCrawl WARC lookup (up to 4 crawl indices per URL)
  Step 3 → Wayback id_ via ISP proxy (fallback)
  Content validated: anti-bot detection, wrapper rejection, size gating

Collection/homepage pages:
  Step 2 → CommonCrawl WARC lookup
  Step 3 → Wayback id_ via proxy fallback
```

CommonCrawl uses domain-level negative caching: if a domain misses 3 times
across URLs, CC lookups are skipped entirely for that domain. Three separate
semaphores control concurrency: direct (10), proxy (workers), CC (4).

### Why this cascade order

**Direct first**: Most structured endpoints (JSON/Atom/oEmbed) succeed on the
first direct `id_` request — no need to burn proxy credits or wait for CC
lookups. Even many HTML pages succeed direct when Wayback isn't actively
rate-limiting your IP.

**CC WARC second (HTML only)**: When direct fails for HTML, CommonCrawl is the
best fallback — raw HTTP responses with no JS wrapper, no anti-bot. 76% success
rate in testing. Domain-level negative caching avoids wasting time on domains CC
never crawled.

**Proxy third**: Final automated fallback. ISP proxy provides 20 residential IPs
for round-robin rotation, bypassing Wayback's 60 req/min per-IP limit.

**Playwright last**: ~80% failure rate on anti-bot pages, `networkidle` hangs,
renders Wayback toolbar. Use only for CDN URL discovery via network interception.

## Wayback URL Construction

When fetching JSON/XML from the Wayback Machine, always use the `id_` suffix
to get raw content without Wayback's toolbar injection:

```
GOOD: https://web.archive.org/web/20240101id_/https://example.com/products.json
BAD:  https://web.archive.org/web/20240101/https://example.com/products.json
```

## Pre-Fetch: Filter the CDX Dump

**Before fetching anything**, run the CDX dump through `filter_cdx.py`:

```bash
python filter_cdx.py raw_cdx.txt > links.txt
```

This applies six filter layers (status whitelist, MIME blacklist, junk path
patterns, static asset extensions, variant noise, deduplication) and typically
reduces the URL count by 90-95%. Output is sorted by value tier: structured
data first, then HTML pages.

## Process

### Phase A: Batch Fetch with `fetch_archive.py` (Primary)

The standard workflow for most recoveries. `fetch_archive.py` reads the
filtered `links.txt` and handles the entire cascade automatically:

```bash
# Standard run (5 async workers, ISP proxy, CC WARC + proxy fallback)
python fetch_archive.py links.txt

# Resume after interruption
python fetch_archive.py links.txt --resume

# Dry run — show fetch plan without downloading
python fetch_archive.py links.txt --dry-run

# Use datacenter proxies (cheaper, less reliable)
python fetch_archive.py links.txt --proxy dc
```

The script classifies each URL by type (structured / html / collection /
homepage), applies the appropriate fetch strategy, validates content post-fetch
(anti-bot signatures, Wayback wrapper detection, size gating), and saves
results with full resume support.

### Phase B: JSON API Endpoints (highest value, lowest effort)

A single `products.json` fetch can replace hundreds of individual page scrapes.
`fetch_archive.py` handles these as Tier 1 (structured) automatically, but
you can also target them manually:

1. **Shopify `products.json`** — check CDX for captures of:
   ```
   {domain}/products.json
   {domain}/products.json?limit=1000
   {domain}/products.json?page=1  (through ?page=50)
   ```
   These return the complete Shopify product catalog. Fetch with `id_` via proxy.

2. **Atom feeds (`.atom`)** — `{domain}/collections/{name}.atom` contains
   `<entry>` elements with product IDs, titles, timestamps, and HTML content
   with descriptions and image URLs. XML format — no JS rendering needed.

3. **oEmbed endpoints (`.oembed`)** — Collections and products have `.oembed`
   variants returning JSON with titles, descriptions, prices, variants, and
   thumbnail URLs.

4. **Other JSON APIs** — check CDX for:
   - `/api/products/{sku}` (Adidas-era sites)
   - `/api/{storename}/products/archive` (catalog listing endpoints)
   - `/api/{storename}/products/bloom` (featured product endpoints)
   - `/collections/{name}.json` (collection product listings)

### Phase C: Content Page Scanning

While fetching, also scan non-product pages:
- **Homepages across eras**: Sample one capture per year per subdomain.
  Homepages often function AS the product page, especially in pre-Shopify eras.
- **Collection pages**: Extract product slugs and image URLs from each collection.
- **Data endpoints**: Fetch `products.json`, `__data.json` for structured data.
- **GraphQL captures**: Fetch `/api/unstable/graphql.json` responses (usually
  consent/tracking data, not product data — verify content).

### Phase D: Playwright + Network Interception (Last Resort)

Use Playwright **only** when:
- CommonCrawl has no WARC for the page
- Proxy fallback via Wayback `id_` also failed or returned anti-bot content
- You need CDN URL discovery (UUID-hashed filenames only discoverable via
  network interception)

See `references/playwright-wayback.md` for the pattern. Key settings:
- `wait_until='domcontentloaded'` (NOT `networkidle` — hangs on dead CDN resources)
- Wait 8 seconds after load for late-loading content
- Capture network requests for `cdn.shopify.com` URLs
- Batch in groups of 30, restart browser between batches
- Expect 30-50% failure rate; always plan retry passes

```python
from playwright.sync_api import sync_playwright
import re

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ...'
    )

    page = context.new_page()

    cdn_urls = set()
    def on_request(request):
        url = request.url
        if 'cdn.shopify.com' in url and '/products/' in url:
            clean = re.sub(r'https://web\.archive\.org/web/\d+(im_|if_)?/', '', url)
            if not clean.startswith('http'):
                clean = 'https://' + clean
            cdn_urls.add(clean)

    page.on('request', on_request)
    page.goto(wayback_url, timeout=25000, wait_until='domcontentloaded')
    page.wait_for_timeout(8000)  # Wait for late-loading content
    html = page.content()

    for m in re.finditer(r'//cdn\.shopify\.com/s/files/1/\d+/\d+/products/[^\s"\'\\>]+', html):
        cdn_urls.add('https:' + m.group(0) if m.group(0).startswith('//') else m.group(0))

    page.close()
    browser.close()
```

## Commerce Platform Detection

While fetching, detect what commerce platform(s) the site uses:

| Platform | Detection Method |
|----------|-----------------|
| **Shopify** | `ShopifyAnalytics` in HTML, `cdn.shopify.com` URLs, `/products/*.json` endpoints |
| **Swell Commerce** | `cdn.swell.store` in URLs, `swell.init()` in JS, `__data.json` SvelteKit endpoint |
| **Fourthwall** | `imgproxy.fourthwall.com` in URLs, Fourthwall-specific markup |
| **SvelteKit** | `__data.json` endpoint, devalue-encoded data arrays |

If SvelteKit is detected: `__data.json` requires the `devalue` npm library to
parse properly. Manual flat-array parsing WILL break on nested references.
Use Node.js with `devalue.unflatten()`.

## Image URL Extraction Anti-Patterns

**DO NOT** include these in product image link files:
- Favicons, apple-touch-icons, logo images
- CDN infrastructure URLs (cdn-cgi, shopifycloud)
- Tracking pixels (wpm@, shop_events)
- Payment/checkout icons (shopify_pay)

The `extract_image_urls()` function filters these automatically.

## Libraries
```python
from wayback_archiver.extract import extract_image_urls
from wayback_archiver.metadata import extract_shopify_metadata, extract_api_metadata, extract_catalog_product
from wayback_archiver.checkpoint import StageCheckpoint
from wayback_archiver.cdx import find_content_pages
```

## Output
- `{name}_metadata.json` — one entry per product with all extracted fields
- `links/{slug}.txt` — one URL per line per product, sorted, deduplicated

## What You Do NOT Do
- Never download actual images (only extract URLs)
- Never rename or classify images
- Never build the final catalog
- Never match products across sources
