from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse

app = FastAPI()

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
INDEX_TEMPLATE = TEMPLATES_DIR / "index.html"


@lru_cache(maxsize=1)
def load_index_template() -> str:
    try:
        return INDEX_TEMPLATE.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail="Index template is unavailable") from exc


@lru_cache(maxsize=1)
def resolve_yt_dlp() -> str:
    path = shutil.which("yt-dlp")
    if path is None:
        raise HTTPException(status_code=500, detail="yt-dlp is not available")
    return path


FormatLiteral = Literal["video", "mp3"]
UrlParam = Annotated[str, Query(..., description="Direct YouTube video URL")]
FormatParam = Annotated[FormatLiteral, Query(pattern="^(video|mp3)$")]

INVALID_FILENAME_CHARS = set('<>:"/\\|?*')


def fetch_video_title(target_url: str) -> str:
    cmd = [
        resolve_yt_dlp(),
        "--no-playlist",
        "--get-title",
        target_url,
    ]
    try:
        result = subprocess.run(  # noqa: S603 - command assembled from a trusted template
            cmd,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:  # yt-dlp missing
        raise HTTPException(status_code=500, detail="yt-dlp is not available") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or "Unable to retrieve video title"
        raise HTTPException(status_code=400, detail=detail) from exc

    title = result.stdout.strip()
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


@app.get("/", response_class=HTMLResponse)  # type: ignore[misc]
async def index() -> str:
    return load_index_template()


def build_command(target_url: str, output_format: str) -> tuple[list[str], str, str]:
    executable = resolve_yt_dlp()
    if output_format == "mp3":
        cmd = [
            executable,
            "--no-playlist",
            "-f",
            "bestaudio/best",
            "--extract-audio",
            "--audio-format",
            "mp3",
            "-o",
            "-",
            target_url,
        ]
        media_type = "audio/mpeg"
        extension = "mp3"
    elif output_format == "video":
        cmd = [
            executable,
            "--no-playlist",
            "-f",
            "bv*+ba/best",
            "--merge-output-format",
            "mp4",
            "-o",
            "-",
            target_url,
        ]
        media_type = "video/mp4"
        extension = "mp4"
    else:
        raise HTTPException(status_code=400, detail="Unsupported format")

    return cmd, media_type, extension


def stream_process(cmd: list[str]) -> Iterator[bytes]:
    try:
        process = subprocess.Popen(  # noqa: S603 - command assembled from a trusted template
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except FileNotFoundError as exc:  # yt-dlp missing
        raise HTTPException(status_code=500, detail="yt-dlp is not available") from exc

    stdout = process.stdout
    stderr = process.stderr

    if stdout is None or stderr is None:
        process.kill()
        raise HTTPException(status_code=500, detail="Process pipes are unavailable")

    def iterator() -> Iterator[bytes]:
        try:
            yield from iter(lambda: stdout.read(65536), b"")
            process.wait()
            if process.returncode:
                stderr_text = stderr.read().decode("utf-8", errors="ignore")
                detail = stderr_text.strip() or (
                    f"Download failed with exit code {process.returncode}"
                )
                raise RuntimeError(detail)
        finally:
            stdout.close()
            stderr.close()

    return iterator()


@app.get("/stream")  # type: ignore[misc]
async def stream(
    url: UrlParam,
    format: FormatParam = "video",
) -> StreamingResponse:
    cmd, media_type, extension = build_command(url, format)
    try:
        title = fetch_video_title(url)
        filename = sanitize_filename(title, extension)
    except HTTPException:
        filename = f"download.{extension}"
    iterator = stream_process(cmd)

    response = StreamingResponse(iterator, media_type=media_type)
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["Cache-Control"] = "no-store"
    return response
