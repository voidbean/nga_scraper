from unittest.mock import Mock

import pytest

from nga_scraper.scraper import NGAScraper


def make_scraper(total=3):
    session = Mock()
    session.get.return_value.content = (
        f"var __PAGE = {{0:'/read.php?tid=123',1:{total},2:1,3:20}};"
    ).encode("gbk")
    return NGAScraper(session, thread_id=123, delay=0)


@pytest.mark.parametrize("start,end,expected", [
    (3, None, [3]),
    (4, None, []),
    (4, 5, []),
    (2, 5, [2, 3]),
    (1, 2, [1, 2]),
    (1, None, [1, 2, 3]),
])
def test_page_bounds(start, end, expected):
    scraper = make_scraper()
    assert [page for page, _ in scraper.iter_pages(start, end)] == expected
    assert scraper.session.get.call_count == max(1, len(expected))


def test_missing_page_count_retains_single_page_fallback():
    scraper = make_scraper()
    scraper.session.get.return_value.content = b"no pagination"
    assert [page for page, _ in scraper.iter_pages(2)] == [2]
