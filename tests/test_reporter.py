"""Unit tests for najdi_praci.reporter."""

from __future__ import annotations

from pathlib import Path

from najdi_praci.models import JobRecord, RunSummary
from najdi_praci.reporter import (
    TIER_ORDER,
    format_terminal_summary,
    render_html,
    render_markdown,
    sort_matches,
    write_reports,
)


def _make_summary(**overrides: object) -> RunSummary:
    defaults = {
        "run_date": "2026-11-24",
        "total_scraped": 42,
        "new_jobs": 15,
        "stage1_kept": 6,
        "stage2_evaluated": 6,
        "stage2_matched": 3,
    }
    defaults.update(overrides)
    return RunSummary(**defaults)  # type: ignore[arg-type]


def test_tier_order_constants() -> None:
    assert TIER_ORDER == {"strong": 0, "good": 1, "stretch": 2}


def test_sort_matches_orders_by_tier_then_title() -> None:
    jobs = [
        JobRecord(job_id="3", url="https://www.jobs.cz/r/3", title="Zeta", match_tier="stretch"),
        JobRecord(job_id="1", url="https://www.jobs.cz/r/1", title="Beta", match_tier="good"),
        JobRecord(job_id="2", url="https://www.jobs.cz/r/2", title="Alpha", match_tier="strong"),
        JobRecord(job_id="4", url="https://www.jobs.cz/r/4", title="Another Strong", match_tier="strong"),
    ]

    sorted_jobs = sort_matches(jobs)
    assert [j.job_id for j in sorted_jobs] == ["2", "4", "1", "3"]


def test_render_markdown_clickable_links_and_summary() -> None:
    jobs = [
        JobRecord(
            job_id="101",
            url="https://www.jobs.cz/rpd/101/",
            title="Senior Python Developer",
            company="Acme s.r.o.",
            match_tier="strong",
            match_reason="Strong match with Python and backend experience.",
        ),
        JobRecord(
            job_id="102",
            url="https://www.jobs.cz/rpd/102/",
            title="Data Analyst",
            company=None,
            match_tier="stretch",
            match_reason="Adjacent analytics role.",
        ),
    ]
    summary = _make_summary(stage2_matched=2)

    md = render_markdown(jobs, summary)

    assert "# Job Matches — 2026-11-24" in md
    assert "**Total scraped:** 42" in md
    assert "**Matched jobs:** 2" in md
    assert (
        "- **[Senior Python Developer](https://www.jobs.cz/rpd/101/)** — "
        "*Acme s.r.o.* (Strong) — Strong match with Python and backend experience."
    ) in md
    assert (
        "- **[Data Analyst](https://www.jobs.cz/rpd/102/)** — "
        "*Unknown company* (Stretch) — Adjacent analytics role."
    ) in md


def test_render_markdown_empty_matches() -> None:
    md = render_markdown([], _make_summary(stage2_matched=0))
    assert "# Job Matches — 2026-11-24" in md
    assert "No matching jobs found for this run." in md


def test_render_html_clickable_links_badges_and_escaping() -> None:
    jobs = [
        JobRecord(
            job_id="201",
            url='https://www.jobs.cz/rpd/201/?q="python"&a=1',
            title='<script>alert("xss")</script> Python Dev',
            company="A & B <Tech>",
            location="Praha",
            match_tier="good",
            match_reason='Matches <Python> & "FastAPI".',
        ),
    ]
    summary = _make_summary(stage2_matched=1)

    doc = render_html(jobs, summary)

    assert "<!DOCTYPE html>" in doc
    assert "badge-good" in doc
    assert 'target="_blank" rel="noopener noreferrer"' in doc
    assert "<script>" not in doc
    assert "&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt; Python Dev" in doc
    assert "A &amp; B &lt;Tech&gt;" in doc
    assert 'href="https://www.jobs.cz/rpd/201/?q=&quot;python&quot;&amp;a=1"' in doc
    assert "Matches &lt;Python&gt; &amp; &quot;FastAPI&quot;." in doc


def test_render_html_empty_matches() -> None:
    doc = render_html([], _make_summary(stage2_matched=0))
    assert "<!DOCTYPE html>" in doc
    assert "No matching jobs found for this run." in doc


def test_write_reports_creates_dated_files(tmp_path: Path) -> None:
    reports_dir = tmp_path / "nested" / "reports"
    jobs = [
        JobRecord(
            job_id="301",
            url="https://www.jobs.cz/rpd/301/",
            title="Backend Engineer",
            company="Example Corp",
            match_tier="strong",
            match_reason="Great fit.",
        )
    ]
    summary = _make_summary(stage2_matched=1)

    md_path, html_path = write_reports(jobs, summary, reports_dir)

    assert md_path == reports_dir / "2026-11-24.md"
    assert html_path == reports_dir / "2026-11-24.html"
    assert md_path.exists()
    assert html_path.exists()
    assert "[Backend Engineer](https://www.jobs.cz/rpd/301/)" in md_path.read_text(encoding="utf-8")
    assert "Backend Engineer" in html_path.read_text(encoding="utf-8")


def test_format_terminal_summary(tmp_path: Path) -> None:
    summary = _make_summary()
    md_path = tmp_path / "2026-11-24.md"
    html_path = tmp_path / "2026-11-24.html"

    text = format_terminal_summary(summary, md_path=md_path, html_path=html_path)

    assert "2026-11-24" in text
    assert "Total scraped:    42" in text
    assert "Matched jobs:     3" in text
    assert str(md_path) in text
    assert str(html_path) in text
