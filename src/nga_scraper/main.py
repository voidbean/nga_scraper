"""
CLI entry point for the NGA scraper.

Usage:
    uv run nga-scraper                    # incremental update
    uv run nga-scraper --full             # full re-scrape
    uv run nga-scraper --start-page 50   # start from page 50
    uv run nga-scraper --pages 1-10      # scrape pages 1 through 10
    uv run nga-scraper --export-md       # export to markdown only
    uv run nga-scraper --cookies "..."   # override cookies inline
    uv run nga-scraper --dry-run         # parse but don't write to disk
    uv run nga-scraper -v                # verbose logging
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from .scraper import NGAScraper, build_session, DEFAULT_THREAD_ID, DEFAULT_AUTHOR_ID
from .parser import parse_page
from .storage import (
    append_posts,
    clear_posts,
    export_markdown,
    load_existing_post_ids,
    load_metadata,
    update_metadata,
)

# ── Logging setup ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── Cookie loading ────────────────────────────────────────────────────────────

COOKIE_FILE = Path("cookies.txt")
COOKIE_ENV_VAR = "NGA_COOKIES"


def load_cookies(override: Optional[str] = None) -> str:
    """
    Load cookie string from (in priority order):
    1. --cookies CLI argument
    2. NGA_COOKIES environment variable
    3. cookies.txt file in the current directory

    The cookie string format is the raw HTTP Cookie header value:
        "ngacn0comUserInfo=xxx; ngacn0comUserKey=yyy; ..."
    """
    if override:
        return override

    env_cookies = os.environ.get(COOKIE_ENV_VAR, "").strip()
    if env_cookies:
        logger.info("Using cookies from %s environment variable", COOKIE_ENV_VAR)
        return env_cookies

    if COOKIE_FILE.exists():
        cookies = COOKIE_FILE.read_text(encoding="utf-8").strip()
        if cookies:
            logger.info("Using cookies from %s", COOKIE_FILE)
            return cookies

    logger.error(
        "No cookies found. Provide via --cookies, %s env var, or %s file.",
        COOKIE_ENV_VAR,
        COOKIE_FILE,
    )
    sys.exit(1)


# ── Argument parsing ──────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="nga-scraper",
        description="爬取 NGA BBS 帖子，整理为 RAG 可用的结构化数据",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  uv run nga-scraper                    # 增量更新（从上次断点继续）
  uv run nga-scraper --full             # 全量重爬（清空重来）
  uv run nga-scraper --start-page 50   # 从第50页开始
  uv run nga-scraper --pages 1-10      # 只爬第1-10页
  uv run nga-scraper --export-md       # 仅导出 Markdown（不爬取）
  uv run nga-scraper --dry-run -v      # 测试解析，不写磁盘
        """,
    )

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--full",
        action="store_true",
        help="全量重爬：删除现有数据，从第1页重新开始",
    )
    mode_group.add_argument(
        "--start-page",
        type=int,
        metavar="N",
        help="从第N页开始爬取（覆盖增量断点）",
    )
    mode_group.add_argument(
        "--pages",
        type=str,
        metavar="START-END",
        help="爬取指定页范围，例如 '1-10' 或 '42'",
    )

    parser.add_argument(
        "--export-md",
        action="store_true",
        help="将所有已爬取帖子导出为 data/posts.md（可与爬取同时使用）",
    )
    parser.add_argument(
        "--cookies",
        type=str,
        default=None,
        metavar="COOKIE_STRING",
        help="直接提供 Cookie 字符串（优先级最高）",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.5,
        metavar="SECONDS",
        help="请求间隔秒数（默认 1.5s）",
    )
    parser.add_argument(
        "--thread-id",
        type=int,
        default=DEFAULT_THREAD_ID,
        help=f"帖子 TID（默认 {DEFAULT_THREAD_ID}）",
    )
    parser.add_argument(
        "--author-id",
        type=int,
        default=DEFAULT_AUTHOR_ID,
        help=f"只看该 UID 的发言（默认 {DEFAULT_AUTHOR_ID}）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只解析，不写入磁盘（用于测试）",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="显示详细日志（DEBUG 级别）",
    )

    return parser.parse_args()


# ── Page range parsing ────────────────────────────────────────────────────────

def parse_page_range(pages_str: str) -> tuple[int, Optional[int]]:
    """
    Parse a page range string like "1-10" or "42" into (start, end).
    Returns (start, start) if only one page is specified.
    """
    parts = pages_str.split("-")
    if len(parts) == 1:
        n = int(parts[0])
        return n, n
    elif len(parts) == 2:
        return int(parts[0]), int(parts[1])
    else:
        raise ValueError(f"无效的页范围: {pages_str!r}，请使用 'N' 或 'N-M' 格式")


# ── Progress display ──────────────────────────────────────────────────────────

