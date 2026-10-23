"""Closed registry of reviewed public protocols; URL matching never executes site code."""

import hashlib
import importlib
import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from ats_core import SchemaError
from ats_vendors import REVISION

VENDORS = (
    "workable",
    "successfactors",
    "workday",
    "greenhouse",
    "lever",
    "ashby",
    "recruitee",
    "teamtailor",
)


def vendor(name):
    if name not in VENDORS:
        raise SchemaError("Unsupported ATS vendor")
    return importlib.import_module("ats_vendors." + name)


def detect(url, html=""):
    try:
        p = urlsplit(url)
        if (
            p.scheme != "https"
            or not p.hostname
            or p.username
            or p.password
            or p.port not in (None, 443)
        ):
            return None
        if "%" in p.path or any(segment in {".", ".."} for segment in p.path.split("/")):
            return None
    except ValueError:
        return None
    for name in VENDORS:
        source = vendor(name).detect(url, html)
        if (
            source
            and re.fullmatch(r"[A-Za-z0-9_.-]+", source.tenant)
            and re.fullmatch(r"[A-Za-z0-9_-]+", source.board)
        ):
            return source
    return None


def from_config(name, config):
    source = detect(config.get("portal_url", ""))
    if source is None and name == "successfactors" and config.get("variant") == "rmk":
        # A custom RMK hostname is saved only after verified page fingerprinting.
        source = detect(config.get("portal_url", ""), "<script>careerSiteBuilder</script>")
    if (
        source is None
        or source.vendor != name
        or source.tenant != config.get("tenant", source.tenant)
    ):
        raise SchemaError("Portal does not match a supported vendor protocol")
    if source.board != config.get("board", source.board):
        raise SchemaError("Portal and configured board disagree")
    return replace(source, company=config.get("company", ""))


def fingerprint(name):
    # Both host validation and isolated parsing must use this same immutable
    # library revision. Updated code requires a newly validated connector image.
    import ats_core

    files = sorted(Path(ats_core.__file__).parent.glob("*.py"))
    files += sorted(Path(__file__).parent.glob("*.py"))
    files += [Path(vendor(name).__file__)]
    value = b"\0".join(path.name.encode() + b"\0" + path.read_bytes() for path in files)
    return hashlib.sha256(value).hexdigest()


def manifest(source):
    request = vendor(source.vendor).catalogue_request(source)
    spec = {
        "format": "xml" if source.vendor in {"successfactors", "teamtailor"} else "json",
        "url": request.url,
        "path_suffix": urlsplit(request.url).path,
        "pagination": "offset" if source.vendor == "workday" else "single",
    }
    return {
        "ats_library": {
            "vendor": source.vendor,
            "revision": REVISION,
            "fingerprint": fingerprint(source.vendor),
            "source": source.config(),
        },
        "catalogue": spec,
        "capabilities": {"browser": False, "incremental": True},
    }


def network_rules(source):
    request = vendor(source.vendor).catalogue_request(source)
    p = urlsplit(request.url)
    rules = [{"host": p.hostname, "path": p.path, "methods": [request.method]}]
    if request.method == "POST":
        rules[0]["body_keys"] = list(request.body)
    # The verified portal remains GET-only. Other public vacancy hosts must be
    # evidenced and added to the source policy, never followed implicitly.
    host = urlsplit(source.portal_url).hostname
    rules.append({"host": host, "path": "/", "methods": ["GET"]})
    if source.vendor == "workday":
        from ats_vendors.workday import base

        rules.append(
            {"host": host, "path": urlsplit(base(source)).path + "/job/", "methods": ["GET"]}
        )
    return rules


def wrapper(name):
    vendor(name)
    return f"""from ats_vendors.runtime import Connector
_connector = Connector({name!r})

def enumerate(config, sdk):
    return _connector.enumerate(config, sdk)

def fetch_detail(config, entry, sdk):
    return _connector.fetch_detail(config, entry, sdk)

def normalize(config, entry, detail):
    return _connector.normalize(config, entry, detail)
"""
