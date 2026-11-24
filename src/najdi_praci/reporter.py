"""Markdown, HTML, and terminal reporting for najdi_praci."""

from __future__ import annotations

import html
from pathlib import Path
from typing import Sequence

from najdi_praci.models import JobRecord, RunSummary

TIER_ORDER: dict[str, int] = {
    "strong": 0,
    "good": 1,
    "stretch": 2,
}

_TIER_LABELS: dict[str, str] = {
    "strong": "Strong",
    "good": "Good",
    "stretch": "Stretch",
}


def sort_matches(jobs: Sequence[JobRecord]) -> list[JobRecord]:
    """Return jobs sorted by match tier (strong -> good -> stretch), then title."""
    return sorted(
        jobs,
        key=lambda job: (
            TIER_ORDER.get((job.match_tier or "").lower(), 99),
            (job.title or "").casefold(),
            job.job_id,
        ),
    )


def _clean_inline(text: str | None, fallback: str = "") -> str:
    """Collapse newlines/whitespace for single-line Markdown/HTML rendering."""
    if not text:
        return fallback
    return " ".join(text.split())


def render_markdown(jobs: Sequence[JobRecord], summary: RunSummary) -> str:
    """Render a daily Markdown report with clickable job links."""
    sorted_jobs = sort_matches(jobs)
    matched_count = len(sorted_jobs)

    lines: list[str] = [
        f"# Job Matches — {summary.run_date}",
        "",
        "## Run Summary",
        f"- **Date:** {summary.run_date}",
        f"- **Total scraped:** {summary.total_scraped}",
        f"- **New jobs:** {summary.new_jobs}",
        f"- **Stage 1 kept:** {summary.stage1_kept}",
        f"- **Stage 2 evaluated:** {summary.stage2_evaluated}",
        f"- **Matched jobs:** {matched_count}",
        "",
        "## Matches",
        "",
    ]

    if not sorted_jobs:
        lines.append("No matching jobs found for this run.")
        lines.append("")
        return "\n".join(lines)

    for job in sorted_jobs:
        title = _clean_inline(job.title, "Untitled position")
        url = _clean_inline(job.url, "#")
        company = _clean_inline(job.company, "Unknown company")
        tier_key = (job.match_tier or "").lower()
        tier_label = _TIER_LABELS.get(tier_key, tier_key.capitalize() or "Unrated")
        reason = _clean_inline(job.match_reason, "No reason provided.")
        lines.append(
            f"- **[{title}]({url})** — *{company}* ({tier_label}) — {reason}"
        )

    lines.append("")
    return "\n".join(lines)


