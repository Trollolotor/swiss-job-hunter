"""Versioned wire contracts shared by collectors and trusted validation."""

import hashlib
import json
from datetime import datetime
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from scrapers.base import ScrapedJob

SDK_VERSION = "1"
LATEST_SDK_VERSION = "2"

if TYPE_CHECKING:
    from employers.network import CollectionContext


class EmployerCollector(Protocol):
    async def __call__(self, config: dict, context: "CollectionContext") -> "CollectionResult": ...


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()
    ).hexdigest()


class Location(BaseModel):
    country: str | None = None
    city: str | None = None
    raw: str = ""

    @field_validator("country")
    @classmethod
    def country_code(cls, value):
        if value and value.casefold().strip() in {"switzerland", "schweiz", "suisse", "svizzera", "ch"}:
            return "CH"
        return value.upper() if value else None


class CollectedPosting(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job: ScrapedJob
    posting_id: str = Field(min_length=1, max_length=2000)
    variant: str = Field(default="", max_length=200)
    requisition_id: str | None = None
    locations: list[Location] = Field(default_factory=list)
    remote_countries: list[str] = Field(default_factory=list)
    application_url: str | None = None
    source_updated_at: datetime | None = None
    evidence: list[str] = Field(default_factory=list)

    @property
    def identity(self):
        return json.dumps([self.posting_id, self.variant], separators=(",", ":"))


class CollectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    postings: list[CollectedPosting] = Field(default_factory=list, max_length=10000)
    observed_ids: list[str] = Field(default_factory=list, max_length=10000)
    observed_identities: list[str] = Field(default_factory=list, max_length=10000)
    reported_total: int | None = Field(default=None, ge=0)
    scope: str = "all"
    termination: Literal["total", "next_absent", "explicit_empty", "incomplete"] = "incomplete"
    checkpoint: dict = Field(default_factory=dict)
    diagnostics: list[str] = Field(default_factory=list)


class CatalogueEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    posting_id: str = Field(min_length=1, max_length=2000)
    variant: str = Field(default="", max_length=200)
    detail_url: str = Field(min_length=1, max_length=2000)
    change_token: str | None = Field(default=None, max_length=1000)
    metadata: dict = Field(default_factory=dict)

    @property
    def identity(self):
        return json.dumps([self.posting_id, self.variant], separators=(",", ":"))


class CatalogueResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entries: list[CatalogueEntry] = Field(default_factory=list, max_length=10000)
    observed_ids: list[str] = Field(default_factory=list, max_length=10000)
    reported_total: int | None = Field(default=None, ge=0)
    scope: str = "all"
    termination: Literal["total", "next_absent", "explicit_empty", "incomplete"] = "incomplete"
    diagnostics: list[str] = Field(default_factory=list)


def in_coverage(posting: CollectedPosting, mode: str) -> bool:
    return (
        mode == "worldwide"
        or any((location.country or "").upper() == "CH" for location in posting.locations)
        or (mode == "swiss_remote" and "CH" in [c.upper() for c in posting.remote_countries])
    )
