# Repository Guidelines

## Project Structure & Module Organization
- `app/main.py` hosts the FastAPI application, streaming pipeline, and helpers for sanitizing filenames.
- `app/templates/index.html` delivers the one-page UI; keep new assets lightweight and colocated if they stay stateless.
- `pyproject.toml` and `uv.lock` pin Python 3.11 dependencies; update them via `uv add` or `uv remove` so the lockfile stays consistent.
- `Dockerfile` builds the production image with `uv` and `ffmpeg`; prefer multi-stage tweaks over ad-hoc shell scripts.

## Build, Test, and Development Commands
- `uv sync --extra dev` creates `.venv/` with runtime and tooling dependencies.
- `uv run uvicorn app.main:app --reload --port 8000` starts the API locally with hot reload.
- `uv run ruff check .` and `uv run ruff format .` lint and format the codebase (line length 100, double quotes).
- `uv run mypy .` enforces strict typing; fix or justify any new ignores.
- `docker build -t dreamy .` and `docker run --rm -p 8000:8000 dreamy` validate container builds before release work.

## Coding Style & Naming Conventions
- Follow Python type hints everywhere; `mypy`’s strict mode requires explicit optionality and generics.
- Use snake_case for functions and variables, PascalCase for classes, and keep module-level constants uppercase.
- Prefer pure functions when possible; document side effects with concise docstrings.
- Keep FastAPI routes thin—delegate streaming logic to helpers in `app/main.py` to avoid endpoint bloat.

## Testing Guidelines
- There is no automated test suite yet; new features should include tests (e.g., `pytest` with `httpx.AsyncClient`) alongside quality checks.
- Name async test modules `test_*.py` under a `tests/` package to keep discovery simple.
- Always run `uv run ruff check .` and `uv run mypy .` before submitting; note any remaining warnings in the PR.

## Commit & Pull Request Guidelines
- Commits should be small, focused, and written in present tense (e.g., `Add streaming timeout handling`). If you adopt Conventional Commits (`feat: ...`), keep the scope relevant.
- Reference related issues in the commit body or PR description.
- Pull requests should describe intent, implementation notes, manual test commands, and any follow-up work. Attach UI screenshots if the HTML template changes.
- Confirm `yt-dlp` and `ffmpeg` availability during review by sharing the commands you used to verify streaming locally.

## Tooling Rules
- Only modify dependencies via `uv add` or `uv remove`; never edit `pyproject.toml` by hand.
- Use `uv` for all package operations; do not invoke other package managers.
- Run Python code with `uv run` (e.g., `uv run python ...`); avoid direct `python` calls.
- Reserve `uv pip` for exceptional cases only, when no other option works.