def render_html(jobs: Sequence[JobRecord], summary: RunSummary) -> str:
    """Render a standalone HTML5 report with tier badges and escaped clickable links."""
    sorted_jobs = sort_matches(jobs)
    matched_count = len(sorted_jobs)
    esc_date = html.escape(summary.run_date, quote=True)

    cards_html: list[str] = []
    for job in sorted_jobs:
        title = html.escape(_clean_inline(job.title, "Untitled position"), quote=True)
        url = html.escape(_clean_inline(job.url, "#"), quote=True)
        company = html.escape(_clean_inline(job.company, "Unknown company"), quote=True)
        location = _clean_inline(job.location)
        location_html = (
            f' <span class="location">· {html.escape(location, quote=True)}</span>'
            if location
            else ""
        )
        tier_key = (job.match_tier or "stretch").lower()
        if tier_key not in TIER_ORDER:
            tier_key = "stretch"
        tier_label = html.escape(
            _TIER_LABELS.get(tier_key, tier_key.capitalize()), quote=True
        )
        reason = html.escape(
            _clean_inline(job.match_reason, "No reason provided."), quote=True
        )

        cards_html.append(
            f"""        <article class="job-card">
          <div class="job-header">
            <a class="job-title" href="{url}" target="_blank" rel="noopener noreferrer">{title}</a>
            <span class="badge badge-{tier_key}">{tier_label}</span>
          </div>
          <div class="job-meta"><em>{company}</em>{location_html}</div>
          <p class="job-reason">{reason}</p>
        </article>"""
        )

    if cards_html:
        matches_section = "\n".join(cards_html)
    else:
        matches_section = (
            '        <p class="empty-state">No matching jobs found for this run.</p>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Job Matches — {esc_date}</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #f8fafc;
      --card: #ffffff;
      --text: #0f172a;
      --muted: #475569;
      --border: #e2e8f0;
      --link: #1d4ed8;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #0f172a;
        --card: #1e293b;
        --text: #f1f5f9;
        --muted: #94a3b8;
        --border: #334155;
        --link: #60a5fa;
      }}
    }}
    body {{
      font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      margin: 0;
      padding: 2rem 1rem;
      line-height: 1.5;
    }}
    main {{
      max-width: 900px;
      margin: 0 auto;
    }}
    h1 {{
      margin-top: 0;
      font-size: 1.75rem;
    }}
    .summary {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 0.75rem;
      margin-bottom: 2rem;
    }}
    .stat {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 0.75rem 1rem;
    }}
    .stat-label {{
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--muted);
    }}
    .stat-value {{
      font-size: 1.25rem;
      font-weight: 700;
    }}
    .job-card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 1rem 1.25rem;
      margin-bottom: 1rem;
    }}
    .job-header {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 1rem;
    }}
    .job-title {{
      font-size: 1.1rem;
      font-weight: 600;
      color: var(--link);
      text-decoration: none;
    }}
    .job-title:hover {{
      text-decoration: underline;
    }}
    .badge {{
      display: inline-block;
      padding: 0.15rem 0.6rem;
      border-radius: 999px;
      font-size: 0.75rem;
      font-weight: 600;
      white-space: nowrap;
    }}
    .badge-strong {{
      background: #dcfce7;
      color: #166534;
    }}
    .badge-good {{
      background: #dbeafe;
      color: #1e40af;
    }}
    .badge-stretch {{
      background: #fef3c7;
      color: #92400e;
    }}
    .job-meta {{
      margin-top: 0.25rem;
      color: var(--muted);
      font-size: 0.92rem;
    }}
    .job-reason {{
      margin: 0.6rem 0 0;
    }}
    .empty-state {{
      color: var(--muted);
      font-style: italic;
    }}
  </style>
</head>
<body>
  <main>
    <h1>Job Matches — {esc_date}</h1>
    <section class="summary" aria-label="Run Summary">
      <div class="stat"><div class="stat-label">Scraped</div><div class="stat-value">{summary.total_scraped}</div></div>
      <div class="stat"><div class="stat-label">New Jobs</div><div class="stat-value">{summary.new_jobs}</div></div>
      <div class="stat"><div class="stat-label">Stage 1 Kept</div><div class="stat-value">{summary.stage1_kept}</div></div>
      <div class="stat"><div class="stat-label">Stage 2 Eval</div><div class="stat-value">{summary.stage2_evaluated}</div></div>
      <div class="stat"><div class="stat-label">Matched</div><div class="stat-value">{matched_count}</div></div>
    </section>
    <section aria-label="Matched Jobs">
{matches_section}
    </section>
  </main>
</body>
</html>
"""


def write_reports(
    jobs: Sequence[JobRecord],
    summary: RunSummary,
    reports_dir: Path,
) -> tuple[Path, Path]:
    """Write Markdown and HTML reports for the run and return their paths."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    md_path = reports_dir / f"{summary.run_date}.md"
    html_path = reports_dir / f"{summary.run_date}.html"

    md_path.write_text(render_markdown(jobs, summary), encoding="utf-8")
    html_path.write_text(render_html(jobs, summary), encoding="utf-8")
    return md_path, html_path


def format_terminal_summary(
    summary: RunSummary,
    md_path: Path | None = None,
    html_path: Path | None = None,
) -> str:
    """Format a concise summary for terminal output."""
    lines = [
        f"=== najdi_praci summary ({summary.run_date}) ===",
        f"Total scraped:    {summary.total_scraped}",
        f"New jobs:         {summary.new_jobs}",
        f"Stage 1 kept:     {summary.stage1_kept}",
        f"Stage 2 eval:     {summary.stage2_evaluated}",
        f"Matched jobs:     {summary.stage2_matched}",
    ]
    if md_path is not None:
        lines.append(f"Markdown report:  {md_path}")
    if html_path is not None:
        lines.append(f"HTML report:      {html_path}")
    return "\n".join(lines)
