"""
Persistence layer for scraped NGA posts.

Storage format:
  data/posts.jsonl      — one JSON object per line, append-only
  data/metadata.json    — scrape state and statistics
  data/posts.md         — human-readable Markdown export

JSONL is chosen for RAG because:
  - Trivially appendable (no need to rewrite the whole file)
  - Line-by-line streaming (no need to load all posts into memory)
  - Directly ingestible by most vector DB loaders (LangChain, LlamaIndex, etc.)
  - Human-readable with `jq` or any text editor
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from .parser import Post

logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────

def get_data_paths(thread_id: int, author_id: int) -> tuple[Path, Path, Path, Path]:
    """
    Return (data_dir, posts_file, metadata_file, markdown_file) for the given
    thread/author combination.

    Layout: data/{thread_id}/{author_id}/
    """
    data_dir = Path("data") / str(thread_id) / str(author_id)
    return (
        data_dir,
        data_dir / "posts.jsonl",
        data_dir / "metadata.json",
        data_dir / "posts.md",
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_data_dir(thread_id: int, author_id: int) -> None:
    data_dir, *_ = get_data_paths(thread_id, author_id)
    data_dir.mkdir(parents=True, exist_ok=True)


# ── Metadata ──────────────────────────────────────────────────────────────────

def load_metadata(thread_id: int, author_id: int) -> dict:
    """Load metadata.json, returning empty dict if it doesn't exist."""
    _, _, metadata_file, _ = get_data_paths(thread_id, author_id)
    if metadata_file.exists():
        with metadata_file.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_metadata(meta: dict, thread_id: int, author_id: int) -> None:
    """Atomically write metadata.json."""
    ensure_data_dir(thread_id, author_id)
    _, _, metadata_file, _ = get_data_paths(thread_id, author_id)
    meta["last_updated"] = _now_iso()
    tmp = metadata_file.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    tmp.replace(metadata_file)  # atomic rename


def update_metadata(
    last_scraped_page: int,
    thread_id: int,
    author_id: int,
    total_pages: Optional[int] = None,
    total_posts_scraped: Optional[int] = None,
    author_name: Optional[str] = None,
) -> dict:
    """Load, update, and save metadata. Returns the updated dict."""
    meta = load_metadata(thread_id, author_id)
    meta["thread_id"] = thread_id
    meta["author_id"] = author_id
    if author_name is not None:
        meta["author_name"] = author_name
    meta["last_scraped_page"] = last_scraped_page
    if total_pages is not None:
        meta["total_pages"] = total_pages
    if total_posts_scraped is not None:
        meta["total_posts_scraped"] = total_posts_scraped
    save_metadata(meta, thread_id, author_id)
    return meta


# ── JSONL post storage ────────────────────────────────────────────────────────

