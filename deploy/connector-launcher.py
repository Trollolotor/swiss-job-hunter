#!/usr/bin/env python3
"""Narrow host launcher: no imports from the application or generated bundles."""

import asyncio
import json
import os
import re
import sys
import uuid
from pathlib import Path

MAX_FRAME = 8 * 1024 * 1024
CONFIG = Path("/etc/sjh-connector/images.json")
SLOTS = asyncio.Semaphore(4)


def command(kind, image, name):
    if kind not in {"python", "browser"} or not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise ValueError("Approved image digest and fixed run kind are required")
    result = [
        "/usr/bin/docker",
        "run",
        "--rm",
        "--init",
        "--interactive",
        "--name",
        name,
        "--label",
        "swiss-job-hunter.connector=true",
        "--network",
        "none",
        "--user",
        "10001:10001",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--security-opt",
        f"apparmor=sjh-connector-{kind}",
        "--pids-limit",
        "256" if kind == "browser" else "64",
        "--memory",
        "2g" if kind == "browser" else "512m",
        "--memory-swap",
        "2g" if kind == "browser" else "512m",
        "--cpus",
        "2" if kind == "browser" else "1",
        "--ipc",
        "private",
        "--shm-size",
        "512m" if kind == "browser" else "16m",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=256m",
        "--ulimit",
        "nofile=256:256",
        "--log-driver",
        "none",
        "--pull",
        "never",
    ]
    if kind == "browser":
        result += ["--security-opt", "seccomp=/etc/sjh-connector/browser-seccomp.json"]
    return result + [image]


async def handle(reader, writer):
    proc = None
    name = "sjh-connector-" + uuid.uuid4().hex
    try:
        header = json.loads(await asyncio.wait_for(reader.readline(), 10))
        if set(header) - {"kind", "payload", "image_digest"}:
            raise ValueError("Unknown launcher options")
        config = json.loads(CONFIG.read_text())
        kind = header["kind"]
        image = config[kind]
        if header.get("image_digest") and header["image_digest"] != image:
            raise ValueError("Connector image changed; revalidation is required")
        payload = json.dumps(header["payload"]).encode() + b"\n"
        if len(payload) > 256 * 1024:
            raise ValueError("Connector bundle exceeds size limit")
        proc = await asyncio.create_subprocess_exec(
            *command(kind, image, name),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=MAX_FRAME,
        )
        proc.stdin.write(payload)
        await proc.stdin.drain()

        async def input_pipe():
            while frame := await reader.readline():
                proc.stdin.write(frame)
                await proc.stdin.drain()
            if proc.returncode is None:
                proc.terminate()

        async def output_pipe():
            size = 0
            while frame := await proc.stdout.readline():
                size += len(frame)
                if size > 20 * 1024 * 1024:
                    raise ValueError("Runner exceeded output limit")
                writer.write(frame)
                await writer.drain()

        async def stderr_pipe():
            size = 0
            while frame := await proc.stderr.read(4096):
                size += len(frame)
                if size > 128 * 1024:
                    raise ValueError("Runner exceeded log limit")

        async with asyncio.timeout(600):
            input_task = asyncio.create_task(input_pipe())
            try:
                await asyncio.gather(output_pipe(), stderr_pipe())
                await proc.wait()
            finally:
                input_task.cancel()
                await asyncio.gather(input_task, return_exceptions=True)
        if proc.returncode:
            raise RuntimeError(
                "Isolated runtime failed; verify installed profiles and image digests"
            )
    except Exception as exc:
        writer.write(json.dumps({"op": "error", "error": str(exc)[:1000]}).encode() + b"\n")
        try:
            await writer.drain()
        except (ConnectionError, BrokenPipeError):
            pass
    finally:
        if proc and proc.returncode is None:
            proc.kill()
            await proc.wait()
        if proc:
            cleanup = await asyncio.create_subprocess_exec(
                "/usr/bin/docker",
                "rm",
                "-f",
                name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await cleanup.wait()
        writer.close()


async def main():
    await cleanup_orphans()
    path = "/run/sjh-connector/launcher.sock"
    Path(path).unlink(missing_ok=True)

    async def bounded(reader, writer):
        async with SLOTS:
            await handle(reader, writer)

    server = await asyncio.start_unix_server(bounded, path, limit=MAX_FRAME)
    os.chmod(path, 0o660)
    async with server:
        await server.serve_forever()


async def cleanup_orphans():
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/docker",
        "ps",
        "--all",
        "--filter",
        "label=swiss-job-hunter.connector=true",
        "--format",
        "{{.ID}}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    output, _ = await process.communicate()
    for identifier in output.decode().split():
        if not re.fullmatch(r"[a-f0-9]{12,64}", identifier):
            continue
        child = await asyncio.create_subprocess_exec(
            "/usr/bin/docker",
            "rm",
            "-f",
            identifier,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await child.wait()


if __name__ == "__main__":
    asyncio.run(cleanup_orphans() if sys.argv[1:] == ["--cleanup"] else main())
