#!/usr/bin/env python3
"""Run a JS fixture with real Chromium sandbox/AppArmor, removing temporary test profiles."""

import argparse
import importlib.util
import json
import select
import subprocess
import tempfile
import time
import uuid
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("image", help="Browser image sha256 ID")
    parser.add_argument(
        "--temporary-profiles",
        action="store_true",
        help="Use sudo to load uniquely named profiles for this check, then remove them",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("launcher", root / "deploy/connector-launcher.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    name = "sjh-browser-check-" + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="sjh-browser-check-") as directory:
        profile = Path(directory) / "profiles"
        prefix = "sjh-check-" + uuid.uuid4().hex + "-"
        loaded = False
        proc = None
        command = launcher.command("browser", args.image, name)
        command[command.index("seccomp=/etc/sjh-connector/browser-seccomp.json")] = (
            "seccomp=" + str(root / "deploy/browser-seccomp.json")
        )
        try:
            if args.temporary_profiles:
                profile.write_text(
                    (root / "deploy/sjh-connector.apparmor")
                    .read_text()
                    .replace("sjh-connector-", prefix)
                )
                subprocess.run(
                    ["sudo", "-n", "apparmor_parser", "--skip-cache", "-r", str(profile)],
                    check=True,
                )
                loaded = True
                command[command.index("apparmor=sjh-connector-browser")] = (
                    "apparmor=" + prefix + "browser"
                )
            proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            proc.stdin.write(
                json.dumps(
                    {"url": "https://public.example/catalogue", "actions": [{"wait_for": "#jobs"}]}
                )
                + "\n"
            )
            proc.stdin.flush()
            deadline = time.monotonic() + 50
            success = False
            while time.monotonic() < deadline:
                ready, _, _ = select.select([proc.stdout], [], [], 2)
                if not ready:
                    if proc.poll() is not None:
                        break
                    continue
                line = proc.stdout.readline()
                if not line:
                    break
                value = json.loads(line)
                if value.get("op") == "fetch":
                    if value["url"] != "https://public.example/catalogue":
                        raise ValueError("Unexpected browser request")
                    proc.stdin.write(
                        json.dumps(
                            {
                                "ok": True,
                                "text": '<html><body><div id="jobs"></div><script>document.getElementById("jobs").textContent="Browser executed offline";</script></body></html>',
                                "url": value["url"],
                                "status": 200,
                                "headers": {"content-type": "text/html"},
                                "key": "fixture",
                            }
                        )
                        + "\n"
                    )
                    proc.stdin.flush()
                elif value.get("op") == "result":
                    success = "Browser executed offline" in value["result"]["html"]
                    break
                else:
                    raise RuntimeError(value.get("error", "Unexpected browser response"))
            if not success or proc.wait(timeout=10):
                raise RuntimeError("Sandboxed browser fixture failed")
            print(
                "PASS: ARM64 Chromium sandbox, AppArmor, network-none, and broker-fulfilled JavaScript"
            )
        finally:
            subprocess.run(
                ["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            if proc and proc.poll() is None:
                proc.kill()
                proc.wait()
            if loaded:
                subprocess.run(
                    ["sudo", "-n", "apparmor_parser", "--skip-cache", "-R", str(profile)],
                    check=True,
                )


if __name__ == "__main__":
    main()
