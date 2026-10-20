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
        context.allowed(payload["url"])
    socket_path = os.environ.get("EMPLOYER_LAUNCHER_SOCKET", "/run/sjh-connector/launcher.sock")
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path, limit=8 * 1024 * 1024)
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise RuntimeUnavailable(
            "Isolated launcher is unavailable; install/start the reviewed host runtime"
        ) from exc
    total = 0

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
                elif op == "browser":
                    if kind != "python":
                        raise ValueError("Nested browser requests are forbidden")
                    if not browser_image_digest:
                        raise ValueError("Browser runtime is not pinned in this connector version")
                    lock = _browser_locks.setdefault(id(asyncio.get_running_loop()), asyncio.Lock())
                    async with lock:
                        result = await run_isolated(
                            "browser",
                            message["request"],
                            context,
                            image_digest=browser_image_digest,
                        )
                    from employers.network import Observation

                    snapshot = Observation(
                        result["url"], "BROWSER", 200, result["html"], {"content-type": "text/html"}
                    )
                    context.observations.append(snapshot)
                    result["key"] = snapshot.key
                    await send({"ok": True, "result": result})
                elif op == "result":
                    return message["result"]
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
        {"code": version.code, "config": json.loads(version.config_json)},
        context,
        version.image_digest,
        json.loads(version.manifest_json).get("browser_image_digest"),
    )
    return CollectionResult.model_validate(value)
