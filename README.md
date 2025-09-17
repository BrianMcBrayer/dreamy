# Dreamy Downloader

Dreamy Downloader is a minimal FastAPI application that wraps `yt-dlp` and streams downloads straight to the browser. Paste any public YouTube URL, choose MP3 or MP4, and the app proxies the media stream to you without persisting files on disk.

## Highlights
- Fast streaming – the response begins as soon as `yt-dlp` produces bytes; no temporary files.
- Audio or video – select MP3 (best available audio) or MP4 (best muxed video).
- Container ready – single Dockerfile installs dependencies with [uv](https://docs.astral.sh/uv/) and ships with `ffmpeg` for audio extraction.
- Tiny UI – a single-page form served by FastAPI with zero extra assets.

## Running with Docker
1. Build the image: `docker build -t dreamy .`
2. Start the container: `docker run --rm -p 8000:8000 dreamy`
3. Open `http://localhost:8000` in your browser, paste a YouTube link, pick a format, and submit. A new tab streams the download immediately.

Environment variables are not required. The container exposes port `8000` and keeps the fast startup profile of uvicorn.
The Dockerfile bases on `ghcr.io/astral-sh/uv:python3.11-bookworm-slim`, so `uv` is preinstalled and dependency layers stay cached via `pyproject.toml`/`uv.lock`. It also uses BuildKit cache mounts for `apt` and `uv`, so build with `DOCKER_BUILDKIT=1 docker build ...` for best results.

## Local Development with uv
This project is configured for [uv](https://docs.astral.sh/uv/), a Python package and environment manager. Install uv (one-time):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Install dependencies into a local `.venv` and start the dev server:

```bash
uv sync --extra dev
uv run uvicorn app.main:app --reload --port 8000
```

The command `uv sync` resolves dependencies from `pyproject.toml`, creates `.venv/`, and ensures `ffmpeg` is available only if you install it on your machine. For streaming audio to MP3 locally you’ll still need `ffmpeg` in your PATH.

### Quality tooling
- Lint & format: `uv run ruff check .` and `uv run ruff format .`
- Type-check: `uv run mypy .`
- Install Git hooks: `uv run pre-commit install`

## HTTP Endpoints
- `GET /` – renders the HTML form.
- `GET /stream?url=...&format=video|mp3` – launches `yt-dlp`, streams its stdout back to the client, and sets appropriate MIME types and download filenames.

On errors (invalid format, missing `yt-dlp`, or extractor issues) the API returns standard HTTP error responses with messages bubbled from `yt-dlp` stderr.

## Notes & Troubleshooting
- YouTube throttling: extremely long videos may take time to start; the app passes through `yt-dlp`’s progress transparently.
- Playlists: the downloader forces `--no-playlist` to avoid accidental batch downloads—submit individual video URLs.
- SSL certificates: ensure the base OS (container or host) has up-to-date certificates; `yt-dlp` relies on them for HTTPS.

Happy downloading!
