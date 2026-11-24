# Implementation Plan: Daily jobs.cz Scraper & Gemini CV Matcher

## 1. Goal & Core Requirements
Build a non-interactive, daily-runnable Python CLI script that:
1. Scrapes all **new** job listings from a configurable `jobs.cz` search URL (e.g., filtered by city/region/remote, without restricting job title).
2. Deduplicates against previously seen jobs using a local SQLite database (`data/jobs.db`).
3. **Stage 1 (High-Recall Title Filter):** Uses Gemini in batches (50–100 titles per request) with a CV/target-role summary and a high-recall prompt ("when in doubt, keep it") so no potentially relevant job is missed.
4. **Stage 2 (CV Matching):** Fetches the full job description for Stage 1 survivors and evaluates each against the user's full CV (PDF, Markdown, or TXT) using Gemini. Tuned for high recall: better to include a borderline/irrelevant job than miss a good match.
5. **Reporting:** Generates daily Markdown (`reports/YYYY-MM-DD.md`) and HTML (`reports/YYYY-MM-DD.html`) reports where every matched job includes a clickable **NAME** + **URL**, match tier/confidence, and a brief reason, plus prints a concise summary to the terminal.
6. **Automation-Ready:** Works via manual CLI runs anytime and is designed for future Windows Task Scheduler automation (clean exit codes, file logging, working-directory independence, `.env` loading).

---

## 2. Architecture & Project Structure

```text
Najdi_praci_skript/
├── AGENT.md                  # Agent orchestration & engineering guidelines
├── PLAN.md                   # This implementation plan
├── pyproject.toml            # Python 3.11+ dependencies & CLI entry point
├── .env.example              # Template for GEMINI_API_KEY and overrides
├── .gitignore                # Ignore .env, data/, reports/, logs/, .venv/, cv.*
├── config.yaml               # Search URL, CV path, models, max_pages, max_age_days, batch size
├── run_daily.bat             # Windows wrapper script (for manual click or Task Scheduler)
├── src/
│   └── najdi_praci/
│       ├── __init__.py
│       ├── cli.py            # CLI entry point (supports --dry-run, --max-pages, --max-age-days, --recheck)
│       ├── config.py         # Load config.yaml + .env (pydantic / pyyaml / python-dotenv)
│       ├── cv_loader.py      # Read CV from .pdf (pypdf/pymupdf) or .md/.txt
│       ├── scraper.py        # jobs.cz search pagination + job detail fetcher (httpx+BS4, Playwright fallback)
│       ├── db.py             # SQLite persistence (data/jobs.db)
│       ├── matcher.py        # Stage 1 (title batch) & Stage 2 (CV vs JD) using google-genai
│       └── reporter.py       # Generate reports/YYYY-MM-DD.md, reports/YYYY-MM-DD.html, and terminal summary
├── data/                     # Runtime SQLite DB (gitignored)
├── logs/                     # Rotating daily run logs (gitignored)
└── reports/                  # Daily .md and .html reports (gitignored)
```

---

## 3. Configuration (`.env` & `config.yaml`)

### `.env.example`
```env
GEMINI_API_KEY=your_gemini_api_key_here
# Optional overrides:
# STAGE1_MODEL=gemini-3.5-flash-lite
# STAGE2_MODEL=gemini-3.5-flash
```

### `config.yaml`
- `search_url`: Configurable `jobs.cz` search URL (e.g., `https://www.jobs.cz/prace/praha/`).
- `cv_path`: Path to local CV file (`cv.pdf`, `cv.md`, or `cv.txt`).
- `scraping`:
  - `max_pages`: `20` (configurable; also overridable via CLI `--max-pages` for testing).
  - `max_age_days`: `3` (configurable; also overridable via CLI `--max-age-days` for testing).
  - `seen_streak_stop`: `15` (stop pagination early once N consecutive already-seen jobs are encountered).
  - `request_delay_sec`: `1.0` (polite rate limiting between detail requests).
  - `use_playwright_fallback`: `true` (if `httpx` gets blocked or misses JS-rendered content).
- `gemini`:
  - `stage1_model`: `gemini-3.5-flash-lite` (or latest configured Flash-Lite model).
  - `stage2_model`: `gemini-3.5-flash` (or latest configured Flash model).
  - `stage1_batch_size`: `75` (50–100 titles per request).
  - `max_retries`: `3` (exponential backoff on 429/5xx).
- `output`:
  - `reports_dir`: `reports`
  - `logs_dir`: `logs`
  - `db_path`: `data/jobs.db`

---

