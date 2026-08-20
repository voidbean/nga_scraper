"""
HTTP fetching layer for NGA BBS.

Responsibilities:
- Build and manage a requests.Session with cookies/headers
- Fetch pages with GBK decoding
- Retry logic with exponential backoff
- Rate limiting between requests
- Extract total page count from JS variable
"""

from __future__ import annotations

import re
import time
import logging
from typing import Iterator, Optional
from urllib.parse import parse_qs, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

BASE_URL = "https://bbs.nga.cn/read.php"

# Matches: var __PAGE = {0:'/read.php?...',1:111,2:1,3:20};
_PAGE_RE = re.compile(
    r"var\s+__PAGE\s*=\s*\{0\s*:\s*'[^']*'\s*,\s*1\s*:\s*(\d+)\s*,\s*2\s*:\s*(\d+)\s*,\s*3\s*:\s*(\d+)\s*\}"
)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/147.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}


# ── Session builder ───────────────────────────────────────────────────────────

def build_session(
    cookie_string: str,
    extra_headers: Optional[dict] = None,
    max_retries: int = 3,
) -> requests.Session:
    """
    Create a requests.Session pre-loaded with NGA auth cookies and headers.

    Args:
        cookie_string: Raw cookie header string, e.g.
            "ngacn0comUserInfo=xxx; ngacn0comUserKey=yyy"
        extra_headers: Any additional headers to merge in.
        max_retries: Number of automatic HTTP retries on 5xx / connection errors.

    Returns:
        Configured requests.Session ready to use.
    """
    session = requests.Session()

    # Mount retry adapter for both http and https
    retry = Retry(
        total=max_retries,
        backoff_factor=1.5,           # waits: 0s, 1.5s, 3s, 6s ...
        status_forcelist={500, 502, 503, 504},
        allowed_methods={"GET"},
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    # Headers
    headers = {**DEFAULT_HEADERS}
    if extra_headers:
        headers.update(extra_headers)
    session.headers.update(headers)

    # Cookies — parse the raw cookie string into the session's cookie jar
    for part in cookie_string.split(";"):
        part = part.strip()
        if "=" in part:
            name, _, value = part.partition("=")
            session.cookies.set(name.strip(), value.strip(), domain="bbs.nga.cn")

    return session


def parse_nga_url(url: str) -> tuple[int, Optional[int]]:
    """
    Extract (thread_id, author_id) from an NGA read.php URL.

    Examples:
        https://bbs.nga.cn/read.php?tid=123
        https://bbs.nga.cn/read.php?tid=123&authorid=456&opt=262144
    """
    parsed = urlparse(url.strip())
    qs = parse_qs(parsed.query)
    tid_values = qs.get("tid") or []
    if not tid_values:
        raise ValueError(f"URL 中缺少 tid 参数: {url!r}")
    try:
        thread_id = int(tid_values[0])
    except ValueError as exc:
        raise ValueError(f"无效的 tid 参数: {tid_values[0]!r}") from exc

    author_id: Optional[int] = None
    aid_values = qs.get("authorid") or []
    if aid_values:
        try:
            author_id = int(aid_values[0])
        except ValueError as exc:
            raise ValueError(f"无效的 authorid 参数: {aid_values[0]!r}") from exc

    return thread_id, author_id


# ── Page fetcher ──────────────────────────────────────────────────────────────

class NGAScraper:
    """
    Fetches pages from a single NGA thread, optionally filtered by author.

    Usage:
        scraper = NGAScraper(session, thread_id=123, author_id=456)
        html = scraper.fetch_page(1)
        total = scraper.total_pages  # populated after first fetch
    """

    def __init__(
        self,
        session: requests.Session,
        thread_id: int,
        author_id: Optional[int] = None,
        delay: float = 1.5,
        timeout: int = 30,
    ) -> None:
        self.session = session
        self.thread_id = thread_id
        self.author_id = author_id
        self.delay = delay           # seconds between requests
        self.timeout = timeout
        self.total_pages: Optional[int] = None
        self._last_request_time: float = 0.0

    def _throttle(self) -> None:
        """Sleep if needed to respect the configured delay between requests."""
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)

    def _page_url(self, page: int) -> str:
        params = [f"tid={self.thread_id}"]
        if self.author_id is not None:
            params.append(f"authorid={self.author_id}")
        params.extend(["opt=262144", f"page={page}"])
        return f"{BASE_URL}?{'&'.join(params)}"

    def fetch_page(self, page: int) -> str:
        """
        Fetch a single page and return the decoded HTML string (GBK -> str).

        Raises:
            requests.HTTPError: on non-2xx after retries exhausted.
            requests.ConnectionError / Timeout: on network failure.
        """
        self._throttle()
        url = self._page_url(page)
        logger.debug("GET %s", url)

        resp = self.session.get(url, timeout=self.timeout)
        self._last_request_time = time.monotonic()

        resp.raise_for_status()

        # NGA serves GBK; requests may mis-detect the encoding.
        # Force GBK decode from raw bytes.
        html = resp.content.decode("gbk", errors="replace")

        # Parse total pages from JS if not yet known
        if self.total_pages is None:
            m = _PAGE_RE.search(html)
            if m:
                self.total_pages = int(m.group(1))
                logger.info("Total pages: %d", self.total_pages)

        return html

    def iter_pages(
        self,
        start: int = 1,
        end: Optional[int] = None,
    ) -> Iterator[tuple[int, str]]:
        """
        Generator that yields (page_number, html) tuples.

        Args:
            start: First page to fetch (1-indexed).
            end:   Last page to fetch (inclusive). If None, fetches until
                   total_pages (discovered from the first response).
        """
        # Fetch the first page to discover total_pages if end is unknown
        html = self.fetch_page(start)
        yield start, html

        # Determine the actual end page
        if end is None:
            end = self.total_pages or start  # fallback: just the one page

        for page in range(start + 1, end + 1):
            html = self.fetch_page(page)
            yield page, html
