"""
Scraper for jobs.ch — Switzerland's largest job board.
Uses their public JSON API (no authentication required).
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import AsyncGenerator, Optional
from urllib.parse import urlencode

from scrapers.base import BaseScraper, ScrapedJob

# jobs.ch internal API endpoint (reverse-engineered from XHR)
_API_BASE = "https://www.jobs.ch/api/v1/public/search/"
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
_PAGE_SIZE = 20


class JobsChScraper(BaseScraper):
    source_name = "jobs.ch"

    async def scrape(
        self, keyword: str, location: str = "Zürich", max_pages: int = 10
    ) -> AsyncGenerator[ScrapedJob, None]:
        """
        Paginate through jobs.ch search results.

        jobs.ch API params:
            query       — keyword
            location    — city name
            page        — 1-based
            num_pages   — results per page (max 20)
        """
        for page in range(1, max_pages + 1):
            params = {
                "query": keyword,
                "page": page,
                "num_pages": _PAGE_SIZE,
                "sort": "date",
            }
            if location:
                params["location"] = location
            url = f"{_API_BASE}?{urlencode(params)}"

            try:
                resp = await self._fetch(url)
                data = resp.json()
            except Exception as exc:
                print(f"[jobs.ch] page {page} error: {exc}")
                break

            documents = data.get("documents", [])
            if not documents:
                break

            for doc in documents:
                job = self._parse_document(doc)
                if job:
                    yield job

            total = data.get("total_hits", 0)
            num_pages = data.get("num_pages", 1)
            if not total or page >= num_pages:
                break

    def _parse_document(self, doc: dict) -> Optional[ScrapedJob]:
        try:
            title = doc.get("title", "").strip()

            # company_name is a flat string (not a nested dict)
            company = doc.get("company_name", "").strip()

            # place is a flat string; regions is a list of dicts with "name"
            place = doc.get("place", "")
            regions = doc.get("regions") or []
            region_name = regions[0].get("name", "") if regions else ""
            location = ", ".join(filter(None, [place, region_name])) or "Switzerland"
            # Normalize common variants → consistent spelling
            location = location.replace("Zurich", "Zürich").replace(", CH", "").strip()

            # preview is the teaser/snippet; full description not in list response
            description = doc.get("preview", "") or doc.get("description", "")

            slug = doc.get("slug", "")
            job_id = doc.get("job_id", doc.get("datapool_id", ""))
            # slug sometimes has job title appended after the UUID; strip it
            m = _UUID_RE.search(slug)
            clean_id = m.group(0) if m else (slug or job_id)
            job_url = f"https://www.jobs.ch/en/vacancies/detail/{clean_id}/" if clean_id else ""

            # employment_grades is a list of percentages e.g. [80, 85, 90, 95, 100]
            salary_raw: Optional[str] = None
            employment_type: Optional[str] = None
            grades = doc.get("employment_grades") or []
            if grades:
                mn, mx = min(grades), max(grades)
                employment_type = f"{mn}–{mx}%" if mn != mx else f"{mx}%"

            # Posted date
            posted_at: Optional[datetime] = None
            if ts := doc.get("publication_date"):
                try:
                    posted_at = datetime.fromisoformat(ts)
                except ValueError:
                    pass

            return ScrapedJob(
                title=title,
                company=company,
                location=location,
                description=description,
                url=job_url,
                source=self.source_name,
                source_job_id=job_id,
                salary_raw=salary_raw,
                employment_type=employment_type,
                posted_at=posted_at,
                posted_at_source="api",
                raw_json=json.dumps(doc, ensure_ascii=False),
            )
        except Exception as exc:
            print(f"[jobs.ch] parse error: {exc}")
            return None

    async def fetch_full_description(self, job_id: str) -> Optional[tuple[str, str]]:
        """
        Fetch full description and canonical URL from the HTML detail page.
        Returns (description, canonical_url), empty tuple () for 404, or None on error.
        """
        from bs4 import BeautifulSoup
        url = f"https://www.jobs.ch/en/vacancies/detail/{job_id}/"
        try:
            resp = await self._fetch(url)
            if resp.status_code in (404, 410):
                return ()  # type: ignore  # job taken down
            soup = BeautifulSoup(resp.text, "lxml")

            # Get canonical URL (contains full slug)
            canonical = ""
            canon_el = soup.select_one("link[rel='canonical']")
            if canon_el:
                canonical = canon_el.get("href", "")

            # Primary selector confirmed via inspection
            el = soup.select_one('[data-cy="vacancy-description"]')
            if el:
                return el.get_text(separator="\n", strip=True), canonical

            # Fallbacks
            for sel in ["div[class*='description']", "article", "main"]:
                el = soup.select_one(sel)
                if el:
                    text = el.get_text(separator="\n", strip=True)
                    if len(text) > 200:
                        return text, canonical

            return None
        except Exception as exc:
            print(f"[jobs.ch] detail fetch error for {job_id}: {exc}")
            return None
