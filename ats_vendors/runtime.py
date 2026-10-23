"""SDK v2 adapters for ready connectors; all I/O goes through the supplied SDK."""

from ats_core import SchemaError
from ats_core.parsing import fingerprint
from ats_vendors.registry import from_config, vendor


class Connector:
    def __init__(self, name):
        self.name, self.protocol = name, vendor(name)
        self.entries = {}

    def enumerate(self, config, sdk):
        if "library_fingerprint" in config:
            from ats_vendors.registry import fingerprint as library_fingerprint

            if config["library_fingerprint"] != library_fingerprint(self.name):
                raise RuntimeError(
                    "runtime failed: ATS library does not match the validated image bundle"
                )
        source = from_config(self.name, config)
        self.entries = {}
        offset, total, diagnostics, termination = 0, None, [], "incomplete"
        for _ in range(min(500, max(1, int(config.get("max_pages", 100))))):
            request = self.protocol.catalogue_request(source, offset)
            raw = sdk.fetch(request.url, request.method, request.body)
            if raw["status"] != 200:
                raise SchemaError(f"Vendor catalogue returned HTTP {raw['status']}")
            try:
                page = self.protocol.parse_catalogue(source, raw["text"])
            except SchemaError as exc:
                if not self.entries:
                    raise
                diagnostics.append(str(exc))
                break
            diagnostics.extend(page.diagnostics)
            fresh = 0
            for entry in page.entries:
                if entry.identity in self.entries:
                    diagnostics.append("Repeated catalogue identity or page")
                    continue
                self.entries[entry.identity] = (entry, raw["key"])
                fresh += 1
            if self.name != "workday":
                total = len(page.entries)
                termination = "next_absent" if total else "explicit_empty"
                break
            if total is None:
                total = page.total
            elif page.total not in (0, total):
                diagnostics.append("Workday catalogue total changed during traversal")
                break
            if total == 2000:
                diagnostics.append(
                    "Workday query may be capped; full employer coverage is unconfirmed"
                )
            if len(self.entries) >= total and total != 2000:
                termination = "total" if total else "explicit_empty"
                break
            if not fresh:
                diagnostics.append("Workday pagination did not advance")
                break
            offset += len(page.entries)
        entries = []
        for entry, key in self.entries.values():
            entries.append(
                {
                    "posting_id": entry.posting_id,
                    "variant": entry.variant,
                    "detail_url": entry.detail_url,
                    "change_token": fingerprint(entry.data),
                    "metadata": {"catalogue_key": key, "ats_vendor": self.name},
                }
            )
        return {
            "entries": entries,
            "observed_ids": list(dict.fromkeys(e["posting_id"] for e in entries)),
            "reported_total": total,
            "scope": "all",
            "termination": termination,
            "diagnostics": list(dict.fromkeys(diagnostics)),
        }

    def fetch_detail(self, config, entry, sdk):
        source = from_config(self.name, config)
        raw, key = self.entries[(entry["posting_id"], entry.get("variant", ""))]
        request = self.protocol.detail_request(source, raw)
        if request is None:
            return {"text": None, "evidence": [key]}
        detail = sdk.fetch(request.url, request.method, request.body)
        if detail["status"] != 200:
            raise SchemaError(f"Vendor detail returned HTTP {detail['status']}")
        return {"text": detail["text"], "evidence": [key, detail["key"]]}

    def normalize(self, config, entry, detail):
        source = from_config(self.name, config)
        raw, _ = self.entries[(entry["posting_id"], entry.get("variant", ""))]
        parsed = self.protocol.parse_posting(source, raw, detail["text"])
        return parsed.wire(self.name, detail["evidence"])
