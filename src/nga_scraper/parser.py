"""
HTML and UBB markup parser for NGA BBS posts.

Responsibilities:
- Extract post metadata (pid, timestamp, subject, floor)
- Clean HTML content to plain text
- Parse nested UBB quote blocks into structured objects
- Resolve author name from known uid mapping or JS userInfo
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

THREAD_ID = 45974302
AUTHOR_ID = 150058
AUTHOR_NAME = "-阿狼-"

# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class QuotedPost:
    """A single quoted post extracted from UBB [quote]...[/quote] markup."""
    quoted_pid: Optional[int]       # pid of the quoted post (from [pid=...])
    quoted_tid: Optional[int]       # tid (from [pid=...,tid,...])
    quoted_uid: Optional[int]       # uid of quoted author
    quoted_user: Optional[str]      # username of quoted author
    quoted_time: Optional[str]      # ISO timestamp string of quoted post
    quoted_content: str             # cleaned text of the quoted content

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Post:
    """A single scraped post, ready for JSONL storage and RAG ingestion."""
    post_id: int                        # from pid{N}Anchor; 0 = thread opener
    thread_id: int
    author_id: int
    author_name: str
    timestamp: str                      # ISO 8601 e.g. "2026-01-12T09:05:00"
    subject: str                        # may be empty string
    content: str                        # cleaned plain text (quotes removed)
    quoted_posts: list[QuotedPost]      # extracted quote blocks
    raw_content: str                    # original HTML/UBB preserved
    page: int                           # which page this was scraped from
    floor: int                          # 楼层 (1-based, cumulative across pages)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# ── Known author map (uid -> name) ────────────────────────────────────────────
# NGA loads author names via JS; the <a> tag is empty in raw HTML.
# We maintain a small static map for the target author and populate
# others from the JS userInfo blob if present.

KNOWN_AUTHORS: dict[int, str] = {
    150058: "-阿狼-",
}

# Matches: commonui.userInfo.setAll("uid\tname\t...", ...)
_USERINFO_RE = re.compile(
    r'commonui\.userInfo\.setAll\(\s*"([^"]+)"'
)


def extract_known_authors(html: str) -> dict[int, str]:
    """
    Scan the page's inline JS for commonui.userInfo.setAll() calls and
    extract uid->name mappings.

    NGA encodes user info as a tab-separated string:
        "uid\\tname\\tregdate\\t..."
    Multiple users may appear in one call, separated by \\n.
    """
    authors: dict[int, str] = dict(KNOWN_AUTHORS)  # start with static map

    for m in _USERINFO_RE.finditer(html):
        blob = m.group(1)
        # Each line is one user record; fields are tab-separated
        for line in blob.split("\\n"):
            parts = line.split("\\t")
            if len(parts) >= 2:
                try:
                    uid = int(parts[0])
                    name = parts[1]
                    if uid and name:
                        authors[uid] = name
                except (ValueError, IndexError):
                    pass

    return authors


# ── UBB Quote parser ──────────────────────────────────────────────────────────

# Matches the opening of a quote block:
# [quote][pid=854270386,45974302,3]Reply[/pid] [b]Post by [uid=60027718]xiaomiwang1[/uid] (2026-01-12 09:07):[/b]
_QUOTE_HEADER_RE = re.compile(
    r'\[quote\]'
    r'(?:\[pid=(\d+),(\d+),\d+\]Reply\[/pid\]\s*)?'   # optional [pid=...]
    r'(?:\[b\]Post by '
    r'\[uid=(\d+)\]([^\[]*)\[/uid\]'                   # [uid=N]name[/uid]
    r'\s*\(([^)]+)\)'                                   # (timestamp)
    r':\[/b\])?',                                       # optional entire header
    re.DOTALL,
)


def _clean_html_fragment(fragment: str) -> str:
    """
    Strip all HTML tags from a fragment and normalize whitespace.
    Also strips residual UBB tags like [b], [/b], [img], etc.
    """
    # Remove HTML tags
    text = re.sub(r'<[^>]+>', ' ', fragment)
    # Remove UBB tags (anything in [...])
    text = re.sub(r'\[[^\]]*\]', '', text)
    # Normalize whitespace
    text = re.sub(r'[ \t]+', ' ', text)
    # Normalize multiple newlines
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def parse_quotes(raw_html: str) -> tuple[list[QuotedPost], str]:
    """
    Extract all [quote]...[/quote] blocks from raw UBB/HTML content.

    Returns:
        (list_of_QuotedPost, remaining_content_with_quotes_removed)

    Handles:
    - Nested quotes (outer quote contains inner quote) — only outermost extracted
    - Missing [pid=...] header (old-style quotes)
    - Missing [uid=...] (anonymous or deleted user quotes)
    - HTML <br/> tags within quote content
    """
    quotes: list[QuotedPost] = []
    result_parts: list[str] = []
    pos = 0

    while pos < len(raw_html):
        open_idx = raw_html.find('[quote]', pos)
        if open_idx == -1:
            result_parts.append(raw_html[pos:])
            break

        # Append text before this quote
        result_parts.append(raw_html[pos:open_idx])

        # Find the matching [/quote] accounting for nesting
        depth = 1
        search_pos = open_idx + len('[quote]')
        close_idx = -1

        while depth > 0 and search_pos < len(raw_html):
            next_open = raw_html.find('[quote]', search_pos)
            next_close = raw_html.find('[/quote]', search_pos)

            if next_close == -1:
                # Malformed: no closing tag — consume rest
                search_pos = len(raw_html)
                depth = 0
                break

            if next_open != -1 and next_open < next_close:
                depth += 1
                search_pos = next_open + len('[quote]')
            else:
                depth -= 1
                if depth == 0:
                    close_idx = next_close
                else:
                    search_pos = next_close + len('[/quote]')

        if close_idx == -1:
            # Malformed quote — skip, advance past the [quote] tag
            result_parts.append('[quote]')
            pos = open_idx + len('[quote]')
            continue

        quote_block = raw_html[open_idx: close_idx + len('[/quote]')]
        inner = raw_html[open_idx + len('[quote]'): close_idx]

        # Parse the header
        header_m = _QUOTE_HEADER_RE.match(quote_block)
        quoted_pid: Optional[int] = None
        quoted_tid: Optional[int] = None
        quoted_uid: Optional[int] = None
        quoted_user: Optional[str] = None
        quoted_time: Optional[str] = None

        if header_m:
            if header_m.group(1):
                try:
                    quoted_pid = int(header_m.group(1))
                except ValueError:
                    pass
            if header_m.group(2):
                try:
                    quoted_tid = int(header_m.group(2))
                except ValueError:
                    pass
            if header_m.group(3):
                try:
                    quoted_uid = int(header_m.group(3))
                except ValueError:
                    pass
            if header_m.group(4):
                quoted_user = header_m.group(4).strip()
            if header_m.group(5):
                quoted_time = _normalize_timestamp(header_m.group(5).strip())

            # Content is everything after the header match within inner
            # header_m matched against quote_block which starts with [quote]
            # inner starts right after [quote], so offset = header_m.end() - len('[quote]')
            content_start = header_m.end() - len('[quote]')
            raw_quoted_content = inner[content_start:]
        else:
            raw_quoted_content = inner

        # Strip leading <br/> separators that NGA inserts between header and content
        raw_quoted_content = re.sub(r'^(\s*<br\s*/?>\s*){1,2}', '', raw_quoted_content)

        quoted_content = _clean_html_fragment(raw_quoted_content)

        quotes.append(QuotedPost(
            quoted_pid=quoted_pid,
            quoted_tid=quoted_tid,
            quoted_uid=quoted_uid,
            quoted_user=quoted_user,
            quoted_time=quoted_time,
            quoted_content=quoted_content,
        ))

        pos = close_idx + len('[/quote]')

    remaining_text = ''.join(result_parts)
    return quotes, remaining_text


def _normalize_timestamp(ts: str) -> str:
    """
    Convert NGA timestamp formats to ISO 8601.

    Observed formats:
      "2026-01-12 09:05"    -> "2026-01-12T09:05:00"
      "2026-01-12 09:05:30" -> "2026-01-12T09:05:30"
    """
    ts = ts.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(ts, fmt).isoformat()
        except ValueError:
            continue
    # Return as-is if we can't parse it
    return ts


# ── Post extractor ────────────────────────────────────────────────────────────

# Matches <a id='pidNNNAnchor'> — N is the post ID
_PID_ANCHOR_RE = re.compile(r'^pid(\d+)Anchor$')

# Matches postrow row IDs: post1strow{N} where N is the 0-based index on the page
_POSTROW_ID_RE = re.compile(r'^post1strow(\d+)$')

# Matches author uid from href
_UID_RE = re.compile(r'uid=(\d+)')


def _extract_post_id(container: Tag) -> int:
    """
    Find the post ID from <a id='pidNNNAnchor'> within a post container.
    Falls back to 0 if not found (thread opener).
    """
    anchor = container.find('a', id=_PID_ANCHOR_RE)
    if anchor:
        m = _PID_ANCHOR_RE.match(anchor.get('id', ''))
        if m:
            return int(m.group(1))
    return 0


def _extract_timestamp(container: Tag, post_index: int) -> str:
    """Extract and normalize the post timestamp."""
    span = container.find('span', id=f'postdate{post_index}')
    if span:
        return _normalize_timestamp(span.get_text(strip=True))
    return ""


def _extract_subject(container: Tag, post_index: int) -> str:
    """Extract the post subject/title (h3 element)."""
    h3 = container.find('h3', id=f'postsubject{post_index}')
    if h3:
        return h3.get_text(strip=True)
    return ""


def parse_page(
    html: str,
    page_num: int,
    thread_id: int = THREAD_ID,
    author_id: int = AUTHOR_ID,
    page_size: int = 20,
) -> list[Post]:
    """
    Parse a full page of HTML and return a list of Post objects.

    Args:
        html:       Decoded HTML string (already GBK-decoded).
        page_num:   1-based page number (used for floor calculation).
        thread_id:  Thread ID for metadata.
        author_id:  Author ID for metadata.
        page_size:  Posts per page (default 20, from NGA's __PAGE JS).

    Returns:
        List of Post objects found on this page.
    """
    soup = BeautifulSoup(html, "lxml")

    # Build uid->name map from inline JS
    author_map = extract_known_authors(html)

    posts: list[Post] = []

    # Find all post rows: <tr id='post1strow{N}' class='postrow row2'>
    post_rows = soup.find_all('tr', id=_POSTROW_ID_RE)

    if not post_rows:
        logger.warning(
            "No post rows found on page %d — possible login redirect or empty page",
            page_num
        )

    for row in post_rows:
        row_id_m = _POSTROW_ID_RE.match(row.get('id', ''))
        if not row_id_m:
            continue
        post_index = int(row_id_m.group(1))  # 0-based index within this page

        # Floor is 1-based, cumulative across all pages
        floor = (page_num - 1) * page_size + post_index + 1

        # Verify this post belongs to our target author
        author_link = row.find('a', id=f'postauthor{post_index}')
        if author_link:
            href = author_link.get('href', '')
            uid_m = _UID_RE.search(href)
            if uid_m and int(uid_m.group(1)) != author_id:
                logger.warning(
                    "Unexpected author uid=%s on page %d index %d — skipping",
                    uid_m.group(1), page_num, post_index
                )
                continue

        # Get the post container td
        container = row.find('td', id=f'postcontainer{post_index}')
        if not container:
            logger.warning("No postcontainer%d on page %d", post_index, page_num)
            continue

        # Extract post ID
        post_id = _extract_post_id(container)

        # Extract timestamp
        timestamp = _extract_timestamp(container, post_index)

        # Extract subject
        subject = _extract_subject(container, post_index)

        # Extract raw content HTML (the full span/p element as string)
        # NGA uses <span> for most posts but <p> for the thread opener (floor 1)
        content_span = container.find(
            ['span', 'p'], id=f'postcontent{post_index}'
        )
        if content_span:
            raw_content = str(content_span)
            inner_html = content_span.decode_contents()
            quoted_posts, remaining_html = parse_quotes(inner_html)
            content = _clean_html_fragment(remaining_html)
        else:
            raw_content = ""
            quoted_posts = []
            content = ""

        # Resolve author name
        author_name = author_map.get(
            author_id,
            KNOWN_AUTHORS.get(author_id, str(author_id))
        )

        posts.append(Post(
            post_id=post_id,
            thread_id=thread_id,
            author_id=author_id,
            author_name=author_name,
            timestamp=timestamp,
            subject=subject,
            content=content,
            quoted_posts=quoted_posts,
            raw_content=raw_content,
            page=page_num,
            floor=floor,
        ))

    return posts
