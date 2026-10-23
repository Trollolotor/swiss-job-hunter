"""Plain value objects: this package has no application, credential, or database imports."""

from dataclasses import asdict, dataclass, field


class SchemaError(ValueError):
    """Observed content does not satisfy the expected public vendor protocol."""


@dataclass(frozen=True)
class Portal:
    vendor: str
    tenant: str
    origin: str
    portal_url: str
    board: str = "default"
    variant: str = "default"
    locale: str = ""
    company: str = ""

    def config(self):
        return asdict(self)


@dataclass(frozen=True)
class Request:
    url: str
    method: str = "GET"
    body: dict | None = None


@dataclass
class Entry:
    posting_id: str
    detail_url: str
    data: dict
    variant: str = ""
    inline: bool = True

    @property
    def identity(self):
        return self.posting_id, self.variant


@dataclass
class CataloguePage:
    entries: list[Entry]
    total: int | None = None
    next_offset: int | None = None
    termination: str = "next_absent"
    diagnostics: list[str] = field(default_factory=list)


@dataclass
class ParsedPosting:
    posting_id: str
    title: str
    company: str
    url: str
    description: str = ""
    variant: str = ""
    requisition_id: str | None = None
    locations: list[dict] = field(default_factory=list)
    application_url: str | None = None
    posted_at: str | None = None
    source_updated_at: str | None = None
    employment_type: str | None = None
    remote_ok: bool | None = None
    remote_countries: list[str] = field(default_factory=list)
    salary_raw: str | None = None
    extra: dict = field(default_factory=dict)

    def wire(self, vendor, evidence):
        location = " | ".join(dict.fromkeys(v["raw"] for v in self.locations if v.get("raw")))
        return {
            "posting_id": self.posting_id,
            "variant": self.variant,
            "requisition_id": self.requisition_id,
            "locations": self.locations,
            "remote_countries": self.remote_countries,
            "application_url": self.application_url or self.url,
            "source_updated_at": self.source_updated_at,
            "evidence": list(dict.fromkeys(evidence)),
            "job": {
                "title": self.title,
                "company": self.company,
                "description": self.description,
                "url": self.url,
                "source": vendor,
                "source_job_id": self.posting_id,
                "location": location,
                "posted_at": self.posted_at,
                "posted_at_source": "api" if self.posted_at else "unknown",
                "employment_type": self.employment_type,
                "remote_ok": self.remote_ok,
                "salary_raw": self.salary_raw,
                "extra": {
                    **self.extra,
                    "description_quality": "complete" if self.description else "insufficient",
                },
            },
        }
