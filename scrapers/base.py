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
from tenacity import retry, stop_after_attempt, wait_exponential

from config.settings import settings


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
    raw_html: Optional[str] = None
    raw_json: Optional[str] = None
    extra: dict = field(default_factory=dict)


class BaseScraper(ABC):
    """
    Base class for all job board scrapers.

    Subclasses must implement:
        - `source_name` property
        - `scrape()` async generator
    """

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None

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

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    async def _fetch(self, url: str, **kwargs) -> httpx.Response:  # type: ignore[override]
        client = await self._get_client()
        await self._polite_delay()
        response = await client.get(url, **kwargs)
        response.raise_for_status()
        return response

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
