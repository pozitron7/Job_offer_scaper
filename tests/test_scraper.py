"""Unit tests for najdi_praci.scraper."""

from __future__ import annotations

import builtins
import logging
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import httpx
import pytest

from najdi_praci.models import JobListing
from najdi_praci.scraper import (
    JobsCzScraper,
    ScraperError,
    build_page_url,
    parse_age_days,
    parse_job_detail,
    parse_search_page,
)

SEARCH_PAGE_HTML = """
<!DOCTYPE html>
<html lang="cs">
<body>
  <article class="PromoBanner"><h2>Promo</h2></article>
  <article class="SearchResultCard">
    <header class="SearchResultCard__header">
      <h2 class="SearchResultCard__title" data-test-ad-title="Python Developer">
        <a class="link-primary SearchResultCard__titleLink"
           data-jobad-id="2001344585"
           href="/rpd/2001344585/?searchId=abc&amp;rps=228">
          Python Developer
        </a>
      </h2>
      <div class="SearchResultCard__status" data-test-ad-status="default">
        Přidáno před 2 hodinami
      </div>
    </header>
    <div class="SearchResultCard__body">
      <span class="Tag">90 000 – 140 000 Kč</span>
    </div>
    <footer class="SearchResultCard__footer">
      <ul class="SearchResultCard__footerList">
        <li class="SearchResultCard__footerItem">
          <span translate="no">Acme s.r.o.</span>
        </li>
        <li class="SearchResultCard__footerItem" data-test="serp-locality">
          Praha – Karlín
        </li>
        <li class="SearchResultCard__footerItem">
          <a data-test="serp-atmoskop" href="https://www.atmoskop.cz/acme">Atmoskop</a>
        </li>
      </ul>
    </footer>
  </article>

  <article class="SearchResultCard">
    <header class="SearchResultCard__header">
      <h2 class="SearchResultCard__title" data-test-ad-title="Data Analyst">
        <a class="link-primary SearchResultCard__titleLink"
           data-jobad-id="2001551557"
           href="https://www.jobs.cz/rpd/2001551557/?rps=233">
          Data Analyst
        </a>
      </h2>
      <div class="SearchResultCard__status SearchResultCard__status--primary" data-test-ad-status="jobsTip">
        Příležitost dne
      </div>
    </header>
    <footer class="SearchResultCard__footer">
      <ul class="SearchResultCard__footerList">
        <li class="SearchResultCard__footerItem">
          <span translate="no">Beta Corp</span>
        </li>
        <li class="SearchResultCard__footerItem" data-test="serp-locality">
          Praha – Dejvice
        </li>
      </ul>
    </footer>
  </article>

  <article class="SearchResultCard">
    <header class="SearchResultCard__header">
      <h2 class="SearchResultCard__title">
        <a class="link-primary SearchResultCard__titleLink"
           data-jobad-id="2000106458"
           href="https://www.jobs.cz/rpd/2000106458/">
          Old Role
        </a>
      </h2>
      <div class="SearchResultCard__status" data-test-ad-status="default">
        Před 10 dny
      </div>
    </header>
    <footer class="SearchResultCard__footer">
      <ul class="SearchResultCard__footerList">
        <li class="SearchResultCard__footerItem">Gamma a.s.</li>
        <li class="SearchResultCard__footerItem" data-test="serp-locality">Praha</li>
      </ul>
    </footer>
  </article>
</body>
</html>
"""

DETAIL_PAGE_HTML = """
<!DOCTYPE html>
<html lang="cs">
<body>
  <div class="Stack">
    <div class="JobDescriptionHeading">
      <h1 class="typography-heading-medium-text">Senior Python Developer</h1>
    </div>
    <figure class="CompanyLogo">
      <img alt="Acme s.r.o." class="Image" src="https://example.com/logo.png"/>
    </figure>
    <div class="IconWithText">
      <p class="typography-body-medium-text-regular">90\u00a0000 \u200d–\u200d 140\u00a0000 Kč</p>
    </div>
    <div class="RichContent mb-1400">
      <p>Hledáme zkušeného vývojáře.</p>
      <ul>
        <li>Python 3.11+</li>
        <li>FastAPI a SQL</li>
      </ul>
    </div>
  </div>
</body>
</html>
"""


