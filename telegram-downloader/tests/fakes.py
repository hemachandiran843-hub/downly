"""Test doubles shared by the suite.

Nothing here touches the network: HTTP responses, Telegram API calls and
yt-dlp itself are all simulated.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from bot.downloader.types import DownloadMethod, DownloadResult, MediaKind, ProbeResult

# --------------------------------------------------------------------- settings
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
MP4_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"\x01" * 256
PDF_BYTES = b"%PDF-1.7\n" + b"x" * 128
ZIP_BYTES = b"PK\x03\x04" + b"\x00" * 32


def make_settings(tmp_path: Path, **overrides: Any):
    """Build Settings from an explicit environment (no .env, no token needed)."""
    from bot.config import Settings

    env: dict[str, str] = {
        "BOT_TOKEN": "123456:TEST-TOKEN-abcdefghijklmnopqrstuvwxyz",
        "DATABASE_PATH": str(tmp_path / "data" / "bot.db"),
        "DATA_DIR": str(tmp_path / "data"),
        "TEMP_DIR": str(tmp_path / "temp"),
        "DOWNLOADS_DIR": str(tmp_path / "downloads"),
        "MIN_FREE_DISK_MB": "0",
        "PROGRESS_UPDATE_INTERVAL_SECONDS": "0.5",
        "LOG_LEVEL": "CRITICAL",
    }
    for key, value in overrides.items():
        env[key.upper()] = str(value)
    return Settings.from_env(env=env, load_dotenv_file=False, require_token=False)


# ------------------------------------------------------------------------ HTTP
class IncompleteReadError(OSError):
    """Raised by the fake content stream to simulate a truncated transfer."""


class FakeContent:
    def __init__(self, chunks: Iterable[bytes], *, fail_after: int | None = None, delay: float = 0.0) -> None:
        self._chunks = list(chunks)
        self._fail_after = fail_after
        self._delay = delay

    async def iter_chunked(self, size: int):
        for index, chunk in enumerate(self._chunks):
            if self._fail_after is not None and index >= self._fail_after:
                raise IncompleteReadError("connection closed mid-stream")
            if self._delay:
                await asyncio.sleep(self._delay)
            yield chunk[:size] if size else chunk
        if self._fail_after is not None and self._fail_after >= len(self._chunks):
            raise IncompleteReadError("connection closed before the end")


class FakeResponse:
    def __init__(
        self,
        status: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
        *,
        chunk_size: int = 8192,
        fail_after: int | None = None,
        delay: float = 0.0,
    ) -> None:
        self.status = status
        self.headers = dict(headers or {})
        self.closed = False
        self.body = body
        chunks = [body[i : i + chunk_size] for i in range(0, len(body), chunk_size)] or [b""]
        self.content = FakeContent(chunks, fail_after=fail_after, delay=delay)

    def close(self) -> None:
        self.closed = True

    def release(self) -> None:
        self.closed = True


class FakeSession:
    """Minimal ``aiohttp.ClientSession`` stand-in with scripted responses."""

    def __init__(self, responses: dict[Any, Any] | None = None, default: Any = None) -> None:
        self.responses = dict(responses or {})
        self.default = default
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    def add(self, url: str, response: Any, method: str | None = None) -> None:
        if method:
            self.responses[(method.upper(), url)] = response
        else:
            self.responses[url] = response

    async def request(self, method: str, url: str, **_kwargs: Any) -> FakeResponse:
        self.calls.append((method.upper(), url))
        key = (method.upper(), url)
        if key in self.responses:
            response = self.responses[key]
        elif url in self.responses:
            response = self.responses[url]
        elif self.default is not None:
            response = self.default
        else:
            raise AssertionError(f"unexpected request: {method} {url}")
        if callable(response):
            response = response(self, method.upper(), url)
        if isinstance(response, list):  # scripted sequence, one per call
            response = response.pop(0)
        return response

    async def close(self) -> None:
        self.closed = True


def fake_response(url: str, body: bytes = b"", **kwargs: Any) -> FakeResponse:
    headers = dict(kwargs.pop("headers", {}))
    return FakeResponse(body=body, headers=headers, **kwargs)


# ------------------------------------------------------------------ Telegram bot
@dataclass
class FakeMessage:
    message_id: int
    chat_id: int = 1
    text: str = ""


class FakeBot:
    """Records every Telegram call instead of talking to the API."""

    def __init__(self, *, fail_document: bool = False) -> None:
        self.messages: list[tuple[int, str]] = []
        self.edits: list[tuple[int, int, str]] = []
        self.sent_files: list[tuple[str, str]] = []
        self.answers: list[tuple[str, bool]] = []
        self._next_id = 100
        self.fail_document = fail_document
        self.fail_video = False

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> FakeMessage:
        self._next_id += 1
        self.messages.append((chat_id, text))
        return FakeMessage(message_id=self._next_id, chat_id=chat_id, text=text)

    async def edit_message_text(self, chat_id: int, message_id: int, text: str, **kwargs: Any) -> None:
        self.edits.append((chat_id, message_id, text))

    async def edit_message_reply_markup(self, **kwargs: Any) -> None:
        return None

    async def set_my_commands(self, commands: Any) -> None:
        return None

    async def answer_callback_query(self, *args: Any, **kwargs: Any) -> None:
        self.answers.append((str(kwargs.get("text", "")), bool(kwargs.get("show_alert", False))))

    def _record(self, kind: str, chat_id: int, media: Any, **kwargs: Any) -> FakeMessage:
        filename = getattr(media, "filename", None) or str(media)
        self.sent_files.append((kind, filename))
        self._next_id += 1
        return FakeMessage(message_id=self._next_id, chat_id=chat_id)

    async def send_document(self, chat_id: int, document: Any, **kwargs: Any) -> FakeMessage:
        if self.fail_document:
            from telegram.error import BadRequest

            raise BadRequest("File must be non-empty")
        return self._record("document", chat_id, document, **kwargs)

    async def send_video(self, chat_id: int, video: Any, **kwargs: Any) -> FakeMessage:
        if self.fail_video:
            from telegram.error import BadRequest

            raise BadRequest("Video failed")
        return self._record("video", chat_id, video, **kwargs)

    async def send_audio(self, chat_id: int, audio: Any, **kwargs: Any) -> FakeMessage:
        return self._record("audio", chat_id, audio, **kwargs)

    async def send_photo(self, chat_id: int, photo: Any, **kwargs: Any) -> FakeMessage:
        return self._record("photo", chat_id, photo, **kwargs)

    async def send_animation(self, chat_id: int, animation: Any, **kwargs: Any) -> FakeMessage:
        return self._record("animation", chat_id, animation, **kwargs)

    async def send_message_to_all(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        return None


# ---------------------------------------------------------------- downloaders
@dataclass
class StubDirect:
    """Duck-typed replacement for DirectDownloader."""

    body: bytes = MP4_BYTES
    filename: str = "clip.mp4"
    kind: MediaKind = MediaKind.VIDEO
    content_type: str = "video/mp4"
    size: int | None = None
    probe_error: Exception | None = None
    download_error: Exception | None = None
    progress_steps: int = 3
    step_delay: float = 0.0
    respect_cancel: bool = True
    downloaded_size: int | None = None
    probe_calls: int = field(default=0, init=False)
    download_calls: int = field(default=0, init=False)

    async def probe(self, url: str, *, method: str = "HEAD") -> ProbeResult:
        self.probe_calls += 1
        if self.probe_error is not None:
            raise self.probe_error
        size = self.size if self.size is not None else len(self.body)
        return ProbeResult(
            url=url,
            final_url=url,
            status=200,
            content_type=self.content_type,
            content_length=size,
            kind=self.kind,
            filename=self.filename,
        )

    async def download(
        self,
        url: str,
        dest_dir: Any,
        *,
        on_progress=None,
        cancel_event=None,
        probe: ProbeResult | None = None,
        title: str | None = None,
    ) -> DownloadResult:
        self.download_calls += 1
        if self.download_error is not None:
            raise self.download_error
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / self.filename
        path.write_bytes(self.body)
        total = len(self.body)
        for step in range(1, self.progress_steps + 1):
            if cancel_event is not None and self.respect_cancel and cancel_event.is_set():
                from bot.exceptions import DownloadCancelled

                raise DownloadCancelled("cancelled by user")
            await asyncio.sleep(self.step_delay)
            if on_progress is not None:
                on_progress(int(total * step / self.progress_steps), total, 1024.0)
        size = self.downloaded_size if self.downloaded_size is not None else path.stat().st_size
        return DownloadResult(
            path=str(path),
            filename=self.filename,
            size_bytes=size,
            kind=self.kind,
            method=DownloadMethod.DIRECT,
            source_url=url,
            content_type=self.content_type,
            title=title or self.filename,
        )

    async def close(self) -> None:
        return None


@dataclass
class StubYtDlp:
    """Duck-typed replacement for YtDlpDownloader."""

    info: dict[str, Any] = field(default_factory=dict)
    download_error: Exception | None = None
    body: bytes = MP4_BYTES
    filename: str = "yt.mp4"
    kind: MediaKind = MediaKind.VIDEO
    probe_calls: int = field(default=0, init=False)

    async def probe(self, url: str) -> dict[str, Any] | None:
        self.probe_calls += 1
        return dict(self.info) if self.info else None

    async def download(self, url: str, dest_dir: Any, *, on_progress=None, cancel_event=None, on_stage=None):
        if self.download_error is not None:
            raise self.download_error
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / self.filename
        path.write_bytes(self.body)
        if on_progress is not None:
            on_progress(len(self.body), len(self.body), 2048.0)
        if on_stage is not None:
            on_stage("processing")
        return DownloadResult(
            path=str(path),
            filename=self.filename,
            size_bytes=len(self.body),
            kind=self.kind,
            method=DownloadMethod.YTDLP,
            source_url=url,
            title=self.info.get("title") or self.filename,
            duration=12.0,
        )


def make_fake_yt_dlp(
    *,
    behavior: str = "ok",
    info: dict[str, Any] | None = None,
    output_name: str = "video.mp4",
    report_total: int | None = None,
):
    """Build a fake ``yt_dlp`` module for monkeypatching ``_import_yt_dlp``."""
    meta = {
        "title": "Fake title",
        "id": "abc123",
        "ext": "mp4",
        "duration": 12.5,
        "width": 640,
        "height": 360,
        "extractor_key": "FakeExtractor",
    }
    meta.update(info or {})
    state: dict[str, Any] = {"options": None, "calls": []}

    class DownloadError(Exception):
        pass

    class FakeYDL:
        def __init__(self, options: dict[str, Any]) -> None:
            self.options = options
            state["options"] = options
            self.behavior = behavior

        def __enter__(self):
            return self

        def __exit__(self, *exc_info) -> bool:
            return False

        def extract_info(self, url: str, download: bool = True):
            state["calls"].append((url, download))
            if self.behavior == "unsupported":
                raise DownloadError("ERROR: Unsupported URL: " + url)
            if self.behavior == "private":
                raise DownloadError(
                    "ERROR: Sign in to confirm your age. This video may be inappropriate for some users."
                )
            if self.behavior == "boom":
                raise RuntimeError("unexpected internal failure")
            if not download:
                return dict(meta)
            target = Path(self.options["paths"]["home"]) / output_name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(MP4_BYTES)
            if self.behavior == "cancel":
                hooks = self.options.get("progress_hooks") or []
                for hook in hooks:
                    hook({"status": "downloading", "downloaded_bytes": 10, "total_bytes": 100, "speed": 100})
            else:
                total = report_total or len(MP4_BYTES)
                for hook in self.options.get("progress_hooks") or []:
                    hook(
                        {
                            "status": "downloading",
                            "downloaded_bytes": total // 2,
                            "total_bytes": total,
                            "speed": 512.0,
                        }
                    )
                    hook({"status": "finished", "total_bytes": total})
                for hook in self.options.get("postprocessor_hooks") or []:
                    hook({"status": "started", "postprocessor": "MoveFiles"})
            return {**meta, "requested_downloads": [{"filepath": str(target)}]}

        def prepare_filename(self, info: dict[str, Any]) -> str:
            return str(Path(self.options["paths"]["home"]) / output_name)

    module = SimpleNamespace(YoutubeDL=FakeYDL, utils=SimpleNamespace(DownloadError=DownloadError))
    return module, state, DownloadError
