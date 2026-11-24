"""Web scraper for jobs.cz search results and job detail pages."""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from datetime import date, datetime
from typing import Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup, Tag

from najdi_praci.models import JobDetail, JobListing

logger = logging.getLogger(__name__)

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
}

_JOB_ID_URL_RE = re.compile(r"/(?:rpd|fp/[^/]+)/(\d+)")
_JOB_ID_BETA_RE = re.compile(r"/nabidka/([0-9a-fA-F-]+)")
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_CZ_DATE_RE = re.compile(r"\b(\d{1,2})\.\s*(\d{1,2})\.(?:\s*(\d{4}))?")
_SALARY_RE = re.compile(r"\d[\d\s\u00a0\u200d]*\s*(?:Kč|CZK|EUR|€)", re.IGNORECASE)


class ScraperError(Exception):
    """Raised when scraping jobs.cz fails unrecoverably."""


def _clean_text(text: str | None) -> str:
    """Normalize whitespace, non-breaking spaces, and zero-width characters."""
    if not text:
        return ""
    cleaned = text.replace("\u00a0", " ").replace("\u200d", "").replace("\u200b", "")
    cleaned = unicodedata.normalize("NFKC", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def build_page_url(search_url: str, page: int) -> str:
    """Build a paginated jobs.cz search URL preserving existing query params."""
    parsed = urlparse(search_url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if page <= 1:
        query.pop("page", None)
    else:
        query["page"] = [str(page)]
    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def parse_age_days(posted_text: str | None) -> int | None:
    """Parse Czech/English relative or absolute posting date text into age in days."""
    if not posted_text:
        return None

    text = _clean_text(posted_text).lower()
    if not text:
        return None

    # Ignore promotional badges and future expiration notices (e.g. "Končí za 3 dny").
    if any(
        marker in text
        for marker in (
            "příležitost dne",
            "končí za",
            "ends in",
            "top nabídka",
            "nová příležitost",
        )
    ):
        return None

    if any(
        marker in text
        for marker in (
            "dnes",
            "today",
            "nové",
            "new",
            "právě teď",
            "just now",
            "před chvilkou",
        )
    ):
        return 0

    if "včera" in text or "yesterday" in text:
        return 1

    # Minutes or hours ago -> today (0 days old).
    if re.search(
        r"(?:před\s+\d+\s*(?:minut|hodin)|před\s+(?:minutou|hodinou)|"
        r"\d+\s*(?:minute|minutes|min|hour|hours|hr)\s+ago)",
        text,
    ):
        return 0

    # Days ago: "Před 2 dny", "Přidáno před 1 dnem", "2 days ago".
    m_days = re.search(
        r"(?:před\s+(\d+)\s*(?:dn[eyů]|dnem)|(\d+)\s*days?\s+ago)",
        text,
    )
    if m_days:
        val = m_days.group(1) or m_days.group(2)
        return int(val)

    # Weeks ago: "Před týdnem", "Před 2 týdny", "2 weeks ago".
    if re.search(r"před\s+týdnem|a\s+week\s+ago", text):
        return 7
    m_weeks = re.search(
        r"(?:před\s+(\d+)\s*týdn[eyů]?|(\d+)\s*weeks?\s+ago)",
        text,
    )
    if m_weeks:
        val = m_weeks.group(1) or m_weeks.group(2)
        return int(val) * 7

    # Months ago: "Před měsícem", "Před 2 měsíci", "2 months ago".
    if re.search(r"před\s+měsícem|a\s+month\s+ago", text):
        return 30
    m_months = re.search(
        r"(?:před\s+(\d+)\s*měsíc[eiů]?|(\d+)\s*months?\s+ago)",
        text,
    )
    if m_months:
        val = m_months.group(1) or m_months.group(2)
        return int(val) * 30

    today = date.today()

    # ISO date: YYYY-MM-DD
    m_iso = _ISO_DATE_RE.search(text)
    if m_iso:
        try:
            parsed_dt = datetime.strptime(m_iso.group(0), "%Y-%m-%d").date()
            return max(0, (today - parsed_dt).days)
        except ValueError:
            pass

    # Czech date: D.M. or D.M.YYYY
    m_cz = _CZ_DATE_RE.search(text)
    if m_cz:
        day = int(m_cz.group(1))
        month = int(m_cz.group(2))
        year = int(m_cz.group(3)) if m_cz.group(3) else today.year
        try:
            parsed_dt = date(year, month, day)
            if not m_cz.group(3) and parsed_dt > today:
                parsed_dt = date(year - 1, month, day)
            return max(0, (today - parsed_dt).days)
        except ValueError:
            pass

    return None


def _extract_job_id(card: Tag, title_link: Tag, href: str) -> str | None:
    """Extract numeric/string job_id from card attributes or URL."""
    for attr in ("data-jobad-id", "data-ad-id", "data-jobad-recruitment-process-id"):
        val = title_link.get(attr) or card.get(attr)
        if val:
            return str(val).strip()

    fav_link = card.select_one("a[data-ad-id]")
    if fav_link and fav_link.get("data-ad-id"):
        return str(fav_link["data-ad-id"]).strip()

    card_id = str(card.get("id") or "").strip()
    if card_id.startswith("advert-") and len(card_id) > len("advert-"):
        return card_id[len("advert-") :]

    m = _JOB_ID_URL_RE.search(href)
    if m:
        return m.group(1)

    m_beta = _JOB_ID_BETA_RE.search(href)
    if m_beta:
        return m_beta.group(1)

    return None


def _extract_beta_labeled_field(card: Tag, label_prefix: str) -> str | None:
    """Extract a labeled metadata value from beta.jobs.cz JobCard <li> items."""
    prefix_lower = label_prefix.lower()
    for li in card.select("li"):
        hidden = li.select_one(".accessibility-hidden")
        if not hidden:
            continue
        label_text = _clean_text(hidden.get_text(" ", strip=True)).lower()
        if label_text.startswith(prefix_lower):
            for span in li.find_all("span", recursive=False):
                if "accessibility-hidden" in (span.get("class") or []):
                    continue
                val = _clean_text(span.get_text(" ", strip=True))
                if val:
                    return val
    return None


def parse_search_page(html: str, base_url: str = "https://www.jobs.cz") -> list[JobListing]:
    """Parse job cards from a jobs.cz search results HTML page (classic or beta DOM)."""
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("article.SearchResultCard")
    if not cards:
        cards = [
            art
            for art in soup.select("article")
            if art.select_one(
                "a[data-jobad-id], a.SearchResultCard__titleLink, a[data-testid='advert-link']"
            )
        ]

    listings: list[JobListing] = []
    seen_ids: set[str] = set()

    for card in cards:
        title_link = (
            card.select_one("a.SearchResultCard__titleLink")
            or card.select_one("a[data-jobad-id]")
            or card.select_one("a[data-testid='advert-link']")
        )
        if not title_link:
            continue

        href = str(title_link.get("href") or "").strip()
        if not href:
            continue

        job_id = _extract_job_id(card, title_link, href)
        if not job_id or job_id in seen_ids:
            continue

        title_el = card.select_one("h2.SearchResultCard__title") or card.select_one(
            "[data-testid='job-card-title']"
        )
        raw_title = (
            title_link.get_text(" ", strip=True)
            or (title_el.get("data-test-ad-title") if title_el else "")
            or (title_el.get_text(" ", strip=True) if title_el else "")
        )
        title = _clean_text(str(raw_title))
        if not title:
            continue

        url = urljoin(base_url, href)

        company: str | None = None
        comp_span = card.select_one(".SearchResultCard__footerItem span[translate]")
        if comp_span:
            company = _clean_text(comp_span.get_text(" ", strip=True)) or None
        else:
            company = _extract_beta_labeled_field(card, "název firmy")
        if not company:
            for item in card.select(".SearchResultCard__footerItem"):
                if item.get("data-test") == "serp-locality":
                    continue
                if item.select_one("a[data-test='serp-atmoskop']"):
                    continue
                text = _clean_text(item.get_text(" ", strip=True))
                if text:
                    company = text
                    break

        loc_el = card.select_one("[data-test='serp-locality']")
        location = _clean_text(loc_el.get_text(" ", strip=True)) if loc_el else None
        if not location:
            location = _extract_beta_labeled_field(card, "lokalita")
        if location == "":
            location = None

        status_el = card.select_one(".SearchResultCard__status") or card.select_one(
            "footer li span"
        )
        posted_text = _clean_text(status_el.get_text(" ", strip=True)) if status_el else None
        if posted_text == "":
            posted_text = None

        age_days = parse_age_days(posted_text)

        seen_ids.add(job_id)
        listings.append(
            JobListing(
                job_id=job_id,
                title=title,
                url=url,
                company=company,
                location=location,
                posted_text=posted_text,
                age_days=age_days,
            )
        )

    return listings


def parse_job_detail(html: str, listing: JobListing) -> JobDetail:
    """Parse full job details from a jobs.cz job posting HTML page."""
    soup = BeautifulSoup(html, "html.parser")

    h1 = soup.select_one(".JobDescriptionHeading h1") or soup.select_one("h1")
    title = _clean_text(h1.get_text(" ", strip=True)) if h1 else ""
    if not title:
        title = listing.title

    company = listing.company
    if not company:
        logo_img = soup.select_one(".CompanyLogo img[alt]")
        if logo_img and logo_img.get("alt"):
            company = _clean_text(str(logo_img["alt"])) or None

    location = listing.location

    salary: str | None = None
    for candidate in soup.select(
        ".IconWithText, .Tag, p.typography-body-medium-text-regular, [data-test='jd-salary']"
    ):
        txt = _clean_text(candidate.get_text(" ", strip=True))
        if txt and len(txt) <= 80 and _SALARY_RE.search(txt):
            salary = txt
            break

    body_el = (
        soup.select_one(".RichContent")
        or soup.select_one("[data-test='jd-body']")
        or soup.select_one(".JobDetail")
        or soup.select_one("main")
        or soup.select_one("article")
    )

    description = ""
    if body_el:
        raw_desc = body_el.get_text("\n", strip=True)
        lines = [_clean_text(line) for line in raw_desc.splitlines()]
        description = "\n".join(line for line in lines if line)

    return JobDetail(
        job_id=listing.job_id,
        title=title,
        url=listing.url,
        company=company,
        location=location,
        salary=salary,
        description=description,
    )


class JobsCzScraper:
    """HTTP + Playwright-fallback scraper for jobs.cz."""

    def __init__(
        self,
        request_delay_sec: float = 1.0,
        use_playwright_fallback: bool = True,
        timeout_sec: float = 20.0,
    ) -> None:
        self.request_delay_sec = max(0.0, request_delay_sec)
        self.use_playwright_fallback = use_playwright_fallback
        self.timeout_sec = timeout_sec
        self._client = httpx.Client(
            headers=_DEFAULT_HEADERS,
            follow_redirects=True,
            timeout=timeout_sec,
        )

    def __enter__(self) -> JobsCzScraper:
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def _fetch_with_playwright(self, url: str) -> str:
        """Fetch rendered HTML using Playwright headless Chromium."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            logger.warning(
                "Playwright fallback requested for %s, but playwright is not installed: %s",
                url,
                exc,
            )
            raise ScraperError(
                f"Playwright is not installed for fallback on {url}"
            ) from exc

        logger.info("Fetching %s via Playwright Chromium fallback", url)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    page = browser.new_page(
                        user_agent=_DEFAULT_HEADERS["User-Agent"],
                        locale="cs-CZ",
                    )
                    page.goto(
                        url,
                        wait_until="networkidle",
                        timeout=int(self.timeout_sec * 1000),
                    )
                    return page.content()
                finally:
                    browser.close()
        except Exception as exc:
            raise ScraperError(f"Playwright failed for {url}: {exc}") from exc

    def _fetch_html(self, url: str) -> tuple[str, bool]:
        """Fetch HTML with httpx; return (html, used_playwright) and track final URL."""
        self._last_url = url
        try:
            response = self._client.get(url)
        except httpx.HTTPError as exc:
            if self.use_playwright_fallback:
                logger.warning("HTTP error fetching %s (%s); trying Playwright", url, exc)
                return self._fetch_with_playwright(url), True
            raise ScraperError(f"HTTP error fetching {url}: {exc}") from exc

        if response.status_code in (403, 429):
            if self.use_playwright_fallback:
                logger.warning(
                    "HTTP %s for %s; falling back to Playwright",
                    response.status_code,
                    url,
                )
                return self._fetch_with_playwright(url), True
            raise ScraperError(f"Blocked with HTTP {response.status_code} for {url}")

        if response.status_code >= 400:
            raise ScraperError(f"HTTP {response.status_code} fetching {url}")

        self._last_url = str(response.url) if getattr(response, "url", None) else url
        return response.text, False

    def scrape_search(
        self,
        search_url: str,
        max_pages: int = 20,
        max_age_days: int | None = 3,
        is_seen_fn: Callable[[str], bool] | None = None,
        seen_streak_stop: int = 15,
        limit: int | None = None,
    ) -> tuple[list[JobListing], int]:
        """Scrape paginated search results, applying age, seen-streak, and limit filters."""
        new_listings: list[JobListing] = []
        total_scraped_count = 0
        seen_streak = 0

        for page_num in range(1, max_pages + 1):
            if page_num > 1 and self.request_delay_sec > 0:
                time.sleep(self.request_delay_sec)

            page_url = build_page_url(search_url, page_num)
            html, used_pw = self._fetch_html(page_url)
            resolved_base_url = getattr(self, "_last_url", page_url) or page_url
            cards = parse_search_page(html, base_url=resolved_base_url)

            if not cards and not used_pw and self.use_playwright_fallback:
                logger.warning(
                    "No job cards parsed on page %d (%s); retrying with Playwright",
                    page_num,
                    page_url,
                )
                try:
                    html = self._fetch_with_playwright(page_url)
                    cards = parse_search_page(html, base_url=page_url)
                except ScraperError as exc:
                    logger.warning("Playwright retry failed on page %d: %s", page_num, exc)

            if not cards:
                logger.info("No cards found on page %d; stopping pagination.", page_num)
                break

            total_scraped_count += len(cards)
            stop_early = False

            for listing in cards:
                if is_seen_fn is not None and is_seen_fn(listing.job_id):
                    seen_streak += 1
                    if seen_streak >= seen_streak_stop:
                        logger.info(
                            "Reached seen_streak=%d (>= %d); stopping pagination early.",
                            seen_streak,
                            seen_streak_stop,
                        )
                        stop_early = True
                        break
                    continue

                seen_streak = 0

                if (
                    listing.age_days is not None
                    and max_age_days is not None
                    and listing.age_days > max_age_days
                ):
                    continue

                new_listings.append(listing)
                if limit is not None and len(new_listings) >= limit:
                    logger.info("Reached limit=%d new listings; stopping.", limit)
                    stop_early = True
                    break

            if stop_early:
                break

        return new_listings, total_scraped_count

    def fetch_job_detail(self, listing: JobListing) -> JobDetail:
        """Fetch and parse the full job detail page for a listing."""
        if self.request_delay_sec > 0:
            time.sleep(self.request_delay_sec)

        html, used_pw = self._fetch_html(listing.url)
        detail = parse_job_detail(html, listing)

        if not detail.description and not used_pw and self.use_playwright_fallback:
            logger.warning(
                "Empty job description for %s (%s); trying Playwright fallback",
                listing.job_id,
                listing.url,
            )
            try:
                pw_html = self._fetch_with_playwright(listing.url)
                detail = parse_job_detail(pw_html, listing)
            except ScraperError as exc:
                logger.warning(
                    "Playwright detail fallback failed for %s: %s",
                    listing.job_id,
                    exc,
                )

        return detail
