"""Shared types for the downloader layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class MediaKind(StrEnum):
    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"
    DOCUMENT = "document"
    ARCHIVE = "archive"
    HTML = "html"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {
            MediaKind.VIDEO: "Video",
            MediaKind.AUDIO: "Audio",
            MediaKind.IMAGE: "Image",
            MediaKind.DOCUMENT: "Document",
            MediaKind.ARCHIVE: "Archive",
            MediaKind.HTML: "Web page",
            MediaKind.UNKNOWN: "File",
        }[self]


class DownloadMethod(StrEnum):
    DIRECT = "direct"
    YTDLP = "ytdlp"


@dataclass(slots=True)
class ProbeResult:
    """What a cheap HTTP inspection of a URL revealed."""

    url: str
    final_url: str
    status: int
    content_type: str = ""
    content_length: int | None = None
    filename: str | None = None
    accepts_ranges: bool = False
    kind: MediaKind = MediaKind.UNKNOWN
    magic_extension: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def is_redirected(self) -> bool:
        return self.final_url != self.url

    @property
    def is_html(self) -> bool:
        return self.kind is MediaKind.HTML


@dataclass(slots=True)
class DownloadResult:
    """A finished download on disk."""

    path: str
    filename: str
    size_bytes: int
    kind: MediaKind
    method: DownloadMethod
    source_url: str
    content_type: str = ""
    title: str | None = None
    duration: float | None = None
    thumbnail: str | None = None
    width: int | None = None
    height: int | None = None
    artist: str | None = None
    extra: dict[str, object] = field(default_factory=dict)

    @property
    def upsert_caption_hint(self) -> str | None:
        return self.title or self.filename
