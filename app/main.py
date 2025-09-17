from __future__ import annotations

import importlib
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal, ParamSpec, Protocol, Self, TypeVar, cast
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse

app = FastAPI()

P = ParamSpec("P")
R = TypeVar("R")
fastapi_get = cast(
    Callable[..., Callable[[Callable[P, R]], Callable[P, R]]],
    app.get,
)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
INDEX_TEMPLATE = TEMPLATES_DIR / "index.html"


@lru_cache(maxsize=1)
def load_index_template() -> str:
    try:
        return INDEX_TEMPLATE.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail="Index template is unavailable") from exc


FormatLiteral = Literal["video", "mp3"]
UrlParam = Annotated[str, Query(..., description="Direct YouTube video URL")]
FormatParam = Annotated[FormatLiteral, Query(pattern="^(video|mp3)$")]

INVALID_FILENAME_CHARS = set('<>:"/\\|?*')
STREAM_CHUNK_SIZE = 65536
StreamInfo = dict[str, Any]

VIDEO_FORMAT_SELECTOR = "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]/best"
VIDEO_FALLBACK_FORMAT = "best[ext=mp4]/best"
AUDIO_FORMAT_SELECTOR = "bestaudio/best"

YDL_BASE_OPTIONS: dict[str, Any] = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "noplaylist": True,
    "consoletitle": False,
    "cachedir": False,
}


class YoutubeDLProtocol(Protocol):
    def __enter__(self: Self) -> Self: ...

    def __exit__(self: Self, exc_type: Any, exc: Any, traceback: Any) -> bool | None: ...

    def extract_info(self: Self, url: str, download: bool = ...) -> StreamInfo: ...


class YtDlpModule(Protocol):
    DownloadError: type[Exception]

    def YoutubeDL(self: Self, params: dict[str, Any] | None = None) -> YoutubeDLProtocol: ...


yt_dlp = cast(YtDlpModule, importlib.import_module("yt_dlp"))


@lru_cache(maxsize=1)
def resolve_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path is None:
        raise HTTPException(status_code=500, detail="ffmpeg is not available")
    return path


def create_ydl(format_selector: str | None = None) -> YoutubeDLProtocol:
    options = dict(YDL_BASE_OPTIONS)
    if format_selector is not None:
        options["format"] = format_selector
    return yt_dlp.YoutubeDL(options)


def fetch_video_title(target_url: str) -> str:
    try:
        with create_ydl(None) as ydl:
            info = ydl.extract_info(target_url, download=False)
    except yt_dlp.DownloadError as exc:
        detail = str(exc).strip() or "Unable to retrieve video title"
        raise HTTPException(status_code=400, detail=detail) from exc
    except Exception as exc:  # pragma: no cover - defensive against unexpected failures
        raise HTTPException(status_code=500, detail="Failed to retrieve video metadata") from exc

    title = cast(str | None, info.get("title"))
    if not title:
        raise HTTPException(status_code=500, detail="Video title is unavailable")
    return title


def sanitize_filename(title: str, extension: str) -> str:
    cleaned = []
    for char in title:
        code_point = ord(char)
        if code_point < 32 or char in INVALID_FILENAME_CHARS:
            cleaned.append("_")
            continue
        cleaned.append(char)

    sanitized = "".join(cleaned)
    sanitized = re.sub(r"\s+", " ", sanitized).strip()
    sanitized = sanitized.strip("._")
    sanitized = sanitized.replace(" ", "_")

    if not sanitized:
        sanitized = "download"

    max_length = 150
    sanitized = sanitized[:max_length]

    return f"{sanitized}.{extension}"


@fastapi_get("/", response_class=HTMLResponse)
async def index() -> str:
    return load_index_template()


def _select_single_stream(info: StreamInfo) -> StreamInfo | None:
    raw_candidates = info.get("requested_downloads") or info.get("requested_formats")
    if isinstance(raw_candidates, list) and raw_candidates:
        first = raw_candidates[0]
        if len(raw_candidates) == 1 and isinstance(first, dict):
            return cast(StreamInfo, first)
        return None
    return info if isinstance(info.get("url"), str) else None


def _normalize_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    if not headers:
        return {}
    return {str(key): str(value) for key, value in headers.items()}


def _ensure_http_scheme(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="Unsupported media URL scheme")


def _iter_http_chunks(download_url: str, headers: Mapping[str, str]) -> Iterator[bytes]:
    _ensure_http_scheme(download_url)
    request = urllib.request.Request(download_url, headers=dict(headers))  # noqa: S310 - scheme validated
    try:
        response = urllib.request.urlopen(request)  # noqa: S310 - scheme validated
    except urllib.error.HTTPError as exc:  # pragma: no cover - network dependent
        detail = exc.reason or "Media request failed"
        raise HTTPException(status_code=exc.code, detail=detail) from exc
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=500, detail="Unable to retrieve media stream") from exc

    def iterator() -> Iterator[bytes]:
        with response:
            yield from iter(lambda: response.read(STREAM_CHUNK_SIZE), b"")

    return iterator()


