"""Command-line entry point and pipeline orchestration for najdi_praci."""

from __future__ import annotations

import argparse
from datetime import date
import logging
from pathlib import Path
import sys
from typing import Sequence

from najdi_praci.config import AppConfig, ConfigError, PROJECT_ROOT, load_config
from najdi_praci.cv_loader import CVLoadError, build_cv_summary, load_cv_text
from najdi_praci.db import JobDatabase
from najdi_praci.matcher import GeminiMatcher
from najdi_praci.models import JobListing, RunSummary
from najdi_praci.reporter import format_terminal_summary, write_reports
from najdi_praci.scraper import JobsCzScraper, ScraperError

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="najdi-praci",
        description="Scrape jobs.cz listings and match them against your CV with Gemini.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config.yaml (defaults to project root config.yaml).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Override scraping.max_pages from config.",
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=None,
        help="Override scraping.max_age_days from config.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of new jobs to collect or recheck.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scrape jobs.cz without mutating the database or calling Gemini.",
    )
    parser.add_argument(
        "--skip-gemini",
        action="store_true",
        help="Scrape and deduplicate in DB without calling Gemini.",
    )
    parser.add_argument(
        "--skip-stage2",
        action="store_true",
        help="Run Stage 1 title filtering only, skipping detail fetch and Stage 2.",
    )
    parser.add_argument(
        "--recheck",
        action="store_true",
        help="Re-evaluate previously discovered jobs pending Stage 2 in the DB.",
    )
    return parser


