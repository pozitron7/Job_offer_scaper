# Najdi práci — Daily `jobs.cz` Scraper & Gemini CV Matcher

A non-interactive Python CLI tool that scrapes new job listings from **jobs.cz**, deduplicates them in SQLite, and matches them against your CV using the **Google Gemini API (`google-genai`)** with a two-stage, high-recall pipeline.

Designed for both manual runs and daily **Windows Task Scheduler** automation, outputting clickable **Markdown** and **HTML** daily reports.

---

## How It Works

1. **Scrape & Deduplicate (`scraper.py`, `db.py`)**
   - Paginates through your configured `jobs.cz` search URL (supports both classic `www.jobs.cz` and `beta.jobs.cz` layouts, with optional Playwright Chromium fallback for JS-rendered branded pages).
   - Filters out listings older than `max_age_days` and skips already-seen jobs stored in `data/jobs.db`.
   - Stops paginating early once `seen_streak_stop` consecutive previously seen listings are encountered.
2. **Stage 1 — High-Recall Title Filter (`matcher.py`)**
   - Sends unseen job titles in batches (default `75` per request) alongside a summary of your CV to `gemini-3.5-flash-lite` using Pydantic structured outputs.
   - Tuned for **high recall** across both Czech and English titles ("when in doubt, keep it") and **fails open** (keeps the batch) if an API call fails after retries.
3. **Stage 2 — Full CV-to-JD Evaluation (`matcher.py`)**
   - Fetches the full job description for Stage 1 survivors and evaluates each against your complete CV using `gemini-3.5-flash`.
   - Assigns each match a tier (`strong`, `good`, or `stretch`) and a concise 1–2 sentence explanation.
4. **Daily Reports (`reporter.py`)**
   - Writes `reports/YYYY-MM-DD.md` and `reports/YYYY-MM-DD.html` ordered by match tier (`strong` → `good` → `stretch`), with clickable job links, company names, badges, and reasons.
   - Logs full run details to `logs/YYYY-MM-DD.log` and prints a summary to the terminal.

---

## Project Structure

```text
Najdi_praci_skript/
├── config.yaml               # Search URL, CV path, scraping & Gemini settings
├── .env.example              # Template for GEMINI_API_KEY and model overrides
├── pyproject.toml            # Python 3.11+ dependencies & CLI entry point
├── run_daily.bat             # Windows wrapper (working-directory independent)
├── src/najdi_praci/
│   ├── cli.py                # CLI entry point & pipeline orchestration
│   ├── config.py             # YAML + .env config loader
│   ├── cv_loader.py          # PDF (.pdf), Markdown (.md), and text (.txt) CV reader
│   ├── db.py                 # SQLite persistence & deduplication
│   ├── matcher.py            # Gemini Stage 1 & Stage 2 matcher
│   ├── models.py             # Shared dataclasses & Pydantic schemas
│   ├── reporter.py           # Markdown, HTML, and terminal report generator
│   └── scraper.py            # jobs.cz search & detail scraper
└── tests/                    # pytest unit test suite
```

---

## Setup

### Prerequisites
- **Python 3.11+**
- A **Google Gemini API key** (from [Google AI Studio](https://aistudio.google.com/))
- Your CV as a `.pdf`, `.md`, or `.txt` file

### 1. Create a virtual environment and install dependencies

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

*(Optional)* Install **Playwright** if you want full job description extraction on JS-rendered branded company pages (e.g., `*.jobs.cz/detail-pozice`):

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[playwright]"
.\.venv\Scripts\playwright.exe install chromium
```

### 2. Configure your API key

Create a `.env` file in the project root (based on `.env.example`):

```env
GEMINI_API_KEY=your_gemini_api_key_here
# Optional model overrides:
# STAGE1_MODEL=gemini-3.5-flash-lite
# STAGE2_MODEL=gemini-3.5-flash
```

### 3. Add your CV

Place your CV in the project root as `cv.pdf` (or place it anywhere and update `cv_path` in `config.yaml`).  
Supported formats: `.pdf`, `.md`, `.txt`.

### 4. Customize `config.yaml`

Edit `config.yaml` to set your preferred `jobs.cz` search URL (e.g., filtered by city, region, or remote work), models, and scraping bounds:

```yaml
search_url: "https://www.jobs.cz/prace/praha/"
cv_path: "cv.pdf"

scraping:
  max_pages: 20
  max_age_days: 3
  seen_streak_stop: 15
  request_delay_sec: 1.0
  use_playwright_fallback: true

gemini:
  stage1_model: "gemini-3.5-flash-lite"
  stage2_model: "gemini-3.5-flash"
  stage1_batch_size: 75
  max_retries: 3

output:
  reports_dir: "reports"
  logs_dir: "logs"
  db_path: "data/jobs.db"
```

---

## Usage

### Run via `run_daily.bat` (Windows)

```powershell
# Quick smoke test (1 page, last 1 day, max 5 jobs)
.\run_daily.bat --max-pages 1 --max-age-days 1 --limit 5

# Full daily run using config.yaml defaults
.\run_daily.bat
```

### Run directly with Python / CLI entry point

```powershell
.\.venv\Scripts\python.exe -m najdi_praci.cli
# or
.\.venv\Scripts\najdi-praci.exe
```

### CLI Options

| Flag | Description |
| :--- | :--- |
| `--config PATH` | Path to a custom `config.yaml` (defaults to project root `config.yaml`). |
| `--max-pages INT` | Override `scraping.max_pages` from `config.yaml`. |
| `--max-age-days INT` | Override `scraping.max_age_days` from `config.yaml`. |
| `--limit INT` | Cap the maximum number of new jobs collected (or rechecked). |
| `--dry-run` | Scrape `jobs.cz` and print a sample without writing to SQLite or calling Gemini (no API key or CV required). |
| `--skip-gemini` | Scrape and record deduplication in SQLite without calling Gemini (no API key or CV required). |
| `--skip-stage2` | Run scraping + Stage 1 title filtering only; skip fetching job details and Stage 2. |
| `--recheck` | Re-evaluate previously discovered Stage 1 candidates stored in SQLite. |

---

## Automating on Windows (Task Scheduler)

`run_daily.bat` automatically switches to the project directory (`cd /d "%~dp0"`) and resolves all paths relative to the project root, so it works from any working directory (including `C:\Windows\System32`).

1. Open **Windows Task Scheduler** → **Create Basic Task**.
2. Set the trigger to **Daily** at your preferred time.
3. Choose **Start a program** and point **Program/script** to the full path of `run_daily.bat` (e.g. `C:\path\to\Najdi_praci_skript\run_daily.bat`).
4. Check `reports/YYYY-MM-DD.html` (or `.md`) after each run, and `logs/YYYY-MM-DD.log` for run diagnostics.

---

## Running Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -v
```

---

## Privacy & Git Safety

`.gitignore` automatically excludes sensitive and runtime files so they are never committed:
- `.env` (API keys)
- `cv.*` (personal CV files)
- `data/`, `reports/`, `logs/` (local SQLite database, daily reports, and logs)
