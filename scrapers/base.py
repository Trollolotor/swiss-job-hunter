"""
Abstract base scraper — all site-specific scrapers inherit from this.
"""
from __future__ import annotations

import asyncio
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncGenerator, Optional

import httpx

from config.settings import settings


class PermanentHTTPError(httpx.HTTPStatusError):
    @property
    def status_code(self):
        return self.response.status_code


@dataclass
class ScrapedJob:
    """
    Intermediate job record as returned by a scraper.
    Gets normalized and stored in RawJob → Job pipeline.
    """
    title: str
    company: str
    location: str
    description: str
    url: str
    source: str

    source_job_id: Optional[str] = None
    salary_raw: Optional[str] = None
    employment_type: Optional[str] = None
    remote_ok: Optional[bool] = None
    language_required: Optional[str] = None
    posted_at: Optional[datetime] = None
    posted_at_source: str = "unknown"
    posted_at_raw: Optional[str] = None
    posted_at_confidence: float = 0.0
    raw_html: Optional[str] = None
    raw_json: Optional[str] = None
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.posted_at and not self.posted_at_raw:
            self.posted_at_raw = self.posted_at.isoformat()
        if self.posted_at and not self.posted_at_confidence:
            self.posted_at_confidence = {
                "api": 1.0, "json_ld": .95, "embedded_json": .9, "meta": .85,
                "html_time": .8, "html": .7, "relative_text": .5,
            }.get(self.posted_at_source, 0.0)


class BaseScraper(ABC):
    """
    Base class for all job board scrapers.

    Subclasses must implement:
        - `source_name` property
        - `scrape()` async generator
    """

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None
        self.report = {"requests": 0, "pages": 0, "errors": [], "termination": None}

    def end(self, reason, *, empty=False):
        self.report["termination"] = reason
        self.report["verified_empty"] = empty

    def _page_error(self, page, exc, yielded=0):
        self.report["errors"].append(f"page {page}: {type(exc).__name__}: {exc}"[:500])
        if not yielded:
            raise exc

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Human-readable source identifier e.g. 'jobs.ch'"""
        ...

    @abstractmethod
    async def scrape(
        self, keyword: str, location: str, max_pages: int
    ) -> AsyncGenerator[ScrapedJob, None]:
        """Yield ScrapedJob instances for a keyword + location search."""
        ...

    # ── HTTP helpers ───────────────────────────────────────────────────────────

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
                },
                timeout=30.0,
                follow_redirects=True,
            )
        return self._client

    async def _fetch(self, url: str, *, allow_status=None, **kwargs) -> httpx.Response:
        client = await self._get_client()
        for attempt in range(3):
            await self._polite_delay()
            self.report["requests"] += 1
            try:
                response = await client.get(url, **kwargs)
                self.last_response = response
                self.report["last_status"] = response.status_code
                if response.status_code in (allow_status or set()):
                    return response
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    raise PermanentHTTPError(f"HTTP {response.status_code}: {url}",
                                             request=response.request, response=response)
                response.raise_for_status()
                return response
            except PermanentHTTPError as exc:
                self.report["errors"].append(str(exc)[:500])
                raise
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if attempt == 2:
                    self.report["errors"].append(str(exc)[:500])
                    raise
                await asyncio.sleep(2 ** attempt)

    async def _polite_delay(self) -> None:
        """Random delay to avoid hammering servers."""
        delay = random.uniform(settings.scraper_delay_min, settings.scraper_delay_max)
        await asyncio.sleep(delay)

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def __aenter__(self) -> "BaseScraper":
        return self

    async def __aexit__(self, *_) -> None:  # type: ignore[override]
        await self.close()