def configure_logging(logs_dir: Path, run_date: str) -> Path:
    """Configure logging to both logs/YYYY-MM-DD.log and stdout."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"{run_date}.log"

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    root_logger.addHandler(file_handler)
    root_logger.addHandler(stream_handler)
    return log_path


def _close_db(db: JobDatabase | None) -> None:
    if db is None:
        return
    close_fn = getattr(db, "close", None)
    if callable(close_fn):
        close_fn()


def _print_sample_listings(listings: Sequence[JobListing], total_scraped: int, sample_size: int = 10) -> None:
    """Print a concise sample of scraped listings for --dry-run / --skip-gemini."""
    print(f"Scraped {total_scraped} listings ({len(listings)} new/selected).")
    for listing in listings[:sample_size]:
        company = f" — {listing.company}" if listing.company else ""
        print(f"  [{listing.job_id}] {listing.title}{company} -> {listing.url}")
    if len(listings) > sample_size:
        print(f"  ... and {len(listings) - sample_size} more.")


def _run_scrape_only(
    config: AppConfig,
    scraper: JobsCzScraper,
    max_pages: int,
    max_age_days: int,
    limit: int | None,
    dry_run: bool,
) -> int:
    """Execute --dry-run or --skip-gemini flow without requiring Gemini or CV."""
    db: JobDatabase | None = None
    try:
        if not dry_run:
            db = JobDatabase(config.output.db_path)
            is_seen_fn = db.is_seen
        else:
            is_seen_fn = None

        listings, total_scraped = scraper.scrape_search(
            config.search_url,
            max_pages=max_pages,
            max_age_days=max_age_days,
            is_seen_fn=is_seen_fn,
            seen_streak_stop=config.scraping.seen_streak_stop,
            limit=limit,
        )

        if db is not None:
            for listing in listings:
                db.upsert_discovered_job(listing)

        _print_sample_listings(listings, total_scraped)
        return 0
    finally:
        _close_db(db)


def _run_full_pipeline(
    config: AppConfig,
    scraper: JobsCzScraper,
    run_date: str,
    max_pages: int,
    max_age_days: int,
    limit: int | None,
    skip_stage2: bool,
    recheck: bool,
) -> int:
    """Execute full scraping + Gemini Stage 1/2 + reporting pipeline."""
    cv_text = load_cv_text(config.cv_path)
    cv_summary = build_cv_summary(cv_text)

    matcher = GeminiMatcher(
        api_key=config.gemini_api_key,
        stage1_model=config.gemini.stage1_model,
        stage2_model=config.gemini.stage2_model,
        stage1_batch_size=config.gemini.stage1_batch_size,
        max_retries=config.gemini.max_retries,
        thinking_level=config.gemini.thinking_level,
    )

    total_scraped = 0
    new_jobs_count = 0
    stage1_kept_count = 0
    stage2_evaluated_count = 0

    db: JobDatabase | None = None
    try:
        db = JobDatabase(config.output.db_path)

        if recheck:
            records = db.get_jobs_for_recheck(limit=limit)
            stage2_candidates = [
                JobListing(
                    job_id=rec.job_id,
                    title=rec.title,
                    url=rec.url,
                    company=rec.company,
                    location=rec.location,
                )
                for rec in records
            ]
            stage1_kept_count = len(stage2_candidates)
            logger.info("Recheck mode: loaded %d job(s) from DB.", len(stage2_candidates))
        else:
            listings, total_scraped = scraper.scrape_search(
                config.search_url,
                max_pages=max_pages,
                max_age_days=max_age_days,
                is_seen_fn=db.is_seen,
                seen_streak_stop=config.scraping.seen_streak_stop,
                limit=limit,
            )
            new_jobs_count = len(listings)
            for listing in listings:
                db.upsert_discovered_job(listing)

            if listings:
                keep_ids = matcher.filter_titles_stage1(listings, cv_summary)
                for listing in listings:
                    db.record_stage1_result(listing.job_id, listing.job_id in keep_ids)
                stage2_candidates = [
                    listing for listing in listings if listing.job_id in keep_ids
                ]
            else:
                stage2_candidates = []
            stage1_kept_count = len(stage2_candidates)

        if not skip_stage2:
            for listing in stage2_candidates:
                try:
                    detail = scraper.fetch_job_detail(listing)
                except ScraperError as exc:
                    logger.warning(
                        "Failed to fetch detail for job %s (%s): %s",
                        listing.job_id,
                        listing.url,
                        exc,
                    )
                    continue

                result = matcher.evaluate_job_stage2(detail, cv_text)
                db.record_stage2_result(
                    listing.job_id,
                    result.should_show,
                    result.match_tier,
                    result.reason,
                )
                stage2_evaluated_count += 1

        unreported_matches = db.get_unreported_matches()
        summary = RunSummary(
            run_date=run_date,
            total_scraped=total_scraped,
            new_jobs=new_jobs_count,
            stage1_kept=stage1_kept_count,
            stage2_evaluated=stage2_evaluated_count,
            stage2_matched=len(unreported_matches),
            dry_run=False,
            skip_gemini=False,
            skip_stage2=skip_stage2,
        )
        md_path, html_path = write_reports(
            unreported_matches, summary, config.output.reports_dir
        )
        if unreported_matches:
            db.mark_reported([job.job_id for job in unreported_matches])

        print(format_terminal_summary(summary, md_path=md_path, html_path=html_path))
        return 0
    finally:
        _close_db(db)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)
    run_date = date.today().isoformat()
    require_api_key = not (args.dry_run or args.skip_gemini)

    try:
        config = load_config(
            config_path=args.config,
            require_api_key=require_api_key,
        )
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    configure_logging(config.output.logs_dir, run_date)
    logger.info("Starting najdi_praci run (root=%s)", PROJECT_ROOT)

    max_pages = args.max_pages if args.max_pages is not None else config.scraping.max_pages
    max_age_days = (
        args.max_age_days if args.max_age_days is not None else config.scraping.max_age_days
    )
    limit = args.limit if args.limit is not None else config.scraping.max_jobs

    try:
        with JobsCzScraper(
            request_delay_sec=config.scraping.request_delay_sec,
            use_playwright_fallback=config.scraping.use_playwright_fallback,
        ) as scraper:
            if args.dry_run or args.skip_gemini:
                return _run_scrape_only(
                    config=config,
                    scraper=scraper,
                    max_pages=max_pages,
                    max_age_days=max_age_days,
                    limit=limit,
                    dry_run=args.dry_run,
                )

            return _run_full_pipeline(
                config=config,
                scraper=scraper,
                run_date=run_date,
                max_pages=max_pages,
                max_age_days=max_age_days,
                limit=limit,
                skip_stage2=args.skip_stage2,
                recheck=args.recheck,
            )
    except (ConfigError, CVLoadError) as exc:
        logger.error("Setup error: %s", exc)
        print(f"Configuration/CV error: {exc}", file=sys.stderr)
        return 2
    except ScraperError as exc:
        logger.error("Scraping failed: %s", exc)
        print(f"Scraping error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - defensive top-level guard
        logger.exception("Unexpected error during run: %s", exc)
        print(f"Unexpected error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
