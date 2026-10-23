"""Bounded, explicit source parsing; missing fields never mean an empty catalogue."""

import hashlib
import html
import json
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from lxml import etree

from ats_core.models import CataloguePage, SchemaError


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def json_object(text):
    try:
        return json.loads(text)
    except (ValueError, TypeError) as exc:
        raise SchemaError(
            "Expected vendor JSON, received malformed JSON or an HTML error page"
        ) from exc


def records(data, key=None):
    value = data.get(key) if key and isinstance(data, dict) else data if key is None else None
    if not isinstance(value, list):
        raise SchemaError(f"Catalogue is missing its explicit {key or 'root'} array")
    if not all(isinstance(item, dict) for item in value):
        raise SchemaError("Catalogue contains a non-object posting")
    return value


def identifier(value):
    if not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value).strip():
        raise SchemaError("Missing stable source posting ID")
    return str(value).strip()


def required(value, name):
    if not isinstance(value, str) or not value.strip():
        raise SchemaError(f"Missing {name}")
    return value.strip()


def public_link(value, base=""):
    value = required(value, "public vacancy link")
    result = urljoin(base, value)
    parsed = urlsplit(result)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise SchemaError("Invalid public vacancy link")
    if parsed.port not in (None, 80, 443):
        raise SchemaError("Nonstandard port in vacancy link")
    return result


def html_text(value):
    if not isinstance(value, str) or not value.strip():
        return ""
    soup = BeautifulSoup(html.unescape(value), "html.parser")
    for element in soup(["script", "style", "noscript"]):
        element.decompose()
    for element in soup.find_all(["br", "p", "div", "li", "h1", "h2", "h3", "h4", "section"]):
        element.insert_before("\n")
        element.insert_after("\n")
    lines = [" ".join(line.split()) for line in soup.get_text().splitlines()]
    return "\n\n".join(line for line in lines if line)


def sections(*values):
    return "\n\n".join(part for value in values if (part := html_text(value)))


def normalized(value):
    return " ".join(html_text(value).casefold().split())


def timestamp(value, *, legacy_locale=None):
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        date = datetime.fromisoformat(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
        except (ValueError, TypeError):
            if not legacy_locale:
                return None
            try:
                fmt = "%m/%d/%Y" if legacy_locale.lower().startswith("en_us") else "%d/%m/%Y"
                date = datetime.strptime(value, fmt)
            except ValueError:
                return None
    return date.replace(tzinfo=UTC).isoformat() if date.tzinfo is None else date.isoformat()


def country_code(value):
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if value.casefold() in {"switzerland", "schweiz", "suisse", "svizzera", "ch"}:
        return "CH"
    return value.upper() if re.fullmatch("[A-Za-z]{2}", value) else None


def location(value):
    if isinstance(value, str):
        raw = value.strip()
        tail = re.split(r"[,;]", raw)[-1].strip()
        swiss = tail.casefold() in {"switzerland", "schweiz", "suisse", "svizzera", "ch"}
        return {"raw": raw, "city": None, "country": "CH" if swiss else None}
    if not isinstance(value, dict):
        return {"raw": "", "city": None, "country": None}
    address = value.get("postalAddress") or value.get("address") or value
    if not isinstance(address, dict):
        return location(str(address))
    city = address.get("city") or address.get("addressLocality")
    country = (
        address.get("countryCode")
        or address.get("country_code")
        or address.get("country")
        or address.get("addressCountry")
    )
    if isinstance(country, dict):
        country = country.get("name")
    raw = (
        value.get("raw")
        or value.get("location")
        or value.get("name")
        or ", ".join(
            str(v) for v in [city, address.get("region") or address.get("state"), country] if v
        )
    )
    return {
        "raw": str(raw),
        "city": city,
        "country": country_code(country) or location(str(raw))["country"],
    }


def locations(*values):
    result = []
    for value in values:
        for item in value if isinstance(value, list) else [value]:
            loc = location(item)
            if loc["raw"] and loc not in result:
                result.append(loc)
    return result


def workplace(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        value = re.sub(r"[\s_-]", "", value.casefold())
        if value in {"remote", "fullyremote", "100%remote"}:
            return True
        if value in {"onsite", "office", "inperson"}:
            return False
    return None


def xml_root(text, expected):
    if not isinstance(text, str) or re.search(r"<!\s*(DOCTYPE|ENTITY)", text, re.IGNORECASE):
        raise SchemaError("DTD and entity declarations are not supported in public job feeds")
    try:
        root = etree.fromstring(
            text.encode(),
            parser=etree.XMLParser(
                resolve_entities=False,
                load_dtd=False,
                no_network=True,
                huge_tree=False,
                recover=False,
            ),
        )
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise SchemaError("Malformed vendor XML feed") from exc
    if etree.QName(root).localname not in expected:
        raise SchemaError("Response is not the expected job feed")
    return root


def xml_fields(element):
    return {etree.QName(child).localname: "".join(child.itertext()).strip() for child in element}


def catalogue(items, make_entry, **kwargs):
    page = CataloguePage([], **kwargs)
    for item in items:
        try:
            page.entries.append(make_entry(item))
        except (SchemaError, KeyError, TypeError, ValueError) as exc:
            page.diagnostics.append(f"Malformed catalogue record: {exc}"[:300])
    return page