## 4. Detailed Pipeline Design

### Step 1: Load Config & CV
- Resolve paths relative to project root (crucial for Windows Task Scheduler where CWD may be `C:\Windows\System32`).
- Load `GEMINI_API_KEY` from `.env`; fail fast with a clear error and non-zero exit code if missing.
- Load CV text from `.pdf` (using `pypdf`) or `.md`/`.txt`. Extract a short role/skills summary (or cache it) for Stage 1, and keep the full CV text for Stage 2.

### Step 2: Scrape `jobs.cz` Search Pages (`scraper.py`)
- Fetch `search_url` page by page (`?page=1`, `?page=2`, ... up to `max_pages`).
- Parse job cards: extract `job_id` (from URL/data attribute), `title` (NAME), `url`, `company`, `location`, and `posted_date` / age if available.
- Filter out listings older than `max_age_days` when date metadata is present.
- Check `db.py`:
  - Skip jobs already in `jobs` table.
  - Increment `seen_streak`; if `seen_streak >= seen_streak_stop`, stop paginating (we've caught up to previous runs).
- **Fallback:** If `httpx` receives 403/429/empty results or missing cards, automatically fall back to Playwright headless Chromium.

### Step 3: Stage 1 — High-Recall Title Filtering (`matcher.py`)
- Group new unseen jobs into batches of `stage1_batch_size` (50–100).
- Send each batch (`[{id, title, company}]`) + CV summary to `stage1_model` using `google-genai` with **Structured Outputs** (Pydantic schema returning `keep_ids: list[str]`).
- Prompt emphasizes **very high recall**:
  - Account for both Czech and English job titles.
  - Keep anything remotely plausible, adjacent, or ambiguous (e.g., generic titles like "Specialista", "Analytik", "Engineer").
  - Only reject obviously unrelated domains (e.g., warehouse worker, nurse, truck driver if CV is software/office).
- **Fail-safe:** If a Stage 1 batch fails after retries, default to keeping all jobs in that batch so good matches are never silently dropped.
- Record Stage 1 decisions in `data/jobs.db`.

### Step 4: Fetch Full Descriptions & Stage 2 — CV Matching (`matcher.py`)
- For each job kept by Stage 1, fetch its full job detail page (`url`) and extract clean text (title, company, location, salary if listed, full description/requirements).
- Evaluate each job against the full CV using `stage2_model` with **Structured Outputs** (Pydantic schema):
  - `should_show: bool` (high recall: `true` for strong matches, partial/stretch matches, or uncertain cases where the user could reasonably apply)
  - `match_tier: Literal["strong", "good", "stretch"]`
  - `reason: str` (1–2 concise sentences explaining why it fits or what's missing)
- **Fail-safe:** On API/parse failure after retries, mark as `stretch` with an error note or keep for retry rather than silently dropping.
- Save Stage 2 results to `data/jobs.db`.

### Step 5: Report Generation (`reporter.py`)
- Collect all jobs from the current run where `should_show == True` (ordered by `match_tier`: `strong` -> `good` -> `stretch`).
- Write `reports/YYYY-MM-DD.md`:
  - Summary header (date, total scraped, Stage 1 kept, Stage 2 matched).
  - Clickable links: `- **[Job Name](https://www.jobs.cz/...)** — *Company* (Tier) — Reason`
- Write `reports/YYYY-MM-DD.html`:
  - Clean, standalone HTML with clickable `<a href="..." target="_blank">Job Name</a>` links, grouped/badged by match tier.
- Print a concise terminal summary with counts and the path to the generated reports.

---

## 5. SQLite Schema (`data/jobs.db`)

```sql
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT,
    location TEXT,
    first_seen_at TEXT NOT NULL,
    stage1_keep INTEGER,          -- 1 = kept, 0 = filtered out
    stage2_evaluated INTEGER DEFAULT 0,
    should_show INTEGER,          -- 1 = show in report, 0 = skip
    match_tier TEXT,              -- 'strong' | 'good' | 'stretch'
    match_reason TEXT,
    reported_at TEXT
);
```

---

## 6. Windows Task Scheduler Readiness
- `run_daily.bat` activates `.venv` (or uses `uv run`), sets `cd /d "%~dp0"`, and runs `najdi-praci`.
- All logs go to `logs/YYYY-MM-DD.log` as well as stdout.
- Exit codes: `0` on success, non-zero on fatal config/API/scrape failure.
- CLI flags (`--max-pages`, `--max-age-days`, `--dry-run`, `--skip-stage2`, `--limit`) allow easy manual testing without touching `config.yaml`.
