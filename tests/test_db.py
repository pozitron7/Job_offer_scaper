"""Unit tests for najdi_praci.db."""

from __future__ import annotations

from pathlib import Path

from najdi_praci.db import JobDatabase, filter_new_listings
from najdi_praci.models import JobListing


def _make_listing(job_id: str, title: str = "Python Developer") -> JobListing:
    return JobListing(
        job_id=job_id,
        title=title,
        url=f"https://www.jobs.cz/rpd/{job_id}/",
        company="Acme s.r.o.",
        location="Praha",
    )


def test_upsert_and_seen_tracking(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "jobs.db"
    with JobDatabase(db_path) as db:
        assert db_path.exists()
        assert db.is_seen("100") is False

        inserted = db.upsert_discovered_job(
            _make_listing("100"), first_seen_at="2026-01-01T10:00:00+00:00"
        )
        assert inserted is True
        assert db.is_seen("100") is True

        # Duplicate insert must return False and preserve original first_seen_at.
        inserted_again = db.upsert_discovered_job(
            _make_listing("100", title="Changed Title"),
            first_seen_at="2026-01-02T10:00:00+00:00",
        )
        assert inserted_again is False
        record = db.get_job("100")
        assert record is not None
        assert record.first_seen_at == "2026-01-01T10:00:00+00:00"
        assert record.title == "Python Developer"


def test_get_seen_ids_and_get_jobs_by_ids_chunking(tmp_path: Path) -> None:
    with JobDatabase(tmp_path / "jobs.db") as db:
        assert db.get_seen_ids([]) == set()
        assert db.get_jobs_by_ids([]) == []

        for idx in range(550):
            if idx % 2 == 0:
                db.upsert_discovered_job(_make_listing(f"id-{idx}"))

        query_ids = [f"id-{idx}" for idx in range(550)]
        seen = db.get_seen_ids(query_ids)
        assert len(seen) == 275
        assert "id-0" in seen
        assert "id-1" not in seen

        fetched = db.get_jobs_by_ids(["id-4", "missing", "id-2"])
        assert [job.job_id for job in fetched] == ["id-4", "id-2"]


def test_stage_results_unreported_ordering_and_mark_reported(tmp_path: Path) -> None:
    with JobDatabase(tmp_path / "jobs.db") as db:
        for jid in ("j-stretch", "j-strong", "j-good", "j-skip", "j-recheck"):
            db.upsert_discovered_job(
                _make_listing(jid), first_seen_at="2026-01-01T00:00:00+00:00"
            )
            db.record_stage1_result(jid, keep=True)

        db.record_stage2_result("j-stretch", True, "stretch", "Partial match")
        db.record_stage2_result("j-strong", True, "strong", "Exact match")
        db.record_stage2_result("j-good", True, "good", "Solid match")
        db.record_stage2_result("j-skip", False, "stretch", "Too junior")

        unreported = db.get_unreported_matches()
        assert [j.job_id for j in unreported] == ["j-strong", "j-good", "j-stretch"]

        recheck = db.get_jobs_for_recheck()
        assert [j.job_id for j in recheck] == ["j-recheck"]
        assert db.get_jobs_for_recheck(limit=1)[0].job_id == "j-recheck"

        db.mark_reported(["j-strong", "j-good"], reported_at="2026-01-01T12:00:00+00:00")
        remaining = db.get_unreported_matches()
        assert [j.job_id for j in remaining] == ["j-stretch"]


def test_filter_new_listings_resets_streak_and_stops_on_streak(tmp_path: Path) -> None:
    with JobDatabase(tmp_path / "jobs.db") as db:
        for jid in ("s1", "s2", "s3", "s4", "s5"):
            db.upsert_discovered_job(_make_listing(jid))

        # Two seen, then one new (resets streak), then three seen -> triggers stop=3.
        listings = [
            _make_listing("s1"),
            _make_listing("s2"),
            _make_listing("new-1"),
            _make_listing("new-1"),  # duplicate within same batch should be deduplicated
            _make_listing("s3"),
            _make_listing("s4"),
            _make_listing("s5"),
            _make_listing("new-after-stop"),
        ]

        new_jobs, stopped = filter_new_listings(db, listings, seen_streak_stop=3)
        assert stopped is True
        assert [j.job_id for j in new_jobs] == ["new-1"]


def test_filter_new_listings_without_hitting_streak_stop(tmp_path: Path) -> None:
    with JobDatabase(tmp_path / "jobs.db") as db:
        db.upsert_discovered_job(_make_listing("seen-1"))

        listings = [_make_listing("seen-1"), _make_listing("new-1"), _make_listing("new-2")]
        new_jobs, stopped = filter_new_listings(db, listings, seen_streak_stop=3)
        assert stopped is False
        assert [j.job_id for j in new_jobs] == ["new-1", "new-2"]
