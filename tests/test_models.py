import json

import pytest

from viewkey_batch.models import SiteConfig, VideoItem


def test_filename_truncates_long_titles():
    item = VideoItem("https://example.test/watch?viewkey=abc", "abc", title="长" * 500)
    name = item.filename
    assert len(name) < 120
    assert name.endswith("[abc]")


def test_filename_avoids_windows_reserved_names():
    item = VideoItem("https://example.test/watch?viewkey=abc", "abc", title="CON")
    assert item.filename.startswith("_CON ")


def test_from_json_ignores_unknown_fields():
    raw = VideoItem("u1", "abc").to_json()
    polluted = json.loads(raw)
    polluted["future_field"] = "whatever"
    item = VideoItem.from_json(json.dumps(polluted))
    assert item.viewkey == "abc"


def test_from_json_rejects_invalid_line():
    with pytest.raises(json.JSONDecodeError):
        VideoItem.from_json("not-json")


def test_listing_page_round_trips():
    item = VideoItem("u1", "abc", listing_page=3)
    restored = VideoItem.from_json(item.to_json())
    assert restored.listing_page == 3


def test_site_config_load_ignores_unknown_keys(tmp_path):
    config_file = tmp_path / "site.json"
    config_file.write_text(
        json.dumps({
            "base_url": "https://example.test",
            "category_urls": {"latest": "/latest"},
            "author_url": "/author/{author}",
            "next_page_selector": "a.next",
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    config = SiteConfig.load(config_file)
    assert config.base_url == "https://example.test"
    assert not hasattr(config, "next_page_selector")
