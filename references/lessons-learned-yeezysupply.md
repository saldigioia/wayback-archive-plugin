# Addendum: Yeezy Supply Recovery Lessons (April 2026)

See `/Users/salvatore/Downloads/yeezy_supply/LESSONS_LEARNED.md` for the full document.

These lessons are now fully integrated into the main pipeline. Key additions:

1. **Stage 2 (FETCH) uses Playwright, not curl, for HTML pages.** curl with `id_` returns Wayback's JS wrapper (~3KB), not the rendered page. Playwright gets the full content with CDN URLs resolved. See `agents/fetcher.md` for the Playwright pattern with network request capture.

2. **Stage 2 (FETCH) prioritizes JSON APIs.** `products.json?limit=1000` returns the complete Shopify catalog in one request. Atom feeds (`.atom`) and oEmbed endpoints (`.oembed`) provide structured data without browser rendering. Always check for these before scraping individual pages.

3. **Stage 4 (DOWNLOAD) tests live CDN first.** Shopify CDNs persist years after store closure (confirmed 2026 for stores closed in 2019). Direct CDN download is faster, more reliable, and not rate-limited.

4. **Multiple Shopify store IDs per domain.** yeezysupply.com used stores 0904/6694, 1324/7915, and 1765/5971. Extract store ID from each image URL rather than hardcoding.

5. **Stage 5 (GAP ANALYSIS) deduplicates before gap-chasing.** ~40% of "missing" products in the initial inventory were duplicates, colorways, drafts, or aliases. Audit first, then chase gaps.

6. **CDX cross-reference at every phase boundary.** Before fetching anything, check which target URLs exist in the CDX. This prevents wasted requests on URLs that were never archived.

7. **HAR fallback for rate-limited pages.** When Playwright fails due to rate limiting or CAPTCHAs, human browsing + automated HAR extraction recovers what automation can't. See `agents/har_processor.md`.

8. **UUID filenames cannot be guessed.** Many Shopify CDN filenames contain cryptographic UUIDs. The only discovery method is rendering the page that references them (Playwright or HAR capture).

9. **Batching and rate limits.** Batch Playwright scrapes in groups of 30 with 2-second delays. Expect 30-50% first-pass failure rate. Always plan retry passes.

10. **Recovery scale.** These techniques took the yeezysupply.com recovery from 119 manually-scraped products to 558 products with 3,463 images (914 MB) across a 9-year, multi-platform store lifecycle.