def _transcode_to_mp3(download_url: str, headers: Mapping[str, str]) -> Iterator[bytes]:
    _ensure_http_scheme(download_url)
    request = urllib.request.Request(download_url, headers=dict(headers))  # noqa: S310 - scheme validated
    try:
        source = urllib.request.urlopen(request)  # noqa: S310 - scheme validated
    except urllib.error.HTTPError as exc:  # pragma: no cover - network dependent
        detail = exc.reason or "Audio request failed"
        raise HTTPException(status_code=exc.code, detail=detail) from exc
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=500, detail="Unable to retrieve audio stream") from exc

    ffmpeg = resolve_ffmpeg()

    try:
        process = subprocess.Popen(  # noqa: S603 - command assembled from controlled template
            [
                ffmpeg,
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                "-vn",
                "-f",
                "mp3",
                "-codec:a",
                "libmp3lame",
                "pipe:1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except FileNotFoundError as exc:  # pragma: no cover - handled by resolve_ffmpeg
        source.close()
        raise HTTPException(status_code=500, detail="ffmpeg is not available") from exc

    stdin = process.stdin
    stdout = process.stdout
    stderr = process.stderr

    if stdin is None or stdout is None or stderr is None:
        process.kill()
        source.close()
        raise HTTPException(status_code=500, detail="Unable to start audio encoder")

    def pump() -> None:
        try:
            for chunk in iter(lambda: source.read(STREAM_CHUNK_SIZE), b""):
                if not chunk:
                    break
                try:
                    stdin.write(chunk)
                except BrokenPipeError:  # pragma: no cover - depends on ffmpeg behaviour
                    break
        finally:
            try:
                stdin.close()
            finally:
                source.close()

    feeder = threading.Thread(target=pump, daemon=True)
    feeder.start()

    def iterator() -> Iterator[bytes]:
        try:
            yield from iter(lambda: stdout.read(STREAM_CHUNK_SIZE), b"")
            feeder.join()
            returncode = process.wait()
            if returncode:
                error_text = stderr.read().decode("utf-8", errors="ignore").strip()
                detail = error_text or f"ffmpeg exited with code {returncode}"
                raise RuntimeError(detail)
        finally:
            stdout.close()
            stderr.close()
            if process.poll() is None:  # pragma: no cover - defensive shutdown
                process.kill()
                process.wait()

    return iterator()


def _extract_stream_info(
    target_url: str,
    format_selector: str,
    *,
    fallback_selector: str | None = None,
) -> tuple[StreamInfo, StreamInfo]:
    try:
        with create_ydl(format_selector) as ydl:
            info = ydl.extract_info(target_url, download=False)
    except yt_dlp.DownloadError as exc:
        detail = str(exc).strip() or "Unable to resolve media stream"
        raise HTTPException(status_code=400, detail=detail) from exc
    except Exception as exc:  # pragma: no cover - defensive against unexpected failures
        raise HTTPException(status_code=500, detail="Failed to resolve media stream") from exc

    stream = _select_single_stream(info)
    if stream is None and fallback_selector is not None:
        with create_ydl(fallback_selector) as ydl:
            info = ydl.extract_info(target_url, download=False)
        stream = _select_single_stream(info)

    if stream is None:
        raise HTTPException(status_code=500, detail="Unable to identify a direct media stream")

    return info, stream


def prepare_video_stream(target_url: str) -> tuple[Iterator[bytes], str, str]:
    info, stream = _extract_stream_info(
        target_url,
        VIDEO_FORMAT_SELECTOR,
        fallback_selector=VIDEO_FALLBACK_FORMAT,
    )
    if isinstance(stream.get("http_headers"), Mapping):
        headers_mapping = cast(Mapping[str, Any], stream["http_headers"])
    elif isinstance(info.get("http_headers"), Mapping):
        headers_mapping = cast(Mapping[str, Any], info["http_headers"])
    else:
        headers_mapping = None
    headers = _normalize_headers(headers_mapping)
    download_url = cast(str | None, stream.get("url"))
    if not download_url:
        raise HTTPException(status_code=500, detail="Stream URL is unavailable")
    iterator = _iter_http_chunks(download_url, headers)
    extension = cast(str | None, stream.get("ext") or info.get("ext")) or "mp4"
    media_type = "video/mp4" if extension == "mp4" else f"video/{extension}"
    return iterator, media_type, extension


def prepare_mp3_stream(target_url: str) -> tuple[Iterator[bytes], str, str]:
    info, stream = _extract_stream_info(target_url, AUDIO_FORMAT_SELECTOR)
    if isinstance(stream.get("http_headers"), Mapping):
        headers_mapping = cast(Mapping[str, Any], stream["http_headers"])
    elif isinstance(info.get("http_headers"), Mapping):
        headers_mapping = cast(Mapping[str, Any], info["http_headers"])
    else:
        headers_mapping = None
    headers = _normalize_headers(headers_mapping)
    download_url = cast(str | None, stream.get("url"))
    if not download_url:
        raise HTTPException(status_code=500, detail="Stream URL is unavailable")
    iterator = _transcode_to_mp3(download_url, headers)
    return iterator, "audio/mpeg", "mp3"


def build_stream(target_url: str, output_format: FormatLiteral) -> tuple[Iterator[bytes], str, str]:
    if output_format == "video":
        return prepare_video_stream(target_url)
    if output_format == "mp3":
        return prepare_mp3_stream(target_url)
    raise HTTPException(status_code=400, detail="Unsupported format")


@fastapi_get("/stream")
async def stream(
    url: UrlParam,
    format: FormatParam = "video",
) -> StreamingResponse:
    iterator, media_type, extension = build_stream(url, format)
    try:
        title = fetch_video_title(url)
        filename = sanitize_filename(title, extension)
    except HTTPException:
        filename = f"download.{extension}"

    response = StreamingResponse(iterator, media_type=media_type)
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["Cache-Control"] = "no-store"
    return response