class TestBuildPageUrl:
    def test_page_1_clean_url(self) -> None:
        assert build_page_url("https://www.jobs.cz/prace/praha/", 1) == "https://www.jobs.cz/prace/praha/"

    def test_page_1_strips_existing_page_and_preserves_other_params(self) -> None:
        url = "https://www.jobs.cz/prace/praha/?locality%5Bradius%5D=50&page=3"
        result = build_page_url(url, 1)
        assert "page=" not in result
        assert "locality%5Bradius%5D=50" in result

    def test_page_gt_1_sets_page_and_preserves_params(self) -> None:
        url = "https://www.jobs.cz/prace/praha/?edu=vysokoskolske"
        result = build_page_url(url, 4)
        assert "edu=vysokoskolske" in result
        assert "page=4" in result


class TestParseAgeDays:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (None, None),
            ("", None),
            ("   ", None),
            ("Příležitost dne", None),
            ("Končí za 3 dny", None),
            ("Dnes", 0),
            ("Nové", 0),
            ("Today", 0),
            ("Přidáno před 2 minutami", 0),
            ("Aktualizováno před 3 hodinami", 0),
            ("Před hodinou", 0),
            ("2 hours ago", 0),
            ("Včera", 1),
            ("Yesterday", 1),
            ("Před 1 dnem", 1),
            ("Před 2 dny", 2),
            ("Přidáno před 5 dny", 5),
            ("3 days ago", 3),
            ("Před týdnem", 7),
            ("Před 2 týdny", 14),
            ("Před měsícem", 30),
        ],
    )
    def test_relative_and_badge_dates(self, text: str | None, expected: int | None) -> None:
        assert parse_age_days(text) == expected

    def test_iso_date(self) -> None:
        target = (date.today() - timedelta(days=4)).isoformat()
        assert parse_age_days(target) == 4

    def test_czech_day_month_date(self) -> None:
        target = date.today() - timedelta(days=5)
        cz_str = f"{target.day}.{target.month}."
        assert parse_age_days(cz_str) == 5

    def test_czech_full_date(self) -> None:
        target = date.today() - timedelta(days=2)
        cz_str = f"{target.day}. {target.month}. {target.year}"
        assert parse_age_days(cz_str) == 2


class TestParseSearchPage:
    def test_parses_real_dom_cards(self) -> None:
        listings = parse_search_page(SEARCH_PAGE_HTML, base_url="https://www.jobs.cz/prace/praha/")
        assert len(listings) == 3

        first = listings[0]
        assert first.job_id == "2001344585"
        assert first.title == "Python Developer"
        assert first.url == "https://www.jobs.cz/rpd/2001344585/?searchId=abc&rps=228"
        assert first.company == "Acme s.r.o."
        assert first.location == "Praha – Karlín"
        assert first.posted_text == "Přidáno před 2 hodinami"
        assert first.age_days == 0

        second = listings[1]
        assert second.job_id == "2001551557"
        assert second.posted_text == "Příležitost dne"
        assert second.age_days is None

        third = listings[2]
        assert third.job_id == "2000106458"
        assert third.company == "Gamma a.s."
        assert third.age_days == 10


class TestParseJobDetail:
    def test_parses_detail_fields(self) -> None:
        listing = JobListing(
            job_id="2001344585",
            title="Fallback Title",
            url="https://www.jobs.cz/rpd/2001344585/",
            company=None,
            location="Praha",
        )
        detail = parse_job_detail(DETAIL_PAGE_HTML, listing)
        assert detail.job_id == "2001344585"
        assert detail.title == "Senior Python Developer"
        assert detail.company == "Acme s.r.o."
        assert detail.location == "Praha"
        assert detail.salary == "90 000 – 140 000 Kč"
        assert "Hledáme zkušeného vývojáře." in detail.description
        assert "Python 3.11+" in detail.description


def _make_card_html(job_id: str, title: str, status: str) -> str:
    return f"""
    <article class="SearchResultCard">
      <header class="SearchResultCard__header">
        <h2 class="SearchResultCard__title">
          <a class="SearchResultCard__titleLink" data-jobad-id="{job_id}" href="/rpd/{job_id}/">{title}</a>
        </h2>
        <div class="SearchResultCard__status">{status}</div>
      </header>
      <footer class="SearchResultCard__footer">
        <ul class="SearchResultCard__footerList">
          <li class="SearchResultCard__footerItem"><span translate="no">Co</span></li>
          <li class="SearchResultCard__footerItem" data-test="serp-locality">Praha</li>
        </ul>
      </footer>
    </article>
    """


