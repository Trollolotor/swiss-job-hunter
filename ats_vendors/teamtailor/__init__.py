"""Teamtailor RSS, including all namespaced locations and unambiguous remote flags."""

import re
from urllib.parse import urlsplit

from ats_core import Entry, ParsedPosting, Portal, Request
from ats_core.parsing import (
    catalogue,
    html_text,
    identifier,
    locations,
    public_link,
    required,
    timestamp,
    xml_fields,
    xml_root,
)


def detect(url, html=""):
    p = urlsplit(url)
    if not p.hostname or not p.hostname.endswith(".teamtailor.com"):
        return None
    tenant = p.hostname.removesuffix(".teamtailor.com")
    if "." in tenant and not tenant.endswith(".na"):
        return None
    return Portal("teamtailor", tenant, f"https://{p.hostname}", f"https://{p.hostname}")


def catalogue_request(source, offset=0):
    return Request(source.origin + "/jobs.rss")


def parse_catalogue(source, text):
    root = xml_root(text, {"rss"})
    if root.find("channel") is None:
        from ats_core import SchemaError

        raise SchemaError("RSS catalogue has no channel")
    items = []
    for item in root.findall("./channel/item"):
        data = xml_fields(item)
        data["locations"] = [
            xml_fields(node)
            for node in item.xpath('./*[local-name()="locations"]/*[local-name()="location"]')
        ]
        items.append(data)

    def entry(item):
        link = public_link(item.get("link"))
        match = re.search(r"/jobs/(\d+)", link)
        return Entry(identifier(match.group(1) if match else item.get("guid")), link, item)

    return catalogue(items, entry)


def detail_request(source, entry):
    return None


def parse_posting(source, entry, detail=None):
    item = entry.data
    remote = {"fully": True, "none": False}.get(item.get("remoteStatus"))
    return ParsedPosting(
        entry.posting_id,
        required(item.get("title"), "title"),
        source.company or source.tenant,
        entry.detail_url,
        description=html_text(item.get("description")),
        locations=locations(item.get("locations")),
        posted_at=timestamp(item.get("pubDate")),
        remote_ok=remote,
        extra={
            "department": item.get("department"),
            "role": item.get("role"),
            "workplace_type": item.get("remoteStatus"),
        },
    )
