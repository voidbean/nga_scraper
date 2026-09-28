import json

import pytest

from nga_scraper.storage import _format_content_images, export_markdown, get_data_paths


PATH = "mon_202609/28/jcQ63-l2z5K1eT3cSsg-cs.jpg"
IMAGE = f"![图片](https://img.nga.cn/attachments/{PATH})"


def test_image_touching_chinese_text():
    assert _format_content_images(f"对谁告白水友混进去了是吗./{PATH}") == (
        f"对谁告白水友混进去了是吗\n\n{IMAGE}"
    )


@pytest.mark.parametrize("extension", ["jpg", "jpeg", "png", "gif", "webp", "bmp", "JPG"])
def test_image_extensions(extension):
    path = f"mon_202609/28/example.{extension}"
    assert _format_content_images(f"./{path}") == (
        f"![图片](https://img.nga.cn/attachments/{path})"
    )


def test_adjacent_images_and_following_text():
    result = _format_content_images(f"前文./{PATH}./{PATH}后文")
    assert result.count(IMAGE) == 2
    assert result.startswith(f"前文\n\n{IMAGE}")
    assert result.endswith(f"{IMAGE}\n\n后文")
    assert _format_content_images(result) == result


@pytest.mark.parametrize("text", [
    "普通文字\n\n保持原样\n",
    "./mon_202609/28/file.zip",
    "./mon_202609/28/file.jpg.txt",
    "./mon_202609/28/file.jpg_backup",
    "./other/example.jpg",
    f"https://img.nga.cn/attachments/{PATH}",
    f"https://example.com/./{PATH}",
    IMAGE,
])
def test_unrelated_text_and_existing_markdown_unchanged(text):
    assert _format_content_images(text) == text


@pytest.mark.parametrize("author_id", [None, 42])
def test_export_formats_body_and_quotes_without_changing_jsonl(tmp_path, monkeypatch, author_id):
    monkeypatch.chdir(tmp_path)
    data_dir, posts_file, _, markdown_file = get_data_paths(123, author_id)
    data_dir.mkdir(parents=True)
    posts = [
        {"post_id": 2, "floor": 2, "content": "普通正文"},
        {
            "post_id": 1,
            "floor": 1,
            "content": f"正文./{PATH}",
            "quoted_posts": [{"quoted_user": "引用作者", "quoted_content": f"引用./{PATH}"}],
        },
    ]
    original = "".join(json.dumps(p, ensure_ascii=False) + "\n" for p in posts)
    posts_file.write_text(original, encoding="utf-8")

    assert export_markdown(123, author_id) == 2
    markdown = markdown_file.read_text(encoding="utf-8")
    assert f"正文\n\n{IMAGE}" in markdown
    assert f"> 引用\n>\n> {IMAGE}" in markdown
    assert markdown.index("[1楼]") < markdown.index("[2楼]")
    assert "普通正文" in markdown
    assert posts_file.read_text(encoding="utf-8") == original
