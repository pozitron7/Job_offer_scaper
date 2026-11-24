"""Unit tests for GeminiMatcher (Stage 1 & Stage 2) with mocked Gemini clients."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from najdi_praci.matcher import GeminiMatcher
from najdi_praci.models import JobDetail, JobListing, Stage1Response, Stage2Response


class _FakeModels:
    """Stub for client.models that replays queued responses or exceptions."""

    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self._outcomes:
            raise AssertionError("No more queued outcomes in _FakeModels")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakeClient:
    """Minimal stub matching google.genai.Client.models."""

    def __init__(self, outcomes: list[Any]) -> None:
        self.models = _FakeModels(outcomes)


def _make_listings(count: int) -> list[JobListing]:
    return [
        JobListing(
            job_id=f"job-{i}",
            title=f"Python Engineer {i}",
            url=f"https://www.jobs.cz/rpd/{i}/",
            company=f"Company {i}",
        )
        for i in range(1, count + 1)
    ]


def _make_detail(job_id: str = "job-42") -> JobDetail:
    return JobDetail(
        job_id=job_id,
        title="Backend Python Developer",
        url=f"https://www.jobs.cz/rpd/{job_id}/",
        company="Acme s.r.o.",
        location="Praha",
        salary="80 000 - 110 000 Kč",
        description="Looking for Python, FastAPI, PostgreSQL, and Docker experience.",
    )


def test_stage1_empty_listings_returns_empty_set() -> None:
    client = _FakeClient([])
    matcher = GeminiMatcher(api_key="test-key", client=client, base_backoff_sec=0.0)

    result = matcher.filter_titles_stage1([], "Python developer")

    assert result == set()
    assert client.models.calls == []


def test_stage1_batching_parsed_and_ignores_hallucinated_ids() -> None:
    listings = _make_listings(5)
    client = _FakeClient(
        [
            SimpleNamespace(
                parsed=Stage1Response(keep_ids=["job-1", "hallucinated-99"]),
                text=None,
            ),
            SimpleNamespace(
                parsed=None,
                text='{"keep_ids": ["job-3", "job-1", "fake-id"]}',
            ),
            SimpleNamespace(
                parsed={"keep_ids": ["job-5"]},
                text=None,
            ),
        ]
    )
    matcher = GeminiMatcher(
        api_key="test-key",
        stage1_model="gemini-3.5-flash-lite",
        stage1_batch_size=2,
        client=client,
        base_backoff_sec=0.0,
    )

    kept = matcher.filter_titles_stage1(listings, "Senior Python & Data Engineer")

    assert kept == {"job-1", "job-3", "job-5"}
    assert len(client.models.calls) == 3
    assert all(call["model"] == "gemini-3.5-flash-lite" for call in client.models.calls)


def test_stage1_retry_recovery() -> None:
    listings = _make_listings(3)
    client = _FakeClient(
        [
            RuntimeError("429 Too Many Requests"),
            RuntimeError("503 Service Unavailable"),
            SimpleNamespace(parsed=Stage1Response(keep_ids=["job-2"]), text=None),
        ]
    )
    matcher = GeminiMatcher(
        api_key="test-key",
        stage1_batch_size=10,
        max_retries=3,
        client=client,
        base_backoff_sec=0.0,
    )

    kept = matcher.filter_titles_stage1(listings, "Python developer")

    assert kept == {"job-2"}
    assert len(client.models.calls) == 3


def test_stage1_fail_open_on_persistent_error_keeps_entire_failed_batch() -> None:
    listings = _make_listings(4)
    # Batch 1 (job-1, job-2) fails all 3 attempts (initial + 2 retries) -> fail open.
    # Batch 2 (job-3, job-4) succeeds and keeps only job-3.
    client = _FakeClient(
        [
            RuntimeError("timeout 1"),
            RuntimeError("timeout 2"),
            RuntimeError("timeout 3"),
            SimpleNamespace(parsed=Stage1Response(keep_ids=["job-3"]), text=None),
        ]
    )
    matcher = GeminiMatcher(
        api_key="test-key",
        stage1_batch_size=2,
        max_retries=2,
        client=client,
        base_backoff_sec=0.0,
    )

    kept = matcher.filter_titles_stage1(listings, "Python developer")

    assert kept == {"job-1", "job-2", "job-3"}
    assert len(client.models.calls) == 4


@pytest.mark.parametrize(
    ("tier", "should_show"),
    [
        ("strong", True),
        ("good", True),
        ("stretch", True),
        ("stretch", False),
    ],
)
def test_stage2_structured_output_parsed_and_text_fallback(
    tier: str, should_show: bool
) -> None:
    expected = Stage2Response(
        should_show=should_show,
        match_tier=tier,  # type: ignore[arg-type]
        reason="Solid Python/FastAPI overlap with minor cloud gaps.",
    )
    # First via response.parsed, second via response.text JSON fallback
    client = _FakeClient(
        [
            SimpleNamespace(parsed=expected, text=None),
            SimpleNamespace(parsed=None, text=expected.model_dump_json()),
        ]
    )
    matcher = GeminiMatcher(
        api_key="test-key",
        stage2_model="gemini-3.5-flash",
        client=client,
        base_backoff_sec=0.0,
    )
    detail = _make_detail()

    res1 = matcher.evaluate_job_stage2(detail, "5 years Python, FastAPI, SQL")
    res2 = matcher.evaluate_job_stage2(detail, "5 years Python, FastAPI, SQL")

    assert res1 == expected
    assert res2 == expected
    assert len(client.models.calls) == 2
    assert all(call["model"] == "gemini-3.5-flash" for call in client.models.calls)


def test_stage2_retry_recovery_including_bad_json() -> None:
    expected = Stage2Response(
        should_show=True,
        match_tier="good",
        reason="Strong backend match.",
    )
    client = _FakeClient(
        [
            RuntimeError("500 Internal Server Error"),
            SimpleNamespace(parsed=None, text="{not valid json"),
            SimpleNamespace(parsed=expected, text=None),
        ]
    )
    matcher = GeminiMatcher(
        api_key="test-key",
        max_retries=2,
        client=client,
        base_backoff_sec=0.0,
    )

    result = matcher.evaluate_job_stage2(_make_detail(), "Python CV")

    assert result == expected
    assert len(client.models.calls) == 3


def test_stage2_fail_open_on_persistent_error() -> None:
    client = _FakeClient(
        [
            RuntimeError("err 1"),
            RuntimeError("err 2"),
            RuntimeError("err 3"),
        ]
    )
    matcher = GeminiMatcher(
        api_key="test-key",
        max_retries=2,
        client=client,
        base_backoff_sec=0.0,
    )

    result = matcher.evaluate_job_stage2(_make_detail(), "Python CV")

    assert result == Stage2Response(
        should_show=True,
        match_tier="stretch",
        reason="Automatic fallback: Gemini evaluation failed after retries; review manually.",
    )
    assert len(client.models.calls) == 3


def test_exponential_backoff_sleeps_with_expected_delays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleep_calls: list[float] = []
    monkeypatch.setattr("najdi_praci.matcher.time.sleep", sleep_calls.append)

    client = _FakeClient(
        [
            RuntimeError("err 1"),
            RuntimeError("err 2"),
            SimpleNamespace(
                parsed=Stage2Response(
                    should_show=True,
                    match_tier="strong",
                    reason="Exact match.",
                ),
                text=None,
            ),
        ]
    )
    matcher = GeminiMatcher(
        api_key="test-key",
        max_retries=3,
        client=client,
        base_backoff_sec=0.5,
    )

    result = matcher.evaluate_job_stage2(_make_detail(), "Python CV")

    assert result.match_tier == "strong"
    assert sleep_calls == [0.5, 1.0]


def test_thinking_level_passed_to_stage1_and_stage2() -> None:
    from google.genai import types

    client = _FakeClient(
        [
            SimpleNamespace(parsed=Stage1Response(keep_ids=["job-1"]), text=None),
            SimpleNamespace(
                parsed=Stage2Response(
                    should_show=True,
                    match_tier="good",
                    reason="Solid match.",
                ),
                text=None,
            ),
        ]
    )
    matcher = GeminiMatcher(
        api_key="test-key",
        thinking_level="low",
        client=client,
        base_backoff_sec=0.0,
    )

    matcher.filter_titles_stage1(_make_listings(1), "Python CV")
    matcher.evaluate_job_stage2(_make_detail(), "Python CV")

    assert len(client.models.calls) == 2
    for call in client.models.calls:
        cfg = call["config"]
        assert cfg.thinking_config is not None
        assert cfg.thinking_config.thinking_level == types.ThinkingLevel.LOW
