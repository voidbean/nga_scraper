import json
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

import pytest

from nga_scraper import main as cli
from nga_scraper.parser import Post
from nga_scraper.storage import get_data_paths, load_metadata, save_metadata


@pytest.mark.parametrize("last,total,new_page", [
    (3, 3, False),  # completed crawl with no new page
    (5, 3, False),  # repair a checkpoint corrupted by the old bug
    (3, 3, True),   # new page after the previous crawl
    (2, 3, False),  # resume an interrupted crawl
    (0, 0, False),  # first crawl
])
def test_incremental_resume(tmp_path, monkeypatch, last, total, new_page):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["nga-scraper", "--thread-id", "123"])
    monkeypatch.setattr(cli, "load_cookies", lambda _: "")
    if last:
        save_metadata({"last_scraped_page": last, "total_pages": total}, 123)
    session = Mock()
    actual_total = 4 if new_page else 3
    session.get.return_value.content = (
        f"var __PAGE = {{0:'/read.php?tid=123',1:{actual_total},2:1,3:20}};"
    ).encode("gbk")
    monkeypatch.setattr(cli, "build_session", lambda _: session)
    monkeypatch.setattr("nga_scraper.scraper.NGAScraper._throttle", lambda _: None)
    # One post per page: exercise real append/dedup and metadata persistence.
    monkeypatch.setattr(cli, "parse_page", lambda html, page_num, **kwargs: [
        Post(page_num, 123, 42, "author", "", "", "text", [], "text", page_num, page_num)
    ])
    cli.main()
    start = min(last, total) if last else 1
    requested = [int(parse_qs(urlparse(call.args[0]).query)["page"][0])
                 for call in session.get.call_args_list]
    assert requested == list(range(start, actual_total + 1))
    assert load_metadata(123)["last_scraped_page"] == actual_total
    _, posts_file, _, _ = get_data_paths(123)
    original = posts_file.read_bytes()

    session.get.reset_mock()
    cli.main()
    assert session.get.call_count == 1
    assert posts_file.read_bytes() == original
    assert load_metadata(123)["last_scraped_page"] == actual_total
    assert all(json.loads(line)["page"] <= actual_total for line in original.splitlines())

    # A reply added to the existing last page must not be missed.
    monkeypatch.setattr(cli, "parse_page", lambda html, page_num, **kwargs: [
        Post(99, 123, 42, "author", "", "", "new reply", [], "", page_num, 99)
    ])
    cli.main()
    records = [json.loads(line) for line in posts_file.read_text().splitlines()]
    assert records[-1]["post_id"] == 99
    assert records[-1]["page"] == actual_total
    assert load_metadata(123)["last_scraped_page"] == actual_total
