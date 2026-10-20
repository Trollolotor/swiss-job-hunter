"""Client for the fixed-policy host launcher. No local execution fallback."""

import asyncio
import json
import os

from employers.contracts import CollectionResult
from employers.network import CollectionLimit

_browser_locks = {}


class RuntimeUnavailable(RuntimeError):
    """Infrastructure is missing or changed; regenerating extraction code cannot fix it."""


async def run_isolated(kind, payload, context, image_digest=None, browser_image_digest=None):
    if kind == "browser":
        lock = _browser_locks.setdefault(id(asyncio.get_running_loop()), asyncio.Lock())
        async with lock:
            return await _run_isolated(kind, payload, context, image_digest, browser_image_digest)
    return await _run_isolated(kind, payload, context, image_digest, browser_image_digest)


async def _run_isolated(kind, payload, context, image_digest=None, browser_image_digest=None):
    if kind == "browser":
        context.allowed(payload["url"])
    socket_path = os.environ.get("EMPLOYER_LAUNCHER_SOCKET", "/run/sjh-connector/launcher.sock")
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path, limit=8 * 1024 * 1024)
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise RuntimeUnavailable(
            "Isolated launcher is unavailable; install/start the reviewed host runtime"
        ) from exc
    total = 0
    observation_start = len(context.observations)

    async def send(value):
        frame = json.dumps(value, default=str, ensure_ascii=False).encode() + b"\n"
        if len(frame) > 8 * 1024 * 1024:
            raise CollectionLimit("SDK frame exceeds size limit")
        writer.write(frame)
        await writer.drain()

    try:
        await send({"kind": kind, "payload": payload, "image_digest": image_digest})
        async with asyncio.timeout(context.settings.runtime_seconds):
            while line := await reader.readline():
                total += len(line)
                if total > 20 * 1024 * 1024:
                    raise CollectionLimit("Runner output budget exhausted")
                message = json.loads(line)
                op = message.get("op")
                if op == "fetch":
                    observation = await context.fetch(
                        message["url"], message.get("method", "GET"), message.get("body")
                    )
                    if hasattr(context, "fresh_detail_keys"):
                        context.fresh_detail_keys.add(observation.key)
                    await send(
                        {
                            "ok": True,
                            "text": observation.text,
                            "url": observation.url,
                            "status": observation.status,
                            "headers": observation.headers,
                            "key": observation.key,
                        }
                    )
                elif op == "catalogue":
                    if kind != "python" or payload.get("sdk_version") != "2":
                        raise ValueError("Catalogue staging requires SDK v2")
                    from employers.contracts import CatalogueResult
                    from employers.cache import cached

                    catalogue = CatalogueResult.model_validate(message["catalogue"])
                    if len({e.identity for e in catalogue.entries}) != len(catalogue.entries):
                        raise ValueError("Duplicate catalogue entry")
                    if {e.posting_id for e in catalogue.entries} != set(catalogue.observed_ids):
                        raise ValueError("Catalogue entries and observed IDs disagree")
                    context.catalogue_entries = catalogue.entries
                    context.cached_postings = []
                    work = []
                    for entry in catalogue.entries:
                        context.allowed(entry.detail_url)
                        posting = cached(context, entry)
                        if posting is None:
                            work.append(entry.model_dump())
                        else:
                            context.cached_postings.append(posting)
                            context.staged.append(posting)
                    await send({"ok": True, "entries": work})
                elif op == "sighting":
                    if kind != "python" or payload.get("sdk_version") != "2":
                        raise ValueError("Staging requires SDK v2")
                    from employers.contracts import CollectedPosting
                    from employers.validation import posting_errors

                    posting = CollectedPosting.model_validate(message["posting"])
                    errors = posting_errors(posting, context.observations)
                    if len(context.staged) >= 10000 or errors:
                        raise ValueError(
                            "Posting sighting rejected: "
                            + ("; ".join(errors) if errors else "sighting limit exceeded")
                        )
                    context.staged.append(posting)
                    await send({"ok": True})
                elif op == "browser":
                    if kind != "python":
                        raise ValueError("Nested browser requests are forbidden")
                    if not browser_image_digest:
                        raise ValueError("Browser runtime is not pinned in this connector version")
                    result = await run_isolated(
                        "browser", message["request"], context, image_digest=browser_image_digest
                    )
                    from employers.network import Observation

                    snapshot = Observation(
                        result["url"], "BROWSER", 200, result["html"], {"content-type": "text/html"}
                    )
                    context.observations.append(snapshot)
                    result["key"] = snapshot.key
                    await send({"ok": True, "result": result})
                elif op == "result":
                    result = message["result"]
                    if kind == "browser":
                        result["network"] = [
                            {
                                "url": o.url,
                                "method": o.method,
                                "body": o.request_body,
                                "status": o.status,
                                "key": o.key,
                                "text": o.text[:20000],
                            }
                            for o in context.observations[observation_start:]
                            if o.method == "POST" or "json" in o.headers.get("content-type", "")
                        ][:20]
                    return result
                elif op == "error":
                    error = str(message.get("error", "Runner failed"))[:2000]
                    if "runtime failed" in error or "image changed" in error:
                        raise RuntimeUnavailable(error)
                    raise RuntimeError(error)
                else:
                    raise ValueError("Unknown connector SDK action")
        raise RuntimeError("Runner ended without a result")
    finally:
        writer.close()
        await writer.wait_closed()


async def collect_generated(version, context):
    value = await run_isolated(
        "python",
        {
            "code": version.code,
            "config": json.loads(version.config_json),
            "sdk_version": version.sdk_version,
        },
        context,
        version.image_digest,
        json.loads(version.manifest_json).get("browser_image_digest"),
    )
    result = CollectionResult.model_validate(value)
    if version.sdk_version == "2":
        result.postings = context.staged
    result.observed_identities = [p.identity for p in result.postings]
    return result