def load_existing_post_ids(thread_id: int, author_id: int) -> set[int]:
    """
    Read posts.jsonl and return the set of all known post_ids.
    Used for deduplication during incremental updates.
    """
    _, posts_file, _, _ = get_data_paths(thread_id, author_id)
    ids: set[int] = set()
    if not posts_file.exists():
        return ids
    with posts_file.open("r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                ids.add(obj["post_id"])
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning("Corrupt line %d in posts.jsonl: %s", line_num, e)
    return ids


def iter_posts(thread_id: int, author_id: int) -> Iterator[dict]:
    """Iterate over all posts in posts.jsonl as dicts."""
    _, posts_file, _, _ = get_data_paths(thread_id, author_id)
    if not posts_file.exists():
        return
    with posts_file.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    pass


def append_posts(
    posts: list[Post],
    existing_ids: Optional[set[int]],
    thread_id: int,
    author_id: int,
) -> tuple[int, int]:
    """
    Append new posts to posts.jsonl, skipping duplicates.

    Args:
        posts:        List of Post objects to write.
        existing_ids: Set of already-known post_ids for dedup.
                      If None, dedup is skipped (use for full re-scrape).
        thread_id:    Thread ID (determines storage path).
        author_id:    Author ID (determines storage path).

    Returns:
        (written_count, skipped_count) tuple.
    """
    ensure_data_dir(thread_id, author_id)
    _, posts_file, _, _ = get_data_paths(thread_id, author_id)

    written = 0
    skipped = 0

    with posts_file.open("a", encoding="utf-8") as f:
        for post in posts:
            if existing_ids is not None and post.post_id in existing_ids:
                skipped += 1
                continue
            if existing_ids is not None:
                existing_ids.add(post.post_id)

            record = post.to_dict()
            f.write(json.dumps(record, ensure_ascii=False))
            f.write("\n")
            written += 1

    return written, skipped


def clear_posts(thread_id: int, author_id: int) -> None:
    """Delete posts.jsonl for a full re-scrape."""
    _, posts_file, _, _ = get_data_paths(thread_id, author_id)
    if posts_file.exists():
        posts_file.unlink()
    logger.info("Cleared posts.jsonl for full re-scrape")


# ── Markdown export ───────────────────────────────────────────────────────────

_MD_HEADER = """\
# NGA 帖子 {thread_id} — {author_name}（uid={author_id}）的发言

> 导出时间：{exported_at}
> 帖子总数：{total_posts}
> 原帖地址：https://bbs.nga.cn/read.php?tid={thread_id}&authorid={author_id}&opt=262144

"""

_MD_SEPARATOR = "\n\n---\n\n"


def _format_quoted_block(q: dict) -> str:
    """Format a single quoted post as a Markdown blockquote."""
    user = q.get("quoted_user") or f"uid={q.get('quoted_uid', '?')}"
    ts = q.get("quoted_time", "")
    content = q.get("quoted_content", "")
    lines = [f"> **{user}** ({ts}) 写道：", ">"]
    for line in content.split("\n"):
        lines.append(f"> {line}" if line.strip() else ">")
    return "\n".join(lines)


def export_markdown(
    thread_id: int,
    author_id: int,
    author_name: Optional[str] = None,
) -> int:
    """
    Read all posts from posts.jsonl and write a human-readable Markdown file.

    Returns:
        Number of posts exported.
    """
    ensure_data_dir(thread_id, author_id)
    _, _, _, markdown_file = get_data_paths(thread_id, author_id)
    posts = list(iter_posts(thread_id, author_id))
    # Sort by floor for correct ordering
    posts.sort(key=lambda p: p.get("floor", 0))

    # Resolve author_name: use provided value, fall back to what's in metadata,
    # then fall back to str(author_id)
    if author_name is None:
        if posts:
            author_name = posts[0].get("author_name", str(author_id))
        else:
            author_name = str(author_id)

    with markdown_file.open("w", encoding="utf-8") as f:
        f.write(_MD_HEADER.format(
            thread_id=thread_id,
            author_name=author_name,
            author_id=author_id,
            exported_at=_now_iso(),
            total_posts=len(posts),
        ))

        for i, post in enumerate(posts):
            subject = post.get("subject", "")
            floor = post.get("floor", "?")
            timestamp = post.get("timestamp", "")
            content = post.get("content", "")
            post_id = post.get("post_id", "")
            page = post.get("page", "")

            # Header line
            subject_part = f" — {subject}" if subject else ""
            f.write(f"## [{floor}楼] {timestamp}{subject_part}\n\n")

            # Quoted blocks
            for q in post.get("quoted_posts", []):
                f.write(_format_quoted_block(q))
                f.write("\n\n")

            # Main content
            f.write(content)
            f.write("\n\n")

            # Footer metadata
            f.write(f"*post_id: {post_id} | 第 {page} 页*")

            if i < len(posts) - 1:
                f.write(_MD_SEPARATOR)
            else:
                f.write("\n")

    logger.info("Exported %d posts to %s", len(posts), markdown_file)
    return len(posts)
