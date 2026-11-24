"""Gemini Stage 1 (title filter) and Stage 2 (CV-to-JD) matcher."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel

from najdi_praci.models import JobDetail, JobListing, Stage1Response, Stage2Response

logger = logging.getLogger(__name__)

T = TypeVar("T")
ModelT = TypeVar("ModelT", bound=BaseModel)

_STAGE1_SYSTEM_INSTRUCTION = (
    "You are a high-recall job title screener for Czech and English job listings (jobs.cz).\n"
    "Your #1 priority is HIGH RECALL: NEVER miss a potentially relevant job.\n"
    "- Account for both Czech and English job titles, synonyms, and seniority variations.\n"
    "- KEEP anything remotely plausible, adjacent, transferable, or ambiguous "
    "(e.g., generic titles like 'Specialista', 'Analytik', 'Konzultant', 'Engineer', 'Coordinator').\n"
    "- ONLY exclude jobs that are clearly and unambiguously in an unrelated domain.\n"
    "- When in doubt, ALWAYS keep the job ID.\n"
    "Return JSON matching the schema with keep_ids containing the exact 'id' strings to keep."
)

_STAGE2_SYSTEM_INSTRUCTION = (
    "You are a high-recall career advisor evaluating a job description against a candidate's CV "
    "(Czech and/or English).\n"
    "Your #1 priority is HIGH RECALL: it is better to include a borderline or stretch match "
    "than to silently drop a good opportunity.\n"
    "- Set should_show=true for strong matches, good matches, AND stretch/borderline matches "
    "where the candidate has transferable skills or could reasonably apply.\n"
    "- Set should_show=false ONLY when the role is clearly in an unrelated field or requires "
    "mandatory hard qualifications/licenses the candidate clearly lacks.\n"
    "- match_tier must be one of: 'strong' (direct fit), 'good' (solid fit with minor gaps), "
    "or 'stretch' (adjacent/partial/uncertain fit).\n"
    "- reason must be 1-2 concise sentences explaining why it fits or what is missing."
)

_STAGE2_FAIL_OPEN_REASON = (
    "Automatic fallback: Gemini evaluation failed after retries; review manually."
)


def _strip_json_fences(text: str) -> str:
    """Strip optional markdown code fences around JSON text."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return stripped


def _parse_response(response: Any, schema: type[ModelT]) -> ModelT:
    """Parse a Gemini response into a Pydantic schema using parsed or text JSON fallback."""
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, schema):
        return parsed
    if isinstance(parsed, dict):
        return schema.model_validate(parsed)

    raw_text = getattr(response, "text", None)
    if not raw_text or not str(raw_text).strip():
        raise ValueError(f"Empty response when parsing {schema.__name__}")

    cleaned = _strip_json_fences(str(raw_text))
    return schema.model_validate_json(cleaned)


class GeminiMatcher:
    """Two-stage Gemini job matcher with retry and fail-open guarantees."""

    def __init__(
        self,
        api_key: str,
        stage1_model: str = "gemini-3.5-flash-lite",
        stage2_model: str = "gemini-3.5-flash",
        stage1_batch_size: int = 75,
        max_retries: int = 3,
        client: Any | None = None,
        base_backoff_sec: float = 1.0,
    ) -> None:
        if stage1_batch_size < 1:
            raise ValueError("stage1_batch_size must be at least 1")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")

        self.stage1_model = stage1_model
        self.stage2_model = stage2_model
        self.stage1_batch_size = stage1_batch_size
        self.max_retries = max_retries
        self.base_backoff_sec = base_backoff_sec
        self._client = client if client is not None else genai.Client(api_key=api_key)

    def _call_with_retry(self, operation_name: str, fn: Callable[[], T]) -> T:
        """Execute fn with exponential backoff up to max_retries retries."""
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt >= self.max_retries:
                    break
                delay = self.base_backoff_sec * (2**attempt)
                logger.warning(
                    "%s failed (attempt %d/%d): %s. Retrying in %.2fs.",
                    operation_name,
                    attempt + 1,
                    self.max_retries + 1,
                    exc,
                    delay,
                )
                if delay > 0:
                    time.sleep(delay)

        assert last_exc is not None
        raise last_exc

    def filter_titles_stage1(
        self, listings: Sequence[JobListing], cv_summary: str
    ) -> set[str]:
        """Filter job listings in batches using Stage 1 high-recall title screening.

        Fails open per batch: if a batch fails after retries, all job_ids in that
        batch are kept.
        """
        if not listings:
            return set()

        kept_ids: set[str] = set()
        for start in range(0, len(listings), self.stage1_batch_size):
            batch = listings[start : start + self.stage1_batch_size]
            batch_ids = {item.job_id for item in batch}
            kept_ids.update(self._filter_stage1_batch(batch, batch_ids, cv_summary))

        return kept_ids

    def _filter_stage1_batch(
        self,
        batch: Sequence[JobListing],
        batch_ids: set[str],
        cv_summary: str,
    ) -> set[str]:
        """Run Stage 1 screening for a single batch with fail-open fallback."""
        payload = [
            {
                "id": item.job_id,
                "title": item.title,
                "company": item.company or "",
            }
            for item in batch
        ]
        prompt = (
            f"Candidate CV Summary:\n{cv_summary.strip()}\n\n"
            f"Job Listings (JSON):\n{json.dumps(payload, ensure_ascii=False)}"
        )
        config = types.GenerateContentConfig(
            system_instruction=_STAGE1_SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=Stage1Response,
            temperature=0.1,
        )

        def _invoke() -> Stage1Response:
            response = self._client.models.generate_content(
                model=self.stage1_model,
                contents=prompt,
                config=config,
            )
            return _parse_response(response, Stage1Response)

        try:
            result = self._call_with_retry("Stage 1 batch", _invoke)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Stage 1 batch of %d jobs failed after %d retries (%s). "
                "Failing open and keeping entire batch.",
                len(batch),
                self.max_retries,
                exc,
            )
            return set(batch_ids)

        return {job_id for job_id in result.keep_ids if job_id in batch_ids}

    def evaluate_job_stage2(self, detail: JobDetail, cv_text: str) -> Stage2Response:
        """Evaluate a full job description against the candidate's full CV.

        Fails open: if evaluation fails after retries, returns a stretch match
        with should_show=True so the user can review manually.
        """
        prompt = (
            f"Candidate Full CV:\n{cv_text.strip()}\n\n"
            "Job Posting:\n"
            f"- Title: {detail.title}\n"
            f"- Company: {detail.company or 'N/A'}\n"
            f"- Location: {detail.location or 'N/A'}\n"
            f"- Salary: {detail.salary or 'N/A'}\n"
            f"- URL: {detail.url}\n\n"
            f"Description:\n{detail.description.strip()}"
        )
        config = types.GenerateContentConfig(
            system_instruction=_STAGE2_SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=Stage2Response,
            temperature=0.1,
        )

        def _invoke() -> Stage2Response:
            response = self._client.models.generate_content(
                model=self.stage2_model,
                contents=prompt,
                config=config,
            )
            return _parse_response(response, Stage2Response)

        try:
            return self._call_with_retry(f"Stage 2 job {detail.job_id}", _invoke)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Stage 2 evaluation for job %s failed after %d retries (%s). "
                "Failing open as stretch match.",
                detail.job_id,
                self.max_retries,
                exc,
            )
            return Stage2Response(
                should_show=True,
                match_tier="stretch",
                reason=_STAGE2_FAIL_OPEN_REASON,
            )
