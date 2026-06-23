"""Publication-date extraction with explicit provenance and confidence."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup

from analyzer.freshness import parse_posted_at


@dataclass
class PublicationDate:
    value: datetime | None = None
    source: str = "unknown"
    raw: str | None = None
    confidence: float = 0.0


CONFIDENCE = {"api": 1.0, "json_ld": 0.95, "embedded_json": 0.9,
              "meta": 0.85, "html_time": 0.8, "html": 0.7,
              "relative_text": 0.5, "unknown": 0.0}


def publication(value: Any, source: str) -> PublicationDate:
    parsed, detected = parse_posted_at(value)
    if not parsed:
        return PublicationDate()
    actual_source = source or detected
    if detected == "relative_text":
        actual_source = "relative_text"
    return PublicationDate(parsed, actual_source, str(value)[:300], CONFIDENCE.get(actual_source, 0.0))


def _json_dates(value: Any):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {"dateposted", "date_published", "publicationdate", "publishedat"}:
                yield item
            yield from _json_dates(item)
    elif isinstance(value, list):
        for item in value:
            yield from _json_dates(item)


def extract_publication_from_html(html: str) -> PublicationDate:
    if not html:
        return PublicationDate()
    soup = BeautifulSoup(html, "lxml")
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
            for raw in _json_dates(payload):
                result = publication(raw, "json_ld")
                if result.value:
                    return result
        except (json.JSONDecodeError, TypeError):
            continue
    for attrs in (
        {"property": "article:published_time"}, {"name": "date"},
        {"name": "datePosted"}, {"itemprop": "datePosted"},
    ):
        element = soup.find("meta", attrs=attrs)
        if element and element.get("content"):
            result = publication(element["content"], "meta")
            if result.value:
                return result
    time_el = soup.select_one("time[datetime]")
    if time_el:
        result = publication(time_el.get("datetime"), "html_time")
        if result.value:
            return result
    selectors = "[class*='posted'], [class*='date'], [data-testid*='date']"
    for element in soup.select(selectors)[:10]:
        result = publication(element.get_text(" ", strip=True), "html")
        if result.value:
            return result
    return publication(soup.get_text(" ", strip=True)[:5000], "relative_text")
