#!/usr/bin/env python3
"""
Pipeline stage runner — single CLI entry point for all stages.

Usage:
    python3 run_stage.py index   --config configs/yeezysupply.yaml [--dry-run]
    python3 run_stage.py filter  --config configs/yeezysupply.yaml [--dry-run]
    python3 run_stage.py fetch   --config configs/yeezysupply.yaml [--dry-run]
    python3 run_stage.py match   --config configs/yeezysupply.yaml [--dry-run]
    python3 run_stage.py download --config configs/yeezysupply.yaml [--dry-run]
    python3 run_stage.py normalize --config configs/yeezysupply.yaml [--dry-run]
    python3 run_stage.py build   --config configs/yeezysupply.yaml

Stage ordering: index → filter → fetch → match → download → normalize → build
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

# Add library to path
sys.path.insert(0, os.path.expanduser("~/lib"))

from wayback_archiver.site_config import load_config
from wayback_archiver.checkpoint import StageCheckpoint

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Repo root (for importing fetch_archive / filter_cdx) ──────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def run_index(config, dry_run=False, **_kw):
    """Stage 1: Parse CDX → product index."""
    from wayback_archiver.cdx import parse_cdx

    for cdx_path in config.cdx_paths:
        log.info("Parsing CDX: %s", cdx_path)
        products = parse_cdx(
            cdx_path,
            config.url_rules,
            config.era_rules,
            config.compiled_junk,
            config.type_priority,
        )

    # Stats
    by_type = {}
    by_era = {}
    for p in products.values():
        by_type[p["url_type"]] = by_type.get(p["url_type"], 0) + 1
        by_era[p["era"]] = by_era.get(p["era"], 0) + 1

    log.info("Total unique products: %d", len(products))
    log.info("By URL type:")
    for t, c in sorted(by_type.items(), key=lambda x: -x[1]):
        log.info("  %s: %d", t, c)
    log.info("By era:")
    for e, c in sorted(by_era.items(), key=lambda x: -x[1]):
        log.info("  %s: %d", e, c)

    if dry_run:
        log.info("[DRY RUN] Would write %d entries to %s", len(products), config.index_file)
        return

    config.index_file.write_text(json.dumps(products, indent=2, sort_keys=False))
    log.info("Written to %s", config.index_file)


def run_filter(config, dry_run=False, **_kw):
    """Stage 1.5: Filter CDX dumps → clean links.txt for fetching.

    Runs filter_cdx.py's logic on each CDX dump file from the config,
    producing a single filtered links.txt sorted by value tier.
    """
    import filter_cdx

    all_urls = []
    total_input = 0

    for cdx_path in config.cdx_paths:
        if not cdx_path.exists():
            log.warning("CDX file not found, skipping: %s", cdx_path)
            continue

        log.info("Filtering CDX: %s", cdx_path)
        lines = cdx_path.read_text().splitlines()
        total_input += len(lines)

        # Run filter_cdx logic inline — reuse its filter constants
        from collections import defaultdict
        candidates = defaultdict(list)

        for line in lines:
            parts = line.split("\t")
            if len(parts) < 5:
                parts = line.split()
            if len(parts) < 5:
                continue

            wayback_url, timestamp, original_url, status, mimetype = (
                parts[0], parts[1], parts[2], parts[3], parts[4]
            )

            if status not in filter_cdx.GOOD_STATUS:
                continue
            if mimetype.lower() in filter_cdx.BAD_MIMES:
                continue
            if filter_cdx.JUNK_PATH_RE.search(original_url):
                continue

            path_for_ext = original_url.split("?")[0]
            ext_match = filter_cdx.STATIC_EXT_RE.search(path_for_ext)
            if ext_match and ext_match.group(1).lower() not in ("json",):
                continue

            if filter_cdx.VARIANT_NOISE_RE.search(original_url):
                continue

            clean_original = filter_cdx.strip_query(original_url)
            canon = filter_cdx.canonical_path(clean_original)
            priority = filter_cdx.classify_url(canon)

            clean_wayback = f"https://web.archive.org/web/{timestamp}/{clean_original}"
            candidates[canon].append((clean_wayback, timestamp, clean_original, priority))

        for canon, entries in candidates.items():
            entries.sort(key=lambda e: (e[3], -int(e[1])))
            best = entries[0]
            all_urls.append((best[3], best[1], best[0]))

    # Sort: structured data first, then by timestamp within tier
    all_urls.sort(key=lambda x: (x[0], x[1]))

    log.info("Filter results: %d input lines → %d clean URLs (%.1f%% reduction)",
             total_input, len(all_urls),
             100 * (1 - len(all_urls) / total_input) if total_input else 0)

    if dry_run:
        log.info("[DRY RUN] Would write %d URLs to %s", len(all_urls), config.filtered_links_file)
        for _, _, url in all_urls[:10]:
            log.info("  %s", url)
        if len(all_urls) > 10:
            log.info("  ... and %d more", len(all_urls) - 10)
        return

    config.project_path.mkdir(parents=True, exist_ok=True)
    config.filtered_links_file.write_text(
        "\n".join(url for _, _, url in all_urls) + "\n"
    )
    log.info("Written to %s", config.filtered_links_file)


def run_fetch(config, dry_run=False, proxy_type="isp", workers=5, max_retries=3,
              backoff_factor=2.0, **_kw):
    """Stage 2: Fetch pages using fetch_archive.py's cascade, then extract metadata.

    Uses the three-step cascade from fetch_archive.py:
      1. Direct Wayback id_ (no proxy)
      2. CommonCrawl WARC (HTML only)
      3. ISP/DC proxy fallback

    After fetching, extracts metadata and image URLs from downloaded HTML files.
    """
    from wayback_archiver.extract import extract_image_urls
    from wayback_archiver.metadata import (
        extract_shopify_metadata, extract_api_metadata, extract_publish_date,
    )

    # Warn about deprecated transport_pkg
    if config.transport_pkg:
        log.warning(
            "Config key 'transport_pkg' is deprecated. "
            "run_fetch now uses fetch_archive.py's cascade (direct → CC WARC → proxy). "
            "The transport_pkg value '%s' is ignored.", config.transport_pkg
        )

    # Determine input links file
    links_file = config.filtered_links_file
    if not links_file.exists():
        log.error("Filtered links file not found: %s — run the 'filter' stage first", links_file)
        sys.exit(1)

    # ── Phase A: Fetch pages via fetch_archive.py cascade ──────────────
    import fetch_archive

    output_dir = config.fetch_output_dir

    if dry_run:
        urls = [l.strip() for l in links_file.read_text().splitlines() if l.strip()]
        log.info("[DRY RUN] Would fetch %d URLs from %s via %s proxy with %d workers",
                 len(urls), links_file, proxy_type, workers)
        log.info("[DRY RUN] Output dir: %s", output_dir)
        # Delegate to fetch_archive's dry-run for detailed plan
        asyncio.run(fetch_archive.run(
            links_file=links_file,
            output_dir=output_dir,
            proxy_type=proxy_type,
            workers=workers,
            resume=True,
            dry_run=True,
        ))
        return

    t0 = time.time()
    log.info("Fetching pages via fetch_archive.py cascade...")
    log.info("  Links: %s", links_file)
    log.info("  Output: %s", output_dir)
    log.info("  Proxy: %s | Workers: %d", proxy_type, workers)

    asyncio.run(fetch_archive.run(
        links_file=links_file,
        output_dir=output_dir,
        proxy_type=proxy_type,
        workers=workers,
        resume=True,
        dry_run=False,
    ))
    fetch_elapsed = time.time() - t0

    # ── Phase B: Extract metadata from downloaded HTML ─────────────────
    log.info("Extracting metadata from fetched pages...")

    ckpt = StageCheckpoint(config.checkpoint_path("fetch"), "fetch")
    ckpt.load()

    metadata = {}
    if config.metadata_file.exists():
        metadata = json.loads(config.metadata_file.read_text())

    config.links_dir.mkdir(exist_ok=True)

    # Load the product index for era/type info
    index = {}
    if config.index_file.exists():
        index = json.loads(config.index_file.read_text())

    # Process each downloaded HTML file
    html_files = sorted(output_dir.glob("*.html")) if output_dir.exists() else []
    processed = 0
    skipped = 0

    try:
        for i, html_path in enumerate(html_files, 1):
            # Derive a slug from the filename
            slug = _slug_from_html_filename(html_path.name)
            if not slug:
                continue

            if ckpt.is_done(slug):
                skipped += 1
                continue

            content = html_path.read_text(errors="replace")
            if not content or len(content) < 100:
                continue

            # Determine if this is JSON/API or HTML
            is_json = False
            if content.lstrip().startswith("{") or content.lstrip().startswith("["):
                is_json = True

            if is_json:
                try:
                    data = json.loads(content)
                except json.JSONDecodeError:
                    m = re.search(r'\{.*"id"\s*:\s*"[A-Z].*\}', content, re.DOTALL)
                    if m:
                        try:
                            data = json.loads(m.group(0))
                        except json.JSONDecodeError:
                            log.debug("  JSON parse failed for %s", slug)
                            continue
                    else:
                        continue

                meta = extract_api_metadata(data)
                image_urls = meta.pop("image_urls", [])
            else:
                image_urls = extract_image_urls(content)
                meta = extract_shopify_metadata(content, slug)

            # Write image links
            links_file_out = config.links_dir / f"{slug}.txt"
            links_file_out.write_text(
                "\n".join(image_urls) + "\n" if image_urls else ""
            )

            # Get era/type from index if available
            idx_entry = index.get(slug, {})
            date_str = extract_publish_date(image_urls)

            metadata[slug] = {
                "slug": slug,
                "era": idx_entry.get("era", "unknown"),
                "url_type": idx_entry.get("url_type", "json" if is_json else "slug"),
                "url": idx_entry.get("original_url", ""),
                "date": date_str,
                "image_count": len(image_urls),
                **{k: v for k, v in meta.items() if v},
            }

            ckpt.mark_done(slug)
            processed += 1

            if processed % 20 == 0:
                config.metadata_file.write_text(json.dumps(metadata, indent=2))
                log.info("  Extracted %d / %d ...", processed, len(html_files))

    except KeyboardInterrupt:
        log.warning("Interrupted — saving progress")
    finally:
        config.metadata_file.write_text(json.dumps(metadata, indent=2))

    total_elapsed = time.time() - t0
    log.info("DONE — fetch: %.1fs, extraction: %.1fs total",
             fetch_elapsed, total_elapsed - fetch_elapsed)
    log.info("  Metadata: %d entries (%d new, %d skipped)",
             len(metadata), processed, skipped)


def _slug_from_html_filename(filename: str) -> str | None:
    """Extract a product slug from a fetch_archive.py output filename.

    Filenames look like: www.yeezygap.com_products_dove-hoodie.html
    We need to extract: dove-hoodie
    """
    name = filename.removesuffix(".html")
    # Try to find /products/{slug} pattern
    m = re.search(r"_products_(.+?)(?:\.json|\.oembed|\.atom)?$", name)
    if m:
        slug = m.group(1)
        # Clean up further path components
        slug = slug.split("_")[0] if "/" not in slug else slug.rsplit("_", 1)[-1]
        return slug

    # Try /collections/{name}/products/{slug}
    m = re.search(r"_collections_[^_]+_products_(.+?)$", name)
    if m:
        return m.group(1)

    # Fallback: use the whole name if it looks reasonable
    if len(name) > 5 and not name.startswith("http"):
        return name

    return None


def run_match(config, dry_run=False, **_kw):
    """Stage 3: Fuzzy match slugs to SKUs."""
    from wayback_archiver.match import match_products
    from wayback_archiver.util import find_empty_dirs, build_dir_to_slug_map

    metadata = json.loads(config.metadata_file.read_text())

    dir_map = build_dir_to_slug_map(metadata)
    empty_slugs = find_empty_dirs(config.products_dir, dir_map)

    slug_products = {s: metadata[s] for s in empty_slugs if s in metadata}
    sku_products = {s: m for s, m in metadata.items() if m.get("url_type") in ("api", "catalog_api")}

    result = match_products(slug_products, sku_products)

    log.info("Match results:")
    log.info("  Matched: %d", len(result.matched))
    log.info("  Unmatched slugs: %d", len(result.unmatched_slugs))
    log.info("  Unmatched SKUs: %d", len(result.unmatched_skus))

    if dry_run:
        for slug, sku in sorted(result.matched.items()):
            log.info("  %s -> %s", slug, sku)
        return

    for slug, sku in result.matched.items():
        if slug in metadata:
            metadata[slug]["matched_sku"] = sku

    config.metadata_file.write_text(json.dumps(metadata, indent=2))
    log.info("Metadata updated with %d matches", len(result.matched))


def run_download(config, dry_run=False, **_kw):
    """Stage 4: Download images via cascade."""
    from wayback_archiver.download import download_product_images
    from wayback_archiver.normalize import list_images
    from wayback_archiver.util import build_dirname
    import requests

    metadata = json.loads(config.metadata_file.read_text())
    ckpt = StageCheckpoint(config.checkpoint_path("download"), "download")
    ckpt.load()

    tasks = {}
    for slug, meta in metadata.items():
        if ckpt.is_done(slug):
            continue
        links_file = config.links_dir / f"{slug}.txt"
        if not links_file.exists():
            continue
        urls = [l.strip() for l in links_file.read_text().splitlines() if l.strip()]
        if urls:
            tasks[slug] = {"meta": meta, "urls": urls}

    log.info("Download: %d products (%d already done)", len(tasks), len(ckpt.completed))

    if dry_run:
        total_urls = sum(len(t["urls"]) for t in tasks.values())
        log.info("  Total URLs: %d", total_urls)
        return

    config.products_dir.mkdir(exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = "wayback-image-fetch/1.0 (archival research)"

    total_dl, total_fail = 0, 0
    try:
        for i, (slug, task) in enumerate(sorted(tasks.items()), 1):
            meta = task["meta"]
            urls = task["urls"]
            dir_name = build_dirname(
                meta.get("name", slug.replace("-", " ").title()),
                meta.get("date") or None,
            )
            dest_dir = config.products_dir / dir_name

            is_live = any("cdn.shopify.com" in u for u in urls)

            log.info("[%d/%d] %s (%d URLs)", i, len(tasks), slug, len(urls))
            result = download_product_images(
                slug, urls, dest_dir, session,
                cdn_tool=config.cdn_tool_path,
                is_live_cdn=is_live,
            )
            total_dl += result["downloaded"]
            total_fail += result["failed"]

            from wayback_archiver.normalize import rename_batch
            images = list_images(dest_dir)
            rename_batch(images)

            ckpt.mark_done(slug)

    except KeyboardInterrupt:
        log.warning("Interrupted — saving progress")
    finally:
        session.close()

    log.info("DONE — %d downloaded, %d failed", total_dl, total_fail)


def run_normalize(config, dry_run=False, **_kw):
    """Stage 5: Rename images + write metadata.txt."""
    from wayback_archiver.normalize import rename_batch, list_images
    from wayback_archiver.metadata import write_metadata_txt
    from wayback_archiver.util import build_dir_to_slug_map

    metadata = json.loads(config.metadata_file.read_text())
    dir_map = build_dir_to_slug_map(metadata)

    total_renamed = 0
    total_metadata = 0

    for d in sorted(config.products_dir.iterdir()):
        if not d.is_dir():
            continue
        slug = dir_map.get(d.name)
        meta = metadata.get(slug, {}) if slug else {}

        images = list_images(d)
        if images:
            if dry_run:
                log.info("  %s: %d images", d.name, len(images))
            else:
                renames = rename_batch(images)
                total_renamed += len(renames)

        if not dry_run:
            meta_with_defaults = {
                "url": meta.get("url", "Unknown"),
                "name": meta.get("name", d.name),
                "date": meta.get("date"),
                **meta,
            }
            write_metadata_txt(d / "metadata.txt", meta_with_defaults, config.credit_line)
            total_metadata += 1

    if dry_run:
        log.info("[DRY RUN] Would process %d directories", total_metadata)
    else:
        log.info("Renamed %d files, wrote %d metadata.txt files", total_renamed, total_metadata)


def run_build(config, dry_run=False, **_kw):
    """Stage 6: Build final catalog JSON."""
    from wayback_archiver.normalize import list_images
    from wayback_archiver.util import build_dir_to_slug_map

    metadata = json.loads(config.metadata_file.read_text())
    dir_map = build_dir_to_slug_map(metadata)

    catalog = []
    for slug, meta in sorted(metadata.items()):
        name = meta.get("name", slug.replace("-", " ").title())
        from wayback_archiver.util import build_dirname
        dir_name = build_dirname(name, meta.get("date") or None)
        d = config.products_dir / dir_name

        images = sorted(f.name for f in list_images(d)) if d.exists() else []

        price_str = meta.get("price")
        price = None
        if price_str:
            try:
                price = float(price_str)
            except (ValueError, TypeError):
                pass

        catalog.append({
            "slug": slug,
            "name": name,
            "era": meta.get("era"),
            "url": meta.get("url"),
            "date": meta.get("date"),
            "price": price,
            "currency": meta.get("currency"),
            "brand": meta.get("brand"),
            "category": meta.get("category"),
            "sku": meta.get("sku"),
            "color": meta.get("color"),
            "gender": meta.get("gender"),
            "images": images,
            "image_count": len(images),
        })

    config.catalog_file.write_text(json.dumps(catalog, indent=2))

    with_images = sum(1 for c in catalog if c["image_count"] > 0)
    total_images = sum(c["image_count"] for c in catalog)
    by_era = {}
    for c in catalog:
        era = c.get("era", "unknown")
        by_era[era] = by_era.get(era, 0) + 1

    log.info("Catalog: %d products", len(catalog))
    log.info("  With images: %d (%d total files)", with_images, total_images)
    log.info("  Empty: %d", len(catalog) - with_images)
    for era, count in sorted(by_era.items(), key=lambda x: -x[1]):
        log.info("  %s: %d", era, count)
    log.info("Written to %s", config.catalog_file)


def main():
    parser = argparse.ArgumentParser(description="Wayback Archive Pipeline")
    parser.add_argument("stage", choices=[
        "index", "filter", "fetch", "match", "download", "normalize", "build",
    ])
    parser.add_argument("--config", required=True, help="Path to site config YAML")
    parser.add_argument("--dry-run", action="store_true")
    # fetch-specific options
    parser.add_argument("--proxy", choices=["isp", "dc"], default="isp",
                        help="Proxy type for fetch stage (default: isp)")
    parser.add_argument("--workers", type=int, default=5,
                        help="Max concurrent proxy requests for fetch (default: 5)")
    parser.add_argument("--max-retries", type=int, default=3,
                        help="Max retries per URL (default: 3)")
    parser.add_argument("--backoff-factor", type=float, default=2.0,
                        help="Backoff multiplier between retries (default: 2.0)")
    args = parser.parse_args()

    config = load_config(Path(args.config))

    stages = {
        "index": run_index,
        "filter": run_filter,
        "fetch": run_fetch,
        "match": run_match,
        "download": run_download,
        "normalize": run_normalize,
        "build": run_build,
    }

    log.info("=" * 60)
    log.info("Stage: %s | Site: %s | Dry-run: %s", args.stage, config.display_name, args.dry_run)
    log.info("=" * 60)

    # Pass CLI args as kwargs for stages that need them
    stages[args.stage](
        config,
        dry_run=args.dry_run,
        proxy_type=args.proxy,
        workers=args.workers,
        max_retries=args.max_retries,
        backoff_factor=args.backoff_factor,
    )


if __name__ == "__main__":
    main()
