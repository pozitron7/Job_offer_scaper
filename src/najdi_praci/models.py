"""Shared data models for the najdi_praci pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

MatchTier = Literal["strong", "good", "stretch"]


@dataclass(slots=True)
class JobListing:
    """A job card parsed from a jobs.cz search results page."""

    job_id: str
    title: str
    url: str
    company: str | None = None
    location: str | None = None
    posted_text: str | None = None
    age_days: int | None = None


@dataclass(slots=True)
class JobDetail:
    """Full job detail extracted from a jobs.cz job posting page."""

    job_id: str
    title: str
    url: str
    company: str | None = None
    location: str | None = None
    salary: str | None = None
    description: str = ""


class Stage1Response(BaseModel):
    """Structured output schema for Gemini Stage 1 batch title filtering."""

    keep_ids: list[str] = Field(
        default_factory=list,
        description="List of job_id strings to keep for Stage 2 evaluation.",
    )


class Stage2Response(BaseModel):
    """Structured output schema for Gemini Stage 2 CV-to-JD evaluation."""

    should_show: bool = Field(
        description=(
            "True if the job is a strong, good, or stretch/borderline match "
            "that the candidate could reasonably apply to."
        )
    )
    match_tier: MatchTier = Field(
        description="Match quality tier: 'strong', 'good', or 'stretch'."
    )
    reason: str = Field(
        description="1-2 concise sentences explaining why it fits or what is missing."
    )


@dataclass(slots=True)
class JobRecord:
    """A job row stored in SQLite and passed to the reporter."""

    job_id: str
    url: str
    title: str
    company: str | None = None
    location: str | None = None
    first_seen_at: str = ""
    stage1_keep: bool | None = None
    stage2_evaluated: bool = False
    should_show: bool | None = None
    match_tier: MatchTier | None = None
    match_reason: str | None = None
    reported_at: str | None = None


@dataclass(slots=True)
class RunSummary:
    """Aggregate statistics for a single pipeline run."""

    run_date: str
    total_scraped: int = 0
    new_jobs: int = 0
    stage1_kept: int = 0
    stage2_evaluated: int = 0
    stage2_matched: int = 0
    dry_run: bool = False
    skip_gemini: bool = False
    skip_stage2: bool = False
