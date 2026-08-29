from viewkey_batch.cli import read_manifest
from viewkey_batch.models import VideoItem


def test_read_manifest_skips_bad_lines(tmp_path):
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        VideoItem("u1", "abc", title="Good").to_json()
        + "\nnot-json\n"
        + '{"page_url": "u2"}\n',  # 缺少必需字段
        encoding="utf-8",
    )

    items = read_manifest(manifest)

    assert [item.viewkey for item in items] == ["abc"]
