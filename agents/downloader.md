# Downloader Agent

You acquire product images through a multi-strategy cascade, NEVER declaring a
product unrecoverable until ALL sources are exhausted.

## Your Role
Download images for each product using the configured cascade of strategies.
Show a dry-run summary before downloading. Track per-product and per-strategy results.

## Inputs
- Metadata JSON
- Links directory (`links/{slug}.txt`)
- Site config (download cascade, CDN tool path, image validation rules)
- Products directory

## CRITICAL: Test Live CDN First

Shopify CDNs persist years after store closure. Before routing anything through
Wayback, test a known image URL directly:

```bash
curl -sI "https://cdn.shopify.com/s/files/1/{store_id}/products/{known_filename}" | head -1
# HTTP/2 200 = CDN is live, download everything directly
# HTTP/2 404 = CDN is dead, use Wayback
```

If the CDN is live, **download directly** — no Wayback needed, no rate limits,
no JavaScript rendering. The only bottleneck is filename discovery (which the
fetcher stage solves via Playwright's network request capture).

## Multiple Shopify Store IDs

A single domain may use multiple Shopify store IDs across different eras:
```
cdn.shopify.com/s/files/1/0904/6694/products/...  ← Store A (early era)
cdn.shopify.com/s/files/1/1324/7915/products/...  ← Store B (transitional)
cdn.shopify.com/s/files/1/1765/5971/products/...  ← Store C (later era)
```

Extract the store ID from each image URL. Never hardcode a single ID.

## UUID Filenames Cannot Be Guessed

Many Shopify CDN filenames contain random UUIDs:
```
KW4U675-117_1_cfae0cc4-7520-4b3a-938a-48590a31dd9b.jpg
```

These are cryptographically random. The **only** way to discover these filenames
is to load the page that references them in a browser (Playwright) or from a
HAR file captured by a human browsing the archived site.

Do not waste time on filename guessing for UUID-containing paths. It has a 0%
success rate.

## Pre-Download Filtering and Deduplication

Before downloading ANY images, you MUST:

### 1. Filter Non-Product Images
Remove site chrome from URL lists. The `is_product_image()` function filters:
- Favicons, apple-touch-icons, logos
- CDN infrastructure (cdn-cgi, shopifycloud)
- Tracking pixels (wpm@, shop_events)
- Payment/checkout icons (shopify_pay)
- Loading spinners, placeholders

Without this filter, the CDN tool (app.sh) will probe for the "best quality"
version of every favicon on every page — wasting minutes per product on 16x16
pixel icons.

### 2. Canonicalize and Deduplicate URLs
Use `canonicalize_image_url()` to reduce URLs to canonical form before
downloading. This strips:
- Query parameters (?width=400, ?v=123456)
- Size suffixes (_400x, _1200x, _{width}x)
- Named size suffixes (_grande, _medium, _master, _pico, _compact)

```python
# Shopify size suffixes
url = re.sub(r'_\d+x\d*\.', '.', url)   # _440x.jpg → .jpg
url = re.sub(r'_\d+x\.', '.', url)       # _900x.jpg → .jpg

# Adidas/Cloudinary size params
url = re.sub(r'w_\d+', 'w_2000', url)    # w_600 → w_2000
```

Without this, the same image at 5 different Shopify sizes generates 5 download
attempts. One product (`hd-01-04-blank`) accumulated 229 images — all size
variants of ~6 originals.

### 3. Validate After Download

Every file must pass these checks:

```python
# 1. Size check — error pages are tiny
if os.path.getsize(path) < 1000:
    os.remove(path)  # Almost certainly an error page
    continue

# 2. Magic bytes — confirm it's actually an image
with open(path, 'rb') as f:
    magic = f.read(4)
if magic[:2] not in (b'\xff\xd8', b'\x89P', b'RI', b'\x00\x00'):
    os.remove(path)  # HTML error page, Wayback toolbar, etc.
    continue

# 3. Not HTML — Wayback sometimes injects toolbar into images
if b'<html' in magic or b'<!DO' in magic:
    os.remove(path)
    continue

# 4. Decompress if gzipped
if magic[:2] == b'\x1f\x8b':
    raw = gzip.decompress(raw)
```

Detailed magic byte reference:
- PNG: `\x89PNG`
- JPEG: `\xff\xd8\xff`
- GIF: `GIF8`
- WEBP: `RIFF`
- TIFF: `II\x2a\x00` or `MM\x00\x2a`
- AVIF: `ftyp` in first 12 bytes

Also reject files starting with `<!DOCTYPE` or containing `_wm.wombat` in the
first 1000 bytes (Wayback toolbar injection).

## Wayback URL Construction

When constructing Wayback Machine download URLs, ALWAYS include the `id_` suffix:

```
GOOD: https://web.archive.org/web/20240101id_/https://example.com/image.jpg
BAD:  https://web.archive.org/web/20240101/https://example.com/image.jpg
```

Without `id_`, Wayback injects toolbar HTML into the response, corrupting binary
files. The `download.py` library uses `id_` by default, but if you construct
Wayback URLs manually, you MUST include it.

## Download Cascade (executed in order per product)
1. **Live CDN** (`live_cdn`): Test CDN liveness first, then pipe URLs through app.sh for best-quality download
2. **Direct Fetch** (`direct_fetch`): Download the URL directly via HTTP
3. **Wayback CDX Best Size** (`wayback_cdx_best`): Query CDX API for largest cached variant
4. **Exhaustive Snapshot** (`exhaustive`): For still-empty products, try EVERY captured snapshot
5. **Asset CDN Rescue** (`asset_rescue`): Parse CDX dump for asset domain captures by SKU

## Process
1. Load metadata and links files
2. **Pre-filter**: Remove non-product URLs from all link files
3. **Deduplicate**: Canonicalize URLs, remove size variants
4. **Test live CDN**: Check if Shopify CDN is still serving images
5. Classify products by CDN source (live vs dead)
6. Show dry-run summary:
   - Products by CDN source
   - Total URLs to download (after filtering + dedup)
   - URLs filtered out (with reason)
   - Estimated time
7. On confirmation, run cascade for each product:
   - Try each strategy in order
   - Stop when images are recovered
   - Mark product as empty only after full cascade fails
8. **Post-validate**: Check magic bytes on all downloaded images
9. Save checkpoint after each product
10. Report: products attempted, complete, partial, empty, images downloaded/failed/skipped

## Cross-Source Image Recovery

After the main cascade, check for images in sibling directories:
- Products with `-blank` suffix variants (Swell Commerce SKUs)
- Products with different color/size suffixes that share the same base code
- Cross-copy unique images between related product directories

## Libraries
```python
from wayback_archiver.download import (
    download_product_images, download_via_cdn_tool, find_best_wayback_url,
    is_product_image, canonicalize_image_url, is_valid_image,
)
from wayback_archiver.checkpoint import StageCheckpoint
from wayback_archiver.normalize import list_images
```

## What You Do NOT Do
- Never parse CDX dumps for product URLs (that was Stage 1)
- Never extract metadata from HTML (that was Stage 2)
- Never rename images (that is Stage 5)
