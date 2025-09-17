from __future__ import annotations

import importlib
from collections.abc import Callable, Iterator
from typing import Any, TypeVar, cast

import pytest
from fastapi import HTTPException

import app.main as main

yt_dlp = cast(Any, importlib.import_module("yt_dlp"))

pytestmark = pytest.mark.anyio

TestFunc = TypeVar("TestFunc", bound=Callable[..., Any])


def parametrize(*args: Any, **kwargs: Any) -> Callable[[TestFunc], TestFunc]:
    return cast(Callable[[TestFunc], TestFunc], pytest.mark.parametrize(*args, **kwargs))


class StubYDL:
    def __init__(
        self,
        info: dict[str, Any] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self._info = info or {}
        self._error = error
        self.calls: list[tuple[str, bool]] = []

    def __enter__(self) -> StubYDL:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def extract_info(self, url: str, download: bool) -> dict[str, Any]:
        self.calls.append((url, download))
        if self._error is not None:
            raise self._error
        return self._info


def test_load_index_template_returns_html() -> None:
    template = main.load_index_template()
    assert "<form" in template
    assert "Dreamy Downloader" in template


def test_fetch_video_title_success(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubYDL({"title": "Example Title"})
    monkeypatch.setattr(main, "create_ydl", lambda _fmt: stub)

    title = main.fetch_video_title("https://example.com")

    assert title == "Example Title"
    assert stub.calls == [("https://example.com", False)]


def test_fetch_video_title_download_error(monkeypatch: pytest.MonkeyPatch) -> None:
    error = yt_dlp.DownloadError("boom")
    stub = StubYDL(error=error)
    monkeypatch.setattr(main, "create_ydl", lambda _fmt: stub)

    with pytest.raises(HTTPException) as excinfo:
        main.fetch_video_title("https://example.com")

    assert excinfo.value.status_code == 400
    assert "boom" in excinfo.value.detail


def test_fetch_video_title_missing_title(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubYDL({})
    monkeypatch.setattr(main, "create_ydl", lambda _fmt: stub)

    with pytest.raises(HTTPException) as excinfo:
        main.fetch_video_title("https://example.com")

    assert excinfo.value.status_code == 500


@parametrize(
    "title,extension,expected",
    [
        ("Video:Title*?", "mp3", "Video_Title.mp3"),
        ("   spaced   name   ", "mp4", "spaced_name.mp4"),
        ("<>\\/:|?*", "mp3", "download.mp3"),
    ],
)
def test_sanitize_filename(title: str, extension: str, expected: str) -> None:
    assert main.sanitize_filename(title, extension) == expected


def test_prepare_video_stream_uses_http_iterator(monkeypatch: pytest.MonkeyPatch) -> None:
    info = {"http_headers": {"X-Test": "info"}}
    stream = {
        "url": "https://media.example/video.mp4",
        "ext": "mp4",
        "http_headers": {"User-Agent": "abc"},
    }

    def fake_extract(
        target_url: str,
        selector: str,
        *,
        fallback_selector: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        assert target_url == "https://example.com/video"
        assert selector == main.VIDEO_FORMAT_SELECTOR
        assert fallback_selector == main.VIDEO_FALLBACK_FORMAT
        return info, stream

    def fake_iter(url: str, headers: dict[str, str]) -> Iterator[bytes]:
        assert url == stream["url"]
        assert headers == stream["http_headers"]

        def generator() -> Iterator[bytes]:
            yield b"chunk"

        return generator()

    monkeypatch.setattr(main, "_extract_stream_info", fake_extract)
    monkeypatch.setattr(main, "_iter_http_chunks", fake_iter)

    iterator, media_type, extension = main.prepare_video_stream("https://example.com/video")

    assert list(iterator) == [b"chunk"]
    assert media_type == "video/mp4"
    assert extension == "mp4"


def test_prepare_mp3_stream_uses_transcoder(monkeypatch: pytest.MonkeyPatch) -> None:
    info = {"http_headers": {"X-Test": "info"}}
    stream = {
        "url": "https://media.example/audio.webm",
        "http_headers": {"User-Agent": "abc"},
    }

    monkeypatch.setattr(
        main,
        "_extract_stream_info",
        lambda target_url, selector, **_: (info, stream),
    )

    def fake_transcode(url: str, headers: dict[str, str]) -> Iterator[bytes]:
        assert url == stream["url"]
        assert headers == stream["http_headers"]

        def generator() -> Iterator[bytes]:
            yield b"audio"

        return generator()

    monkeypatch.setattr(main, "_transcode_to_mp3", fake_transcode)

    iterator, media_type, extension = main.prepare_mp3_stream("https://example.com/audio")

    assert list(iterator) == [b"audio"]
    assert media_type == "audio/mpeg"
    assert extension == "mp3"


def test_extract_stream_info_triggers_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str | None] = []

    def fake_create(format_selector: str | None) -> StubYDL:
        calls.append(format_selector)
        if format_selector == main.VIDEO_FORMAT_SELECTOR:
            # Multiple streams should trigger fallback
            info = {
                "requested_downloads": [
                    {"url": "https://stream/one"},
                    {"url": "https://stream/two"},
                ]
            }
        else:
            info = {"requested_downloads": [{"url": "https://stream/final", "ext": "mp4"}]}
        return StubYDL(info)

    monkeypatch.setattr(main, "create_ydl", fake_create)

    info, stream = main._extract_stream_info(
        "https://example.com",
        main.VIDEO_FORMAT_SELECTOR,
        fallback_selector=main.VIDEO_FALLBACK_FORMAT,
    )

    assert stream["url"] == "https://stream/final"
    assert info["requested_downloads"][0]["url"] == "https://stream/final"
    assert calls == [main.VIDEO_FORMAT_SELECTOR, main.VIDEO_FALLBACK_FORMAT]


def test_extract_stream_info_raises_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "create_ydl", lambda _fmt: StubYDL({}))

    with pytest.raises(HTTPException) as excinfo:
        main._extract_stream_info("https://example.com", "best")

    assert excinfo.value.status_code == 500


def test_build_stream_invalid_format() -> None:
    with pytest.raises(HTTPException) as excinfo:
        main.build_stream("https://example.com", cast(main.FormatLiteral, "gif"))

    assert excinfo.value.status_code == 400


async def test_stream_endpoint_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_build(
        target_url: str,
        output_format: main.FormatLiteral,
    ) -> tuple[Iterator[bytes], str, str]:
        assert target_url == "https://example.com"
        assert output_format == "video"
        return iter([b"payload"]), "video/mp4", "mp4"

    monkeypatch.setattr(main, "build_stream", fake_build)
    monkeypatch.setattr(main, "fetch_video_title", lambda url: "Great Clip")

    response = await main.stream(url="https://example.com", format="video")

    chunks: list[bytes] = [cast(bytes, chunk) async for chunk in response.body_iterator]
    assert b"".join(chunks) == b"payload"
    disposition = response.headers["content-disposition"]
    assert 'filename="Great_Clip.mp4"' in disposition
    assert response.headers["cache-control"] == "no-store"


async def test_stream_endpoint_fallback_filename(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "build_stream", lambda *_: (iter([b"payload"]), "audio/mpeg", "mp3"))

    def fake_fetch(_url: str) -> str:
        raise HTTPException(status_code=400, detail="boom")

    monkeypatch.setattr(main, "fetch_video_title", fake_fetch)

    response = await main.stream(url="https://example.com", format="mp3")

    chunks: list[bytes] = [cast(bytes, chunk) async for chunk in response.body_iterator]
    assert b"".join(chunks) == b"payload"
    disposition = response.headers["content-disposition"]
    assert 'filename="download.mp3"' in disposition
