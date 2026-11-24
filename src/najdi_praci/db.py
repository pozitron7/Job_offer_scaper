"""SQLite persistence layer for job deduplication and match tracking."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path

from najdi_praci.models import JobListing, JobRecord, MatchTier

_SQLITE_BATCH_SIZE = 500

_CREATE_JOBS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT,
    location TEXT,
    first_seen_at TEXT NOT NULL,
    stage1_keep INTEGER,
    stage2_evaluated INTEGER DEFAULT 0,
    should_show INTEGER,
    match_tier TEXT,
    match_reason TEXT,
    reported_at TEXT
);
"""


def _utc_now_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


def _chunked(items: Sequence[str], size: int = _SQLITE_BATCH_SIZE) -> Iterable[Sequence[str]]:
    """Yield fixed-size slices of a sequence to stay under SQLite parameter limits."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _row_to_job_record(row: sqlite3.Row) -> JobRecord:
    """Convert a SQLite row into a JobRecord dataclass."""
    stage1_raw = row["stage1_keep"]
    should_show_raw = row["should_show"]
    return JobRecord(
        job_id=row["job_id"],
        url=row["url"],
        title=row["title"],
        company=row["company"],
        location=row["location"],
        first_seen_at=row["first_seen_at"],
        stage1_keep=None if stage1_raw is None else bool(stage1_raw),
        stage2_evaluated=bool(row["stage2_evaluated"]),
        should_show=None if should_show_raw is None else bool(should_show_raw),
        match_tier=row["match_tier"],
        match_reason=row["match_reason"],
        reported_at=row["reported_at"],
    )


class JobDatabase:
    """SQLite database wrapper for storing and querying discovered jobs."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute(_CREATE_JOBS_TABLE_SQL)

    def __enter__(self) -> JobDatabase:
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()

    def is_seen(self, job_id: str) -> bool:
        """Return True if a job_id is already stored in the database."""
        row = self._conn.execute(
            "SELECT 1 FROM jobs WHERE job_id = ? LIMIT 1",
            (job_id,),
        ).fetchone()
        return row is not None

    def get_seen_ids(self, job_ids: Iterable[str]) -> set[str]:
        """Return the subset of job_ids already present in the database."""
        unique_ids = list(dict.fromkeys(job_ids))
        if not unique_ids:
            return set()

        seen: set[str] = set()
        for batch in _chunked(unique_ids):
            placeholders = ",".join("?" for _ in batch)
            rows = self._conn.execute(
                f"SELECT job_id FROM jobs WHERE job_id IN ({placeholders})",
                tuple(batch),
            ).fetchall()
            seen.update(row["job_id"] for row in rows)
        return seen

    def upsert_discovered_job(
        self,
        job: JobListing,
        first_seen_at: str | None = None,
    ) -> bool:
        """Insert a newly discovered job if unseen; return True if newly inserted."""
        timestamp = first_seen_at or _utc_now_iso()
        with self._conn:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO jobs (
                    job_id, url, title, company, location, first_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (job.job_id, job.url, job.title, job.company, job.location, timestamp),
            )
            return cursor.rowcount > 0

    def record_stage1_result(self, job_id: str, keep: bool) -> None:
        """Record the Stage 1 title-filter decision for a job."""
        with self._conn:
            self._conn.execute(
                "UPDATE jobs SET stage1_keep = ? WHERE job_id = ?",
                (1 if keep else 0, job_id),
            )

    def record_stage2_result(
        self,
        job_id: str,
        should_show: bool,
        match_tier: MatchTier | None,
        reason: str | None,
    ) -> None:
        """Record the Stage 2 CV-matching decision for a job."""
        with self._conn:
            self._conn.execute(
                """
                UPDATE jobs
                SET stage2_evaluated = 1,
                    should_show = ?,
                    match_tier = ?,
                    match_reason = ?
                WHERE job_id = ?
                """,
                (1 if should_show else 0, match_tier, reason, job_id),
            )

    def get_job(self, job_id: str) -> JobRecord | None:
        """Fetch a single job record by ID, or None if not found."""
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        return _row_to_job_record(row) if row is not None else None

    def get_jobs_by_ids(self, job_ids: Sequence[str]) -> list[JobRecord]:
        """Fetch job records for the given IDs, preserving input order."""
        if not job_ids:
            return []

        unique_ids = list(dict.fromkeys(job_ids))
        by_id: dict[str, JobRecord] = {}
        for batch in _chunked(unique_ids):
            placeholders = ",".join("?" for _ in batch)
            rows = self._conn.execute(
                f"SELECT * FROM jobs WHERE job_id IN ({placeholders})",
                tuple(batch),
            ).fetchall()
            for row in rows:
                record = _row_to_job_record(row)
                by_id[record.job_id] = record

        return [by_id[jid] for jid in job_ids if jid in by_id]

    def get_unreported_matches(self) -> list[JobRecord]:
        """Return matched jobs not yet reported, ordered strong -> good -> stretch."""
        rows = self._conn.execute(
            """
            SELECT * FROM jobs
            WHERE should_show = 1 AND reported_at IS NULL
            ORDER BY
                CASE match_tier
                    WHEN 'strong' THEN 0
                    WHEN 'good' THEN 1
                    WHEN 'stretch' THEN 2
                    ELSE 3
                END ASC,
                first_seen_at ASC
            """
        ).fetchall()
        return [_row_to_job_record(row) for row in rows]

    def get_jobs_for_recheck(self, limit: int | None = None) -> list[JobRecord]:
        """Return Stage 1 kept jobs that have not yet been Stage 2 evaluated."""
        sql = """
            SELECT * FROM jobs
            WHERE stage1_keep = 1 AND (stage2_evaluated = 0 OR stage2_evaluated IS NULL)
            ORDER BY first_seen_at ASC
        """
        params: tuple[int, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)

        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_job_record(row) for row in rows]

    def mark_reported(
        self,
        job_ids: Iterable[str],
        reported_at: str | None = None,
    ) -> None:
        """Mark the given jobs as reported at the specified or current UTC timestamp."""
        unique_ids = list(dict.fromkeys(job_ids))
        if not unique_ids:
            return

        timestamp = reported_at or _utc_now_iso()
        with self._conn:
            for batch in _chunked(unique_ids):
                placeholders = ",".join("?" for _ in batch)
                self._conn.execute(
                    f"UPDATE jobs SET reported_at = ? WHERE job_id IN ({placeholders})",
                    (timestamp, *batch),
                )


def filter_new_listings(
    db: JobDatabase,
    listings: Iterable[JobListing],
    seen_streak_stop: int = 15,
) -> tuple[list[JobListing], bool]:
    """Filter out seen listings and detect when seen_streak_stop consecutive seen jobs occur."""
    listings_list = list(listings)
    seen_ids = db.get_seen_ids(listing.job_id for listing in listings_list)

    new_listings: list[JobListing] = []
    batch_seen: set[str] = set()
    seen_streak = 0

    for listing in listings_list:
        if listing.job_id in seen_ids:
            seen_streak += 1
            if seen_streak_stop > 0 and seen_streak >= seen_streak_stop:
                return new_listings, True
            continue

        seen_streak = 0
        if listing.job_id in batch_seen:
            continue
        batch_seen.add(listing.job_id)
        new_listings.append(listing)

    return new_listings, False
