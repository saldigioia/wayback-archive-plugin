# Site Configuration Schema

To add a new target site, create a YAML file in `configs/`. No Python code needed.

## Required Fields

```yaml
name: sitename              # Used for output file naming (no spaces)
display_name: "Site Name"   # Human-readable name
credit_line: "Site Name"    # Credit in metadata.txt
project_dir: /path/to/project  # Working directory for this site
transport_pkg: /path/to/wayback_cdx_v2  # Wayback transport layer
cdx_files:                  # CDX dump files to process
  - /path/to/cdx_dump.txt
domains:                    # All domains to scan (including subdomains)
  - example.com
  - www.example.com
  - shop.example.com
```

## URL Classification Rules

Define how to extract product slugs from CDX URLs:

```yaml
url_rules:
  - path_prefix: "/products/"     # Shopify product pages
    url_type: slug
    require_status: "200"
    require_ctype: "text/html"

  - path_prefix: "/api/products/" # API endpoints
    url_type: api
    require_status: "200"
    require_ctype:
      - "application/json"
      - "text/html"

  - path_contains: "/collections/*/products/"  # Collection-embedded
    url_type: collection
    require_status: "200"
    require_ctype: "text/html"

  - path_suffix: ".atom"            # Atom feeds (XML, fetchable with curl)
    url_type: atom
    require_status: "200"
    require_ctype:
      - "application/atom+xml"
      - "text/xml"
      - "application/xml"

  - path_suffix: ".oembed"          # oEmbed endpoints (JSON, fetchable with curl)
    url_type: oembed
    require_status: "200"
    require_ctype:
      - "application/json+oembed"
      - "application/json"

  - path_prefix: "/products.json"   # Shopify catalog API (the holy grail)
    url_type: api
    require_status: "200"
    require_ctype: "application/json"
```

## Era Detection Rules

Classify products by platform era (evaluated in order, first match wins):

```yaml
era_rules:
  - condition: "url_type == 'api'"
    era: adidas_api
  - condition: "timestamp_year <= 2017"
    era: early_shopify
  - condition: "default"
    era: late_shopify
```

## CDN Patterns (for image extraction)

Define patterns for each CDN the site uses. The pipeline now supports multiple
commerce platforms — add patterns for each platform detected:

```yaml
cdn_patterns:
  # Shopify CDN (standard)
  - name: shopify
    regex: 'https?://cdn\.shopify\.com/s/files/[^\s"''\\]+/products/[^\s"''\\]+'
    size_strip: '_(?:\d+|\{width\})x(?:@\dx)?(?=\.\w+)'
    named_size_strip: '_(?:grande|medium|small|large|compact|master|pico|icon|thumb)(?=\.\w+)'

  # Shopify domain-hosted CDN (e.g., yeezy.com/cdn/shop/files/)
  - name: shopify_files
    regex: 'https?://(?:example\.com)/cdn/shop/(?:files|products)/[^\s"''\\]+\.(?:png|jpg|jpeg|webp|gif|avif)'
    size_strip: '_(?:\d+|\{width\})x(?:@\dx)?(?=\.\w+)'
    named_size_strip: '_(?:grande|medium|small|large|compact|master|pico|icon|thumb)(?=\.\w+)'

  # Swell Commerce CDN
  - name: swell
    regex: 'https?://cdn\.swell\.store/[^\s"''\\]+\.(?:png|jpg|jpeg|webp|gif|avif)'

  # Fourthwall CDN
  - name: fourthwall
    regex: 'https?://imgproxy\.fourthwall\.com/[^\s"''\\]+'
```

## Download Cascade

```yaml
download_cascade:
  - live_cdn         # app.sh for live Shopify CDN
  - direct_fetch     # Direct HTTP
  - wayback_cdx_best # CDX API for largest variant
  - exhaustive       # Try every snapshot
  - asset_rescue     # CDX asset domain rescue
```

## Optional Fields

```yaml
cdn_tool: /path/to/app.sh  # CDN quality probe tool
junk_patterns:              # Regex patterns to filter garbage URLs
  - '%22|%3[CcEe]'
type_priority:              # Dedup priority (first = preferred)
  - api
  - slug
  - collection
  - sku
catalog_api_patterns:       # For bloom/archive catalog APIs
  - "/api/yeezysupply/products/bloom"
  - "/api/yeezysupply/products/archive"
  - "/products.json"
min_image_bytes: 500        # Minimum valid image size

# Commerce platform hints (auto-detected, but can be specified)
platforms:
  - shopify
  - swell
  - fourthwall
```

## Alternative Archives

Configure fallback archive sources for URLs that fail the primary
Wayback + CommonCrawl cascade. These are tried after the main fetch
cascade is exhausted (Step 4, after proxy, before giving up).

```yaml
alternative_archives:
  enabled: true
  sources:
    - archive_today    # archive.ph / archive.today — on-demand captures
    - memento          # timetravel.mementoweb.org — cross-archive aggregator
```

Can also be enabled per-run via CLI:
```bash
python3 run_stage.py fetch --config configs/site.yaml --fallback-archives archive_today memento
```

When enabled, after the primary cascade completes, the pipeline:
1. Identifies URLs that failed (no output file or file < 500 bytes)
2. Queries archive.today's timemap for available captures
3. Queries Memento Time Travel for cross-archive captures
4. Fetches from the most recent snapshot found
5. Records successes as `alt_archive` method in fetch_stats.json

## Junk Patterns

The junk pattern filter rejects URLs matching these patterns. Common junk:

```yaml
junk_patterns:
  # URL-encoded characters in paths (malformed URLs)
  - '%22|%3[CcEe]|%7[Bb]|%5[Bb]|%0A'
  # Template/placeholder strings
  - 'undefined|:productId|\[insert'
  # Shopify web pixels and tracking
  - 'wpm@|sandbox/modern'
  # Image optimization query params leaked into paths
  - '=center|=fast|=pad|=100|=80|=16|=375|=560'
  # JavaScript files in product paths
  - '\.js$'
```

## Example: Multi-Platform Site

```yaml
name: yeezy
display_name: "YEEZY"
credit_line: "YEEZY"

domains:
  - yeezy.com
  - www.yeezy.com
  - vultures.yeezy.com
  - bully.yeezy.com
  - sply.yeezy.com

cdx_files:
  - /path/to/yeezy_com_wayback.txt

project_dir: /path/to/yeezy
cdn_tool: /path/to/cdn/app.sh

url_rules:
  - path_prefix: "/products/"
    url_type: slug
    require_status: "200"
    require_ctype:
      - "text/html"
      - "application/json"

era_rules:
  - condition: "timestamp_year <= 2022"
    era: pre_shopify
  - condition: "default"
    era: shopify

cdn_patterns:
  - name: shopify_files
    regex: 'https?://(?:yeezy\.com|vultures\.yeezy\.com|bully\.yeezy\.com|sply\.yeezy\.com)/cdn/shop/(?:files|products)/[^\s"''\\]+\.(?:png|jpg|jpeg|webp|gif|avif)'
  - name: swell
    regex: 'https?://cdn\.swell\.store/[^\s"''\\]+\.(?:png|jpg|jpeg|webp|gif|avif)'
  - name: fourthwall
    regex: 'https?://imgproxy\.fourthwall\.com/[^\s"''\\]+'

catalog_api_patterns:
  - "/products.json"

download_cascade:
  - live_cdn
  - direct_fetch
  - wayback_cdx_best
  - exhaustive
```
