# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Generic NGA BBS scraper that fetches posts from any thread (optionally filtered by author) and stores them as JSONL for RAG ingestion. Managed with [uv](https://docs.astral.sh/uv/).

## Commands

```bash
# Install dependencies
uv sync

# Run the scraper (incremental update from last checkpoint)
uv run nga-scraper --url "https://bbs.nga.cn/read.php?tid=123"

# Full re-scrape (clears existing data)
uv run nga-scraper --url "https://bbs.nga.cn/read.php?tid=123" --full

# Scrape a page range
uv run nga-scraper --url "https://bbs.nga.cn/read.php?tid=123" --pages 1-10

# Dry-run (parse without writing to disk, useful for testing parser changes)
uv run nga-scraper --url "https://bbs.nga.cn/read.php?tid=123" --dry-run --pages 1-1 -v

# Export collected posts to Markdown
uv run nga-scraper --url "https://bbs.nga.cn/read.php?tid=123" --export-md

# Watch for new posts (polls every 3 minutes, Ctrl+C to exit)
uv run nga-scraper --url "https://bbs.nga.cn/read.php?tid=123" --watch

# Run tests
uv run pytest
```

A thread must be specified with `--url` or `--thread-id`. Pass `--author-id` (or include `authorid` in the URL) to scrape only that user's posts; omit it to scrape the whole thread.

## Cookie Setup (Required)

NGA requires login cookies. Load priority:
1. `--cookies "..."` CLI argument
2. `NGA_COOKIES` environment variable
3. `cookies.txt` file in the project root (gitignored)

## Architecture

The pipeline is a linear four-module chain: **scraper → parser → storage**, orchestrated by **main**.

### `scraper.py` — HTTP layer
- `parse_nga_url(url)`: extracts `tid` and optional `authorid` from an NGA `read.php` URL.
- `build_session(cookie_string)`: creates a `requests.Session` with NGA auth cookies, browser-like headers, and `urllib3.Retry` exponential backoff (3 retries, 5xx errors).
- `NGAScraper.fetch_page(page)`: fetches a page, **force-decodes bytes as GBK** (NGA's encoding), and extracts `total_pages` from the inline JS variable `__PAGE` via regex on first fetch.
- `NGAScraper.iter_pages(start, end)`: generator yielding `(page_num, html)` tuples with rate limiting via `time.monotonic()`.

### `parser.py` — HTML/UBB parsing
- `parse_page(html, page_num)`: entry point. Uses BeautifulSoup (`lxml`) to find `<tr id='post1strow{N}'>` rows, then extracts post metadata from NGA's ID-based element scheme (`postcontainer{N}`, `postdate{N}`, `postsubject{N}`, `postcontent{N}`).
- `parse_quotes(raw_html)`: depth-counter character scan to extract outermost `[quote]...[/quote]` UBB blocks, returning `(list[QuotedPost], remaining_html)`. Handles nesting, malformed tags, and old-style quotes without headers.
- `extract_known_authors(html)`: scans inline JS `commonui.userInfo.setAll(...)` calls to build a `uid → name` map.
- Data models: `Post` and `QuotedPost` are `@dataclass` with `.to_dict()` for JSON serialization.

### `storage.py` — Persistence
- All data lives in `data/{tid}/{uid or all}/` (gitignored): `posts.jsonl`, `metadata.json`, `posts.md`.
- `append_posts(posts, existing_ids)`: appends to JSONL, skipping duplicates by `post_id`. The `existing_ids` set is loaded at startup and updated in-place during the run.
- `update_metadata(...)`: atomically writes `metadata.json` via a `.tmp` rename after each page — enables crash-safe resume.
- `export_markdown(...)`: reads all JSONL posts, sorts by `floor`, and writes `posts.md` with blockquote-formatted `quoted_posts`.

### `main.py` — CLI orchestration
- `argparse`-based CLI with mutually exclusive mode flags: `--full`, `--start-page`, `--pages`, `--watch` (incremental is the default).
- Incremental mode reads `last_scraped_page` from `metadata.json` and resumes from `last + 1`.
- The scrape loop calls `parse_page` → `append_posts` → `update_metadata` per page, with a progress bar written to stderr.
- `--watch` mode (`run_watch()`): polls the last page every `WATCH_INTERVAL` (180 s), prints new posts to stdout, and runs until `KeyboardInterrupt`. Single-poll failures are logged as warnings without stopping the loop.

## Key Implementation Details

- **Floor calculation**: `floor = (page_num - 1) * page_size + post_index + 1` where `page_size=20`.
- **Author name resolution**: NGA renders author names via JS, not in static HTML. The `<a>` tag is empty; names come from `commonui.userInfo.setAll()` JS blobs, falling back to the numeric uid.
- **`post_id = 0`** for the thread-opening post (no `pidNNNAnchor` element).
- **`data/` directory** is relative to the working directory where `uv run nga-scraper` is invoked (project root).