class TestJobsCzScraper:
    def test_scrape_search_filters_age_tracks_seen_streak_and_limit(self) -> None:
        page1 = (
            _make_card_html("1", "Fresh 1", "Dnes")
            + _make_card_html("2", "Too Old", "Před 5 dny")
            + _make_card_html("3", "Seen 1", "Dnes")
            + _make_card_html("4", "Fresh 2", "Včera")
        )
        page2 = (
            _make_card_html("5", "Seen 2", "Dnes")
            + _make_card_html("6", "Seen 3", "Dnes")
            + _make_card_html("7", "Never Reached", "Dnes")
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if "page=2" in str(request.url):
                return httpx.Response(200, text=page2)
            return httpx.Response(200, text=page1)

        seen_ids = {"3", "5", "6"}
        with JobsCzScraper(request_delay_sec=0.0, use_playwright_fallback=False) as scraper:
            scraper._client = httpx.Client(transport=httpx.MockTransport(handler))
            listings, total = scraper.scrape_search(
                "https://www.jobs.cz/prace/praha/",
                max_pages=5,
                max_age_days=3,
                is_seen_fn=lambda jid: jid in seen_ids,
                seen_streak_stop=2,
            )

        assert [item.job_id for item in listings] == ["1", "4"]
        # Page 1 had 4 cards; Page 2 processed 2 cards before hitting seen_streak=2,
        # while total_scraped_count counts all parsed cards on fetched pages (4 + 3 = 7).
        assert total == 7

    def test_scrape_search_respects_limit(self) -> None:
        page1 = (
            _make_card_html("1", "Job 1", "Dnes")
            + _make_card_html("2", "Job 2", "Dnes")
            + _make_card_html("3", "Job 3", "Dnes")
        )
        handler = lambda req: httpx.Response(200, text=page1)
        with JobsCzScraper(request_delay_sec=0.0, use_playwright_fallback=False) as scraper:
            scraper._client = httpx.Client(transport=httpx.MockTransport(handler))
            listings, total = scraper.scrape_search(
                "https://www.jobs.cz/prace/praha/",
                max_pages=3,
                limit=2,
            )
        assert [item.job_id for item in listings] == ["1", "2"]
        assert total == 3

    def test_playwright_fallback_on_403_and_empty_cards(self) -> None:
        handler = lambda req: httpx.Response(403, text="Forbidden")
        with JobsCzScraper(request_delay_sec=0.0, use_playwright_fallback=True) as scraper:
            scraper._client = httpx.Client(transport=httpx.MockTransport(handler))
            with patch.object(
                scraper,
                "_fetch_with_playwright",
                return_value=_make_card_html("99", "PW Job", "Dnes"),
            ) as mock_pw:
                listings, total = scraper.scrape_search(
                    "https://www.jobs.cz/prace/praha/",
                    max_pages=1,
                )
        assert mock_pw.call_count == 1
        assert len(listings) == 1
        assert listings[0].job_id == "99"
        assert total == 1

    def test_playwright_import_error_logs_warning_and_raises(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        real_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name.startswith("playwright"):
                raise ImportError("No module named 'playwright'")
            return real_import(name, *args, **kwargs)

        handler = lambda req: httpx.Response(403, text="Forbidden")
        with caplog.at_level(logging.WARNING):
            with JobsCzScraper(request_delay_sec=0.0, use_playwright_fallback=True) as scraper:
                scraper._client = httpx.Client(transport=httpx.MockTransport(handler))
                with patch.object(builtins, "__import__", side_effect=fake_import):
                    with pytest.raises(ScraperError, match="Playwright is not installed"):
                        scraper.scrape_search("https://www.jobs.cz/prace/praha/", max_pages=1)

        assert "playwright is not installed" in caplog.text.lower()

    def test_fetch_job_detail_falls_back_to_playwright_on_empty_spa(self) -> None:
        spa_html = "<html><body><main class='main'></main></body></html>"
        handler = lambda req: httpx.Response(200, text=spa_html)
        listing = JobListing(
            job_id="2000106458",
            title="SPA Role",
            url="https://rohde-schwarz.jobs.cz/detail-pozice?id=2000106458",
        )
        with JobsCzScraper(request_delay_sec=0.0, use_playwright_fallback=True) as scraper:
            scraper._client = httpx.Client(transport=httpx.MockTransport(handler))
            with patch.object(
                scraper,
                "_fetch_with_playwright",
                return_value=DETAIL_PAGE_HTML,
            ) as mock_pw:
                detail = scraper.fetch_job_detail(listing)

        assert mock_pw.call_count == 1
        assert detail.title == "Senior Python Developer"
        assert "Python 3.11+" in detail.description
