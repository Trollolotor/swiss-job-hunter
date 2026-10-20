"""
Scraper for jobup.ch — leading job board for French-speaking Switzerland.

jobup.ch is JobCloud's sister site to jobs.ch and serves the same search page:
a JSON-LD ItemList plus result cards keyed by vacancy UUID, the same
wrap-back-to-page-1 past the last page, and the same detail-page markup. So
this reuses the jobs.ch scraper and only swaps the URLs.

It used to call the public search API (job-search-api.jobup.ch), which still
answers 200 but now ignores `term` and returns the same unfiltered feed for
every query (#26).
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional
from scrapers.base import ScrapedJob

from scrapers.jobs_ch import JobsChScraper


class JobupChScraper(JobsChScraper):
    source_name = "jobup.ch"
    search_url = "https://www.jobup.ch/en/jobs/"
    detail_url = "https://www.jobup.ch/en/jobs/detail/"

    def _parse(self, doc: dict) -> Optional[ScrapedJob]:
        try:
            title = doc.get("title", "").strip()
            company = (doc.get("company") or {}).get("name", "Unknown").strip()
            place = doc.get("place", "")
            location = (place if isinstance(place, str) else (place or {}).get("name", "Switzerland")).strip() or "Switzerland"

            description = doc.get("teaser", "") or doc.get("description", "")
            slug = doc.get("slug", "") or str(doc.get("id", ""))
            url = f"https://www.jobup.ch/en/jobs/detail/{slug}/"

            posted_at: Optional[datetime] = None
            if ts := doc.get("publication_date"):
                try:
                    posted_at = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    pass

            return ScrapedJob(
                title=title,
                company=company,
                location=location,
                description=description,
                url=url,
                source=self.source_name,
                source_job_id=str(doc.get("id", "")),
                posted_at=posted_at,
                posted_at_source="api",
                raw_json=json.dumps(doc, ensure_ascii=False),
            )
        except Exception as exc:
            print(f"[jobup.ch] parse error: {exc}")
            return None
