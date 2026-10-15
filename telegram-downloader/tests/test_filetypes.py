"""File-type detection, filename extraction and disk-space guards."""

from __future__ import annotations

from pathlib import Path

import pytest

from bot.downloader.types import MediaKind
from bot.downloader.utils import (
    available_disk_bytes,
    classify,
    ensure_disk_space,
    ensure_extension,
    extension_from_url,
    filename_from_content_disposition,
    filename_from_url,
    is_executable_name,
    is_probably_html,
    kind_from_content_type,
    kind_from_extension,
    sniff_magic,
)
from bot.exceptions import InsufficientDiskSpaceError
from tests.fakes import MP4_BYTES, PDF_BYTES, PNG_BYTES, ZIP_BYTES


class TestContentType:
    @pytest.mark.parametrize(
        ("content_type", "kind"),
        [
            ("video/mp4", MediaKind.VIDEO),
            ("video/x-matroska; charset=binary", MediaKind.VIDEO),
            ("audio/mpeg", MediaKind.AUDIO),
            ("image/webp", MediaKind.IMAGE),
            ("application/pdf", MediaKind.DOCUMENT),
            ("application/zip", MediaKind.ARCHIVE),
            ("application/octet-stream", MediaKind.UNKNOWN),
            ("text/html; charset=utf-8", MediaKind.HTML),
            ("video/whatever", MediaKind.VIDEO),
            ("audio/whatever", MediaKind.AUDIO),
            ("image/whatever", MediaKind.IMAGE),
            ("application/x-unknown", MediaKind.UNKNOWN),
            ("", MediaKind.UNKNOWN),
            (None, MediaKind.UNKNOWN),
        ],
    )
    def test_kind_from_content_type(self, content_type, kind):
        assert kind_from_content_type(content_type) is kind


class TestMagicBytes:
    @pytest.mark.parametrize(
        ("data", "kind", "extension"),
        [
            (PDF_BYTES, MediaKind.DOCUMENT, "pdf"),
            (PNG_BYTES, MediaKind.IMAGE, "png"),
            (ZIP_BYTES, MediaKind.ARCHIVE, "zip"),
            (MP4_BYTES, MediaKind.VIDEO, "mp4"),
            (b"\xff\xd8\xff\xe0", MediaKind.IMAGE, "jpg"),
            (b"GIF89a....", MediaKind.IMAGE, "gif"),
            (b"ID3\x04\x00", MediaKind.AUDIO, "mp3"),
            (b"\x1aE\xdf\xa3....", MediaKind.VIDEO, "mkv"),
            (b"RIFF\x00\x00\x00\x00WAVEfmt ", MediaKind.AUDIO, "wav"),
            (b"RIFF\x00\x00\x00\x00WEBPVP8 ", MediaKind.IMAGE, "webp"),
            (b"7z\xbc\xaf\x27\x1c", MediaKind.ARCHIVE, "7z"),
            (b"Rar!\x1a\x07\x00", MediaKind.ARCHIVE, "rar"),
            (b"fLaC\x00\x00", MediaKind.AUDIO, "flac"),
        ],
    )
    def test_sniff(self, data, kind, extension):
        assert sniff_magic(data) == (kind, extension)

    def test_unknown(self):
        assert sniff_magic(b"\x01\x02\x03\x04") is None
        assert sniff_magic(b"") is None
        assert sniff_magic(None) is None

    def test_html_detection(self):
        assert is_probably_html(b"<!DOCTYPE html><html>")
        assert is_probably_html(b"  <html><head></head>")
        assert not is_probably_html(PNG_BYTES)
        assert not is_probably_html(b"")


