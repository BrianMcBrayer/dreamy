from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

import app.main as main

pytestmark = pytest.mark.anyio


class DummyStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        return b""

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(
        self, stdout_chunks: list[bytes], stderr: bytes = b"", returncode: int = 0
    ) -> None:
        self.stdout = DummyStream(stdout_chunks)
        self.stderr = DummyStream([stderr])
        self._returncode = returncode

    def wait(self) -> None:  # pragma: no cover - behaviour checked via returncode
        pass

    @property
    def returncode(self) -> int:
        return self._returncode


def test_load_index_template_returns_html() -> None:
    template = main.load_index_template()
    assert "<form" in template
    assert "Dreamy Downloader" in template


def test_fetch_video_title_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(
        cmd: list[str],
        check: bool,
        *,
        capture_output: bool,
        text: bool,
        **_kwargs: Any,
    ) -> SimpleNamespace:
        assert Path(cmd[0]).name == "yt-dlp"
        assert cmd[1:] == [
            "--no-playlist",
            "--get-title",
            "https://example.com",
        ]
        assert capture_output is True
        assert text is True
        return SimpleNamespace(stdout=" Example Title \n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    title = main.fetch_video_title("https://example.com")
    assert title == "Example Title"


def test_fetch_video_title_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(HTTPException) as excinfo:
        main.fetch_video_title("https://example.com")
    assert excinfo.value.status_code == 500
    assert "yt-dlp" in excinfo.value.detail


def test_fetch_video_title_subprocess_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=["yt-dlp"],
            stderr="failure message\n",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(HTTPException) as excinfo:
        main.fetch_video_title("https://example.com")
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "failure message"


@pytest.mark.parametrize(  # type: ignore[misc]
    "title,extension,expected",
    [
        ("Video:Title*?", "mp3", "Video_Title.mp3"),
        ("   spaced   name   ", "mp4", "spaced_name.mp4"),
        ("<>\\/:|?*", "mp3", "download.mp3"),
    ],
)
def test_sanitize_filename(title: str, extension: str, expected: str) -> None:
    assert main.sanitize_filename(title, extension) == expected


def test_build_command_mp3() -> None:
    cmd, media_type, extension = main.build_command("https://example.com", "mp3")
    assert Path(cmd[0]).name == "yt-dlp"
    assert cmd[1:4] == ["--no-playlist", "-f", "bestaudio/best"]
    assert cmd[4:8] == ["--extract-audio", "--audio-format", "mp3", "-o"]
    assert media_type == "audio/mpeg"
    assert extension == "mp3"


def test_build_command_video() -> None:
    cmd, media_type, extension = main.build_command("https://example.com", "video")
    assert Path(cmd[0]).name == "yt-dlp"
    assert cmd[1:4] == ["--no-playlist", "-f", "bv*+ba/best"]
    assert cmd[4:7] == ["--merge-output-format", "mp4", "-o"]
    assert media_type == "video/mp4"
    assert extension == "mp4"


def test_build_command_invalid() -> None:
    with pytest.raises(HTTPException) as excinfo:
        main.build_command("https://example.com", "gif")
    assert excinfo.value.status_code == 400
    assert "Unsupported" in excinfo.value.detail


def test_stream_process_success(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_process = FakeProcess(stdout_chunks=[b"data", b""], stderr=b"", returncode=0)

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: fake_process)

    iterator = main.stream_process(["yt-dlp"])
    assert list(iterator) == [b"data"]
    assert fake_process.stdout.closed is True
    assert fake_process.stderr.closed is True


def test_stream_process_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_process = FakeProcess(stdout_chunks=[b""], stderr=b"fatal error", returncode=1)

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: fake_process)

    iterator = main.stream_process(["yt-dlp"])
    with pytest.raises(RuntimeError) as excinfo:
        list(iterator)
    assert "fatal error" in str(excinfo.value)


def test_stream_process_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_popen(*_args: Any, **_kwargs: Any) -> FakeProcess:
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with pytest.raises(HTTPException) as excinfo:
        main.stream_process(["yt-dlp"])
    assert excinfo.value.status_code == 500
    assert "yt-dlp" in excinfo.value.detail


async def test_index_route_returns_template() -> None:
    html = await main.index()
    assert "Dreamy Downloader" in html
    assert "<form" in html


async def test_stream_endpoint_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_fetch(url: str) -> str:
        assert url == "https://example.com"
        return "Great Clip"

    def fake_stream(_cmd: list[str]) -> Iterator[bytes]:
        def generator() -> Iterator[bytes]:
            yield b"payload"

        return generator()

    monkeypatch.setattr(main, "fetch_video_title", fake_fetch)
    monkeypatch.setattr(main, "stream_process", fake_stream)

    response = await main.stream(url="https://example.com", format="video")
    chunks = [chunk async for chunk in response.body_iterator]
    assert b"".join(chunks) == b"payload"
    disposition = response.headers["content-disposition"]
    assert 'filename="Great_Clip.mp4"' in disposition
    assert response.headers["cache-control"] == "no-store"


async def test_stream_endpoint_fallback_filename(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_fetch(_url: str) -> str:
        raise HTTPException(status_code=400, detail="boom")

    def fake_stream(_cmd: list[str]) -> Iterator[bytes]:
        def generator() -> Iterator[bytes]:
            yield b"payload"

        return generator()

    monkeypatch.setattr(main, "fetch_video_title", fake_fetch)
    monkeypatch.setattr(main, "stream_process", fake_stream)

    response = await main.stream(url="https://example.com", format="mp3")
    chunks = [chunk async for chunk in response.body_iterator]
    assert b"".join(chunks) == b"payload"
    disposition = response.headers["content-disposition"]
    assert 'filename="download.mp3"' in disposition