def _progress(
    current: int,
    total: Optional[int],
    total_written: int,
    total_skipped: int,
    page_posts: int,
) -> None:
    """Print a progress line to stderr."""
    if total:
        pct = current / total * 100
        bar_len = 25
        filled = int(bar_len * current / total)
        bar = "█" * filled + "░" * (bar_len - filled)
        print(
            f"\r  [{bar}] 第 {current}/{total} 页 ({pct:.0f}%) "
            f"| 本页 {page_posts} 条 | 累计写入 {total_written} 条，跳过 {total_skipped} 条   ",
            end="",
            flush=True,
            file=sys.stderr,
        )
    else:
        print(
            f"\r  第 {current}/? 页 | 本页 {page_posts} 条 "
            f"| 累计写入 {total_written} 条，跳过 {total_skipped} 条   ",
            end="",
            flush=True,
            file=sys.stderr,
        )


# ── Main orchestration ────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # ── Pure export-only mode (no scraping flags given) ───────────────────────
    if (
        args.export_md
        and not args.full
        and args.start_page is None
        and args.pages is None
    ):
        count = export_markdown(
            thread_id=args.thread_id,
            author_id=args.author_id,
        )  # author_name resolved from posts data automatically
        print(f"已导出 {count} 条帖子到 data/posts.md")
        return

    # ── Determine page range ──────────────────────────────────────────────────

    start_page: int
    end_page: Optional[int] = None
    existing_ids: Optional[set[int]]

    if args.full:
        start_page = 1
        end_page = None  # will be discovered from first page
        clear_posts(args.thread_id, args.author_id)
        logger.info("全量重爬模式：已清空现有数据")
        existing_ids = set()

    elif args.pages:
        start_page, end_page = parse_page_range(args.pages)
        existing_ids = load_existing_post_ids(args.thread_id, args.author_id)
        logger.info("指定页范围模式：第 %d–%s 页", start_page, end_page or "?")

    elif args.start_page:
        start_page = args.start_page
        existing_ids = load_existing_post_ids(args.thread_id, args.author_id)
        logger.info("从第 %d 页开始爬取", start_page)

    else:
        # Incremental: resume from last scraped page + 1
        meta = load_metadata(args.thread_id, args.author_id)
        last = meta.get("last_scraped_page", 0)
        start_page = last + 1 if last > 0 else 1
        existing_ids = load_existing_post_ids(args.thread_id, args.author_id)
        if last > 0:
            logger.info(
                "增量模式：从第 %d 页继续（上次爬到第 %d 页）",
                start_page, last,
            )
        else:
            logger.info("未找到历史记录，从第 1 页开始")

    # ── Build HTTP session ────────────────────────────────────────────────────

    cookies = load_cookies(args.cookies)
    session = build_session(cookies)
    scraper = NGAScraper(
        session,
        thread_id=args.thread_id,
        author_id=args.author_id,
        delay=args.delay,
    )

    # ── Scrape loop ───────────────────────────────────────────────────────────

    total_written = 0
    total_skipped = 0
    last_page_scraped = start_page - 1

    print(f"\n开始爬取，起始页：{start_page}...\n", file=sys.stderr)

    try:
        for page_num, html in scraper.iter_pages(start=start_page, end=end_page):
            # Parse posts from this page
            try:
                posts = parse_page(
                    html,
                    page_num=page_num,
                    thread_id=args.thread_id,
                    author_id=args.author_id,
                )
            except Exception as e:
                logger.error("第 %d 页解析失败: %s", page_num, e, exc_info=True)
                posts = []

            # Write to storage
            if not args.dry_run and posts:
                written, skipped = append_posts(posts, existing_ids, args.thread_id, args.author_id)
            elif args.dry_run:
                written = len(posts)
                skipped = 0
                if args.verbose and posts:
                    for p in posts[:2]:  # show first 2 posts in verbose dry-run
                        logger.debug(
                            "  [dry-run] floor=%d pid=%d ts=%s content_len=%d quotes=%d",
                            p.floor, p.post_id, p.timestamp,
                            len(p.content), len(p.quoted_posts)
                        )
            else:
                written, skipped = 0, 0

            total_written += written
            total_skipped += skipped
            last_page_scraped = page_num

            # Update metadata after each page (crash-safe resume)
            if not args.dry_run:
                update_metadata(
                    last_scraped_page=page_num,
                    thread_id=args.thread_id,
                    author_id=args.author_id,
                    total_pages=scraper.total_pages,
                    total_posts_scraped=total_written,
                )

            _progress(page_num, scraper.total_pages, total_written, total_skipped, len(posts))

    except KeyboardInterrupt:
        print("\n\n用户中断。", file=sys.stderr)
    except Exception as e:
        print(f"\n\n致命错误: {e}", file=sys.stderr)
        logger.exception("爬取过程中发生致命错误")
        sys.exit(1)
    finally:
        print("\n", file=sys.stderr)  # newline after progress bar

    # ── Summary ───────────────────────────────────────────────────────────────

    mode_str = "[dry-run] " if args.dry_run else ""
    print(
        f"{mode_str}完成。"
        f"爬取页范围：{start_page}–{last_page_scraped} | "
        f"写入 {total_written} 条 | "
        f"跳过重复 {total_skipped} 条"
    )

    # ── Optional Markdown export ──────────────────────────────────────────────

    if args.export_md and not args.dry_run:
        count = export_markdown(
            thread_id=args.thread_id,
            author_id=args.author_id,
        )  # author_name resolved from posts data automatically
        print(f"已导出 {count} 条帖子到 data/posts.md")