class TestClassify:
    def test_magic_wins_over_lying_extension(self):
        kind, extension = classify(
            content_type="application/octet-stream",
            url="https://example.com/notavideo.txt",
            magic=MP4_BYTES,
        )
        assert kind is MediaKind.VIDEO
        assert extension == "mp4"

    def test_html_without_magic_uses_content_type(self):
        kind, extension = classify(content_type="text/html; charset=utf-8", url="https://example.com/watch")
        assert kind is MediaKind.HTML
        assert extension == "html"

    def test_extension_fallback_when_content_type_unknown(self):
        kind, extension = classify(url="https://example.com/movie.mkv")
        assert kind is MediaKind.VIDEO
        assert extension == "mkv"

    def test_content_type_extension_used(self):
        kind, extension = classify(content_type="application/pdf", url="https://example.com/download?id=5")
        assert kind is MediaKind.DOCUMENT
        assert extension == "pdf"

    def test_sniffed_html(self):
        kind, _ = classify(magic=b"<!DOCTYPE html><html lang='en'>")
        assert kind is MediaKind.HTML


class TestFilenames:
    def test_from_content_disposition_plain(self):
        assert filename_from_content_disposition('attachment; filename="movie.mp4"') == "movie.mp4"

    def test_from_content_disposition_encoded(self):
        header = "attachment; filename*=UTF-8''my%20file%20%C3%A9.mp4"
        assert filename_from_content_disposition(header) == "my file é.mp4"

    def test_from_content_disposition_traversal(self):
        assert filename_from_content_disposition('attachment; filename="../../evil.sh"') == "evil.sh"

    def test_from_content_disposition_missing(self):
        assert filename_from_content_disposition(None) is None
        assert filename_from_content_disposition("inline") is None

    def test_from_url(self):
        assert filename_from_url("https://example.com/a/b/report%20final.pdf") == "report final.pdf"
        assert filename_from_url("https://example.com/") is None
        assert filename_from_url("https://example.com/dir/") is None

    def test_extension_from_url(self):
        assert extension_from_url("https://example.com/a.mp4?x=1") == "mp4"
        assert extension_from_url("https://example.com/download") is None
        assert extension_from_url("https://example.com/a." + "b" * 20) is None

    def test_extension_from_extension_lookup(self):
        assert kind_from_extension("MP4") is MediaKind.VIDEO
        assert kind_from_extension(".pdf") is MediaKind.DOCUMENT
        assert kind_from_extension("nope") is None
        assert kind_from_extension(None) is None

    def test_ensure_extension(self):
        assert ensure_extension("file", "mp4") == "file.mp4"
        assert ensure_extension("file.mp4", "mp4") == "file.mp4"
        assert ensure_extension("file", None) == "file"
        assert ensure_extension("README.rst", "txt") == "README.rst"
        # Even a misleading name is kept: classification uses the content, not the name.
        assert ensure_extension("movie.mp4", "pdf") == "movie.mp4"
        assert ensure_extension("file.", "mp4") == "file..mp4"
        # A lying executable extension is always replaced by the detected one.
        assert ensure_extension("payload.exe", "pdf") == "payload.pdf"
        assert ensure_extension("update.BAT", "zip") == "update.zip"

    def test_is_executable_name(self):
        assert is_executable_name("setup.exe")
        assert is_executable_name("script.PS1")
        assert not is_executable_name("video.mp4")
        assert not is_executable_name("noextension")


class TestDiskSpace:
    def test_available_disk_bytes(self, tmp_path: Path):
        assert available_disk_bytes(tmp_path) > 0

    def test_enough_space_passes(self, tmp_path: Path):
        ensure_disk_space(tmp_path, 1024, min_free_mb=0)

    def test_not_enough_space_raises(self, tmp_path: Path):
        with pytest.raises(InsufficientDiskSpaceError) as excinfo:
            ensure_disk_space(tmp_path, 10**15, min_free_mb=0)
        assert "Not enough disk space" in excinfo.value.user_message

    def test_reserve_is_honoured(self, tmp_path: Path):
        free = available_disk_bytes(tmp_path)
        with pytest.raises(InsufficientDiskSpaceError):
            ensure_disk_space(tmp_path, 0, min_free_mb=free // (1024 * 1024) + 1024)
