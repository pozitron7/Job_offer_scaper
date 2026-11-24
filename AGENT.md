# AGENT.md — Project Guide & Orchestration Context

## Project Overview
This repository implements a daily automated job-hunting pipeline for **jobs.cz** powered by the **Google Gemini API (`google-genai`)**.
See [PLAN.md](file:///c:/Users/uzivatel/Downloads/Najdi_praci_skript/PLAN.md) for the full architecture and specification.

## Core Design Principles (Do Not Violate)
1. **High Recall Over Precision:**
   - Stage 1 (title filtering) and Stage 2 (CV matching) must **never** miss a good match.
   - Showing some irrelevant or stretch jobs is acceptable; silently dropping a relevant job is a bug.
   - If an LLM call fails after retries in Stage 1, fail open (keep the batch).
2. **Clickable Output:**
   - Every reported job must display its **NAME** and a clickable **URL** (`[NAME](URL)` in Markdown, `<a href="URL">NAME</a>` in HTML).
3. **Configurable Time/Page Bounds:**
   - `max_pages` and `max_age_days` must be configurable in `config.yaml` AND overridable via CLI flags (`--max-pages`, `--max-age-days`, `--limit`, `--dry-run`) for fast testing.
4. **Configurable Gemini Models & API Key:**
   - Load `GEMINI_API_KEY` from `.env` (never hardcode keys, never commit `.env`).
   - Use the official `google-genai` Python SDK (`from google import genai`).
   - Keep `stage1_model` and `stage2_model` configurable in `config.yaml` / `.env` (defaulting to newer, low-cost Flash-Lite / Flash models such as `gemini-3.5-flash-lite` and `gemini-3.5-flash`). Verify model IDs against the API during testing.
5. **Windows & Scheduler Compatibility:**
   - Resolve all paths relative to the project root, not the caller's current working directory.
   - Avoid interactive prompts during normal runs.
   - Use clear exit codes and write logs to `logs/YYYY-MM-DD.log`.
   - Include `run_daily.bat` for manual execution and future Windows Task Scheduler setup.

## Tech Stack
- **Language:** Python 3.11+
- **Package Manager:** `uv` (or `pip` with `pyproject.toml`)
- **HTTP & Scraping:** `httpx`, `beautifulsoup4` (with `playwright` fallback if `jobs.cz` blocks or requires JS rendering)
- **LLM SDK:** `google-genai`
- **Data & Config:** `sqlite3` (stdlib), `pydantic`, `pyyaml`, `python-dotenv`, `pypdf`

## Suggested Orchestration / Implementation Order
When building this project, execute in these phases:
1. **Scaffold & Config:** Create `pyproject.toml`, `.gitignore`, `.env.example`, `config.yaml`, `src/najdi_praci/config.py`, and `src/najdi_praci/cv_loader.py`.
2. **Persistence:** Implement `src/najdi_praci/db.py` with SQLite schema and unit tests for deduplication & seen-streak logic.
3. **Scraper:** Implement `src/najdi_praci/scraper.py` against live `jobs.cz` HTML structure (inspect actual DOM/selectors first; add Playwright fallback if needed). Test with `--max-pages 1`.
4. **Gemini Matcher:** Implement `src/najdi_praci/matcher.py` (Stage 1 batch title filter + Stage 2 CV-to-JD evaluator with Pydantic structured outputs and retry/fail-open logic).
5. **Reporter & CLI:** Implement `src/najdi_praci/reporter.py` (Markdown + HTML + terminal summary), `src/najdi_praci/cli.py`, and `run_daily.bat`.
6. **End-to-End Verification:** Run a dry-run and a limited live run (`--max-pages 1 --max-age-days 1 --limit 5`), inspect the generated `.md` and `.html` reports, and verify no duplicate processing on a second run.

## Coding Standards
- Keep modules small, typed, and single-purpose.
- Write unit tests (with `pytest`) for `db.py`, `cv_loader.py`, `reporter.py`, and `matcher.py` (mocked Gemini responses).
- Never commit `.env`, `data/`, `reports/`, `logs/`, or personal CV files.
