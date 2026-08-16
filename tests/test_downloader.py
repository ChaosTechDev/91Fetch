from viewkey_batch.downloader import MIN_MEDIA_BYTES, validate_media_file


def test_rejects_tiny_mp4_shell(tmp_path):
    path = tmp_path / "empty.mp4"
    path.write_bytes(b"\x00\x00\x00 ftypisom\x00\x00\x00\x00mdat")

    valid, reason = validate_media_file(path)

    assert not valid
    assert "CDN 空响应" in reason


def test_accepts_sized_mp4_with_valid_header(tmp_path):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"\x00\x00\x00 ftypisom" + b"\x00" * MIN_MEDIA_BYTES)

    valid, reason = validate_media_file(path)

    assert valid
    assert reason == ""
