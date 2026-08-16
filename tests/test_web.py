from fastapi.testclient import TestClient

from viewkey_batch.web import app, downloaded_keys, pagination_info
import viewkey_batch.web as web
from viewkey_batch.models import VideoItem


client = TestClient(app)


def test_web_index_loads():
    response = client.get("/")
    assert response.status_code == 200
    assert "91Fetch" in response.text
    assert "下载所选" in response.text
    assert "no-store" in response.headers["cache-control"]
    assert "app.js?v=13" in response.text


def test_web_config_exposes_categories():
    response = client.get("/api/config")
    assert response.status_code == 200
    assert {"top_day", "latest", "hot", "featured"}.issubset(response.json()["categories"])


def test_download_manager_loads():
    response = client.get("/api/downloads")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload["counts"]) == {"queued", "downloading", "completed", "failed"}


def test_pagination_info_reads_site_page_links():
    html = '<a href="/v.php?page=2&category=hot">2</a><a href="/v.php?page=3">下一页</a>'
    assert pagination_info(html, 2) == {
        "page": 2,
        "has_previous": True,
        "has_next": True,
        "last_visible_page": 3,
    }


def test_download_state_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    first = web.Store()
    first.download_status["abc"] = {"state": "downloading", "percent": 42}
    first.dismissed_downloads.add("hidden")
    first.save_state()

    restored = web.Store()
    assert restored.download_status["abc"]["state"] == "failed"
    assert restored.download_status["abc"]["percent"] == 42
    assert "中断" in restored.download_status["abc"]["error"]
    assert restored.dismissed_downloads == {"hidden"}


def test_store_merges_category_membership(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    catalog = web.Store()
    latest = VideoItem("https://example.test/watch?viewkey=abc", "abc", title="Demo")
    latest.add_source("latest", "https://example.test/v.php?next=watch")
    hot = VideoItem("https://example.test/watch?viewkey=abc", "abc", thumbnail_url="thumb.jpg")
    hot.add_source("hot", "https://example.test/v.php?category=hot")

    catalog.add(latest)
    catalog.add(hot)

    merged = catalog.videos["abc"]
    assert merged.sources == ["latest", "hot"]
    assert merged.listing_urls == {
        "latest": "https://example.test/v.php?next=watch",
        "hot": "https://example.test/v.php?category=hot",
    }
    assert merged.title == "Demo"
    assert merged.thumbnail_url == "thumb.jpg"


def test_old_catalog_source_is_migrated(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    old = VideoItem("https://example.test/watch?viewkey=abc", "abc", source="latest", listing_url="/latest")
    raw = old.to_json().replace(', "sources": ["latest"], "listing_urls": {"latest": "/latest"}', "")
    (tmp_path / "catalog.jsonl").write_text(raw + "\n", encoding="utf-8")

    restored = web.Store()
    assert restored.videos["abc"].sources == ["latest"]
    assert restored.videos["abc"].listing_urls == {"latest": "/latest"}


def test_replacing_category_snapshot_removes_only_stale_membership(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    catalog = web.Store()
    shared = VideoItem("u1", "shared")
    shared.add_source("latest", "/latest")
    shared.add_source("hot", "/hot")
    stale = VideoItem("u2", "stale")
    stale.add_source("hot", "/hot")
    catalog.add(shared)
    catalog.add(stale)

    catalog.replace_source_membership("hot", {"shared"})

    assert catalog.videos["shared"].sources == ["latest", "hot"]
    assert "stale" not in catalog.videos


def test_missing_completed_file_becomes_retryable(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(web, "VIDEO_DIR", tmp_path / "videos")
    isolated = web.Store()
    isolated.add(VideoItem("https://example.test/watch?viewkey=abc", "abc", title="Demo"))
    isolated.download_status["abc"] = {"state": "completed", "percent": 100, "error": ""}
    monkeypatch.setattr(web, "store", isolated)

    payload = client.get("/api/downloads").json()

    assert payload["downloads"][0]["state"] == "failed"
    assert "文件不存在" in payload["downloads"][0]["error"]
    assert isolated.download_status["abc"]["state"] == "failed"


def test_removed_download_stays_hidden(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(web, "VIDEO_DIR", tmp_path / "videos")
    isolated = web.Store()
    isolated.add(VideoItem("https://example.test/watch?viewkey=abc", "abc", title="Demo"))
    isolated.download_status["abc"] = {"state": "failed", "percent": 0, "error": "network"}
    monkeypatch.setattr(web, "store", isolated)

    response = client.post("/api/downloads/remove", json={"viewkeys": ["abc"]})

    assert response.status_code == 200
    assert client.get("/api/downloads").json()["downloads"] == []
    restored = web.Store()
    assert restored.dismissed_downloads == {"abc"}


def test_removing_download_cancels_active_job(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    isolated = web.Store()
    isolated.download_status.update({"abc": {"state": "downloading", "percent": 12}, "def": {"state": "queued", "percent": 0}})
    job = web.Job(id="job1", kind="download", status="running", viewkeys=["abc", "def"])
    isolated.jobs[job.id] = job
    monkeypatch.setattr(web, "store", isolated)

    response = client.post("/api/downloads/remove", json={"viewkeys": ["abc"]})

    assert response.status_code == 200
    assert job.cancelled is True
    assert job.status == "failed"
    assert "abc" not in isolated.download_status
    assert isolated.download_status["def"]["state"] == "failed"


def test_downloaded_filter_ignores_tiny_media_shell(tmp_path, monkeypatch):
    video_dir = tmp_path / "videos"
    video_dir.mkdir()
    (video_dir / "broken [bad].mp4").write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 36)
    (video_dir / "valid [good].mp4").write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * (256 * 1024))
    monkeypatch.setattr(web, "get_video_dir", lambda: video_dir)

    assert downloaded_keys() == {"good"}
