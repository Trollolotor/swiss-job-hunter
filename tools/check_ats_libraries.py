#!/usr/bin/env python3
"""Run synthetic vendor fixtures through the real network-disabled SDK container.

This smoke test uses Docker directly, not the production launcher; it does not
certify host AppArmor profiles or ARM64 when run on an x86 development machine.
"""

import argparse
import json
import os
import re
import selectors
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def check(image, name):
    from ats_vendors.registry import detect, manifest, vendor, wrapper
    from employers.contracts import CollectedPosting, CollectionResult
    from employers.network import Observation
    from employers.validation import assess, posting_errors

    data = json.loads((ROOT / "tests/fixtures/ats" / (name + ".json")).read_text())
    source = replace(detect(data["portal"], data.get("html", "")), company="Acme")
    spec = manifest(source)
    config = {**source.config(), "library_fingerprint": spec["ats_library"]["fingerprint"]}
    container_name = "sjh-ats-check-" + uuid4().hex
    command = [
        "docker",
        "--host=unix:///var/run/docker.sock",
        "run",
        "--rm",
        "--name=" + container_name,
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=64",
        "--memory=256m",
        "--tmpfs=/tmp:rw,noexec,nosuid,size=16m",
        "-i",
        image,
    ]
    # Never select another daemon through inherited TLS/context configuration.
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_TLS",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
        }
    }
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    observations, staged = [], []

    def send(value):
        process.stdin.write(json.dumps(value) + "\n")
        process.stdin.flush()

    try:
        send({"code": wrapper(source.vendor), "config": config, "sdk_version": "2"})
        deadline = time.monotonic() + 90
        while True:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                if not selector.select(max(0, deadline - time.monotonic())):
                    raise TimeoutError("Container fixture exceeded its 90-second limit")
            line = process.stdout.readline()
            if not line:
                raise RuntimeError(process.stderr.read()[:2000] or "Runner ended without a result")
            message = json.loads(line)
            if message["op"] == "fetch":
                body = message.get("body")
                request = vendor(source.vendor).catalogue_request(
                    source, (body or {}).get("offset", 0)
                )
                if (message["url"], message.get("method", "GET"), body) == (
                    request.url,
                    request.method,
                    request.body,
                ):
                    payload = data["catalogue"]
                elif data.get("detail") is not None:
                    payload = data["detail"]
                else:
                    raise RuntimeError("Fixture does not contain requested response")
                text = payload if isinstance(payload, str) else json.dumps(payload)
                obs = Observation(message["url"], message.get("method", "GET"), 200, text, {}, body)
                observations.append(obs)
                send(
                    {
                        "ok": True,
                        "text": text,
                        "url": obs.url,
                        "status": 200,
                        "headers": {},
                        "key": obs.key,
                    }
                )
            elif message["op"] == "catalogue":
                send({"ok": True, "entries": message["catalogue"]["entries"]})
            elif message["op"] == "sighting":
                posting = CollectedPosting.model_validate(message["posting"])
                errors = posting_errors(posting, observations, spec)
                if errors:
                    raise RuntimeError("; ".join(errors))
                staged.append(posting)
                send({"ok": True})
            elif message["op"] == "result":
                result = CollectionResult.model_validate(message["result"])
                report = assess(result, observations, spec)
                if report["outcome"] != "complete" or len(staged) != 1:
                    raise RuntimeError(json.dumps(report))
                process.stdin.close()
                if process.wait(timeout=15) != 0:
                    raise RuntimeError(process.stderr.read()[:2000])
                return {"vendor": name, "outcome": report["outcome"], "requests": len(observations)}
            else:
                raise RuntimeError(str(message))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=15)
            subprocess.run(
                ["docker", "--host=unix:///var/run/docker.sock", "rm", "--force", container_name],
                env=env,
                capture_output=True,
                timeout=15,
                check=False,
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="Pinned sha256 ID of the python connector image")
    args = parser.parse_args()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", args.image):
        parser.error("Pass an immutable image ID, not a tag")
    from ats_vendors.registry import VENDORS

    for name in [*VENDORS, "successfactors_legacy"]:
        print(json.dumps(check(args.image, name)), flush=True)


if __name__ == "__main__":
    main()
