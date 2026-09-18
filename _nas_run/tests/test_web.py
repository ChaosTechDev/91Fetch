from fastapi.testclient import TestClient

from viewkey_batch.web import AppSettings, app, downloaded_keys, next_inventory_schedule, next_schedule, pagination_info
import viewkey_batch.web as web
from viewkey_batch.models import VideoItem
from datetime import datetime


client = TestClient(app)


def test_web_index_loads():
    response = client.get("/")
    assert response.status_code == 200
    assert "ViewKey Batch" in response.text
    assert "下载所选" in response.text
    assert "no-store" in response.headers["cache-control"]
    assert "app.js?v=20" in response.text
    assert 'data-filter="pending"' in response.text
    assert 'data-filter="all"' not in response.text


def test_download_manager_defaults_to_active_tasks():
    script = (web.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 'downloadFilter: "pending"' in script
    assert '["queued", "downloading"].includes(item.state)' in script


def test_web_config_exposes_categories():
    response = client.get("/api/config")
    assert response.status_code == 200
    assert {"latest", "hot", "featured"}.issubset(response.json()["categories"])


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


def test_daily_and_interval_schedule_calculation():
    now = datetime(2026, 8, 15, 12, 0)
    daily = AppSettings(schedule_mode="daily", daily_time="13:30")
    interval = AppSettings(schedule_mode="interval", interval_minutes=45)
    assert next_schedule(daily, now) == datetime(2026, 8, 15, 13, 30)
    assert next_schedule(interval, now) == datetime(2026, 8, 15, 12, 45)


def test_inventory_schedule_calculation_is_independent():
    now = datetime(2026, 8, 15, 23, 30)
    daily = AppSettings(inventory_schedule_mode="daily", inventory_daily_time="04:15")
    interval = AppSettings(inventory_schedule_mode="interval", inventory_interval_minutes=90)
    assert next_inventory_schedule(daily, now) == datetime(2026, 8, 16, 4, 15)
    assert next_inventory_schedule(interval, now) == datetime(2026, 8, 16, 1, 0)


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
    web.missing_file_observations.clear()
    isolated = web.Store()
    isolated.add(VideoItem("https://example.test/watch?viewkey=abc", "abc", title="Demo"))
    isolated.download_status["abc"] = {"state": "completed", "percent": 100, "error": ""}
    monkeypatch.setattr(web, "store", isolated)

    # 防抖设计：连续多次确认文件缺失后才转为失败，避免库存刷新间隙界面来回跳
    payload = {}
    for _ in range(web.MISSING_FILE_CONFIRMATIONS):
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

    # 只移除部分视频时任务继续，剩余条目保持原状态
    assert response.status_code == 200
    assert job.cancelled is False
    assert job.viewkeys == ["def"]
    assert "abc" not in isolated.download_status
    assert isolated.download_status["def"]["state"] == "queued"

    # 全部移除后任务取消并标记失败
    client.post("/api/downloads/remove", json={"viewkeys": ["def"]})
    assert job.cancelled is True
    assert job.status == "failed"
    assert isolated.download_status == {}


def test_downloaded_filter_ignores_tiny_media_shell(tmp_path, monkeypatch):
    video_dir = tmp_path / "videos"
    video_dir.mkdir()
    (video_dir / "broken [bad].mp4").write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 36)
    (video_dir / "valid [good].mp4").write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * (256 * 1024))
    monkeypatch.setattr(web, "get_video_dir", lambda: video_dir)

    assert downloaded_keys() == {"good"}


def test_completed_keys_persist_across_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    first = web.Store()
    first.completed_keys.add("persisted")
    first.save_state()

    restored = web.Store()
    assert restored.completed_keys == {"persisted"}


def test_local_file_stays_detectable_after_download_record_removal(tmp_path, monkeypatch):
    video_dir = tmp_path / "videos"
    video_dir.mkdir()
    media = video_dir / "imported [local-key].mp4"
    media.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * (256 * 1024))
    isolated = web.Store()
    isolated.completed_keys.add("local-key")
    isolated.download_status["local-key"] = {"state": "completed", "percent": 100}
    monkeypatch.setattr(web, "store", isolated)
    monkeypatch.setattr(web, "get_video_dir", lambda: video_dir)

    isolated.download_status.pop("local-key")
    assert web.downloaded_keys() == {"local-key"}


def test_completed_record_without_local_file_is_not_downloaded(tmp_path, monkeypatch):
    video_dir = tmp_path / "videos"
    video_dir.mkdir()
    isolated = web.Store()
    isolated.completed_keys.add("missing-key")
    isolated.download_status["missing-key"] = {"state": "completed", "percent": 100}
    monkeypatch.setattr(web, "store", isolated)
    monkeypatch.setattr(web, "get_video_dir", lambda: video_dir)

    assert web.downloaded_keys() == set()


