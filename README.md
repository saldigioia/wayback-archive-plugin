# wayback-archive

Claude Code plugin for recovering complete product databases (catalog data + images) from defunct e-commerce websites using the Wayback Machine, CommonCrawl, and Shopify CDN archaeology.

Supports Shopify, Swell Commerce, Fourthwall, and custom platforms via config-driven CDN patterns.

## Installation

Add the marketplace and install:

```
/plugin marketplace add saldigioia/wayback-archive-plugin
/plugin install wayback-archive@rare-data-club
```

Then install Python dependencies:

```bash
pip install -r ~/.claude/plugins/cache/rare-data-club/wayback-archive/1.0.0/requirements.txt
```

### Local development

```bash
claude --plugin-dir ./wayback-archive
```

## Usage

Once installed, the skill is available as `/wayback-archive:wayback-archive`. Claude also invokes it automatically when you mention recovering products from dead websites, CDX dumps, Wayback Machine, or archived stores.

## Quick Start

```bash
# 1. Copy and customize a config for your target site
cp skills/wayback-archive/configs/example.yaml configs/mysite.yaml
# Edit configs/mysite.yaml with your domains

# 2. Run the full pipeline (with confirmation gates)
python3 scripts/run_stage.py all --config configs/mysite.yaml

# Or dry-run first
python3 scripts/run_stage.py all --config configs/mysite.yaml --dry-run
```

## Prerequisites

- Python 3.10+
- Proxy credentials (optional): set `OXYLABS_ISP_USER` and `OXYLABS_ISP_PASS` environment variables, or copy `tools/.env.example` to `tools/.env`

## Pipeline

```
cdx_dump -> index -> filter -> fetch -> cdn_discover -> match -> download -> normalize -> build
```

Nine stages from domain name to complete product catalog. Run individually or use `all`. See the [skill documentation](skills/wayback-archive/SKILL.md) for details.

## Structure

```
wayback-archive/
├── .claude-plugin/
│   ├── plugin.json              # Plugin manifest
│   └── marketplace.json         # Marketplace catalog
├── skills/wayback-archive/      # Skill definition + reference docs
├── scripts/                     # Pipeline orchestrator
├── lib/wayback_archiver/        # Python library modules
├── tools/                       # Bundled tools (wayback_cdx, cdn probe)
├── fetch_archive.py             # Multi-strategy page fetcher
├── filter_cdx.py                # CDX dump filter
├── shopify_downloader.py        # Shopify CDN archaeology
└── requirements.txt             # Python dependencies
```