def test_scheduled_download_filters_global_local_inventory(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(web, "store", web.Store())
    settings = AppSettings(auto_download_enabled=True, filter_downloaded=True, categories=["latest"])
    monkeypatch.setattr(web.settings_store, "snapshot", lambda: settings)
    config = web.SiteConfig(
        base_url="https://example.test",
        category_urls={"latest": "/v.php?category=mr"},
        author_url="/author/{author}",
    )
    monkeypatch.setattr(web.SiteConfig, "load", classmethod(lambda cls, path: config))

    class Response:
        text = "listing"
        url = "https://example.test/v.php?category=mr"

        @staticmethod
        def raise_for_status():
            return None

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        @staticmethod
        def get(url):
            return Response()

    existing = VideoItem("https://example.test/watch?viewkey=old", "old", title="Old")
    new = VideoItem("https://example.test/watch?viewkey=new", "new", title="New")
    monkeypatch.setattr(web, "build_client", lambda config, cookies: Client())
    monkeypatch.setattr(web, "parse_listing", lambda html, url, config: [existing, new])
    monkeypatch.setattr(web, "downloaded_keys", lambda: {"old"})
    queued: list[str] = []

    def fake_queue(keys, workers, fragments):
        queued.extend(keys)
        return web.Job(id="test", kind="download", total=len(keys))

    monkeypatch.setattr(web, "queue_download", fake_queue)
    message = web.scheduled_refresh()

    assert queued == ["new"]
    assert "1 个下载任务" in message


def test_manual_download_can_queue_while_another_batch_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    isolated = web.Store()
    isolated.videos["running"] = VideoItem("https://example.test/watch?viewkey=running", "running")
    isolated.videos["next"] = VideoItem("https://example.test/watch?viewkey=next", "next")
    isolated.jobs["active"] = web.Job(id="active", kind="download", status="running", viewkeys=["running"])
    isolated.download_status["running"] = {"state": "downloading", "percent": 10}
    monkeypatch.setattr(web, "store", isolated)

    started: list[tuple] = []

    class DeferredThread:
        def __init__(self, target, args, daemon):
            started.append((target, args, daemon))

        def start(self):
            return None

    monkeypatch.setattr(web, "Thread", DeferredThread)
    job = web.queue_download(["running", "next"], workers=2, fragments=4)

    assert job.status == "queued"
    assert job.viewkeys == ["next"]
    assert isolated.download_status["next"]["state"] == "queued"
    assert started[0][0] is web.queued_download_worker


def test_valid_local_file_recovers_stale_failed_status(tmp_path, monkeypatch):
    video_dir = tmp_path / "videos"
    video_dir.mkdir()
    media = video_dir / "Recovered [recover-key].mp4"
    media.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * (256 * 1024))
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(web, "INVENTORY_PATH", tmp_path / "inventory.sqlite3")
    monkeypatch.setattr(web, "get_video_dir", lambda: video_dir)
    isolated = web.Store()
    isolated.videos["recover-key"] = VideoItem(
        "https://example.test/watch?viewkey=recover-key", "recover-key", title="Recovered"
    )
    isolated.download_status["recover-key"] = {
        "state": "failed", "percent": 0, "error": "本地文件不存在，点击重新下载"
    }
    monkeypatch.setattr(web, "store", isolated)
    monkeypatch.setattr(web, "inventory", web.LocalInventory())

    payload = web.downloads()

    assert payload["downloads"][0]["state"] == "completed"
    assert payload["downloads"][0]["error"] == ""
    assert isolated.download_status["recover-key"]["state"] == "completed"


def test_interrupted_downloads_are_requeued_after_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    isolated = web.Store()
    isolated.videos["resume-key"] = VideoItem(
        "https://example.test/watch?viewkey=resume-key", "resume-key", title="Resume"
    )
    isolated.download_status["resume-key"] = {
        "state": "failed", "percent": 35, "error": "上次运行被中断，点击重试可继续下载"
    }
    monkeypatch.setattr(web, "store", isolated)
    monkeypatch.setattr(web.inventory, "ensure_ready", lambda: None)
    monkeypatch.setattr(web, "downloaded_keys", lambda: set())
    queued: list[str] = []
    monkeypatch.setattr(web, "queue_download", lambda keys, workers, fragments: queued.extend(keys))

    web.resume_interrupted_downloads()

    assert queued == ["resume-key"]


def test_catalog_prune_evicts_oldest_and_keeps_active(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "CATALOG_PATH", tmp_path / "catalog.jsonl")
    monkeypatch.setattr(web, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(web, "CATALOG_MAX_ENTRIES", 3)
    isolated = web.Store()
    for key in ("old1", "old2", "old3", "old4"):
        isolated.add(VideoItem(f"https://example.test/watch?viewkey={key}", key, title=key))
    isolated.download_status["old4"] = {"state": "downloading", "percent": 30}
    isolated.completed_keys.add("old2")
    isolated.dismissed_downloads.add("old3")

    isolated.save()
    isolated.save_state()

    assert len(isolated.videos) == 3
    # 最旧的 old1 被淘汰；old4 进行中受保护，old2/old3 状态字段一并清理
    assert "old1" not in isolated.videos
    assert "old1" not in isolated.download_status
    # old3 未被淘汰，dismissed 标记保留
    assert isolated.dismissed_downloads == {"old3"}
    restored = web.Store()
    assert set(restored.videos) == {"old2", "old3", "old4"}
    # 重启加载后进行中任务按设计标记为中断，等待重新入队
    assert restored.download_status["old4"]["state"] == "failed"
