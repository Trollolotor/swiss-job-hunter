#!/usr/bin/env python3
"""Host-side ARM64 isolation smoke test. Never mounts application files in a runner."""

import argparse
import importlib.util
import json
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("image", help="Approved Python runner sha256 image ID")
    parser.add_argument(
        "--development-profile",
        action="store_true",
        help="Use docker-default before installing the dedicated AppArmor profile",
    )
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location(
        "launcher", Path(__file__).resolve().parents[1] / "deploy/connector-launcher.py"
    )
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    command = launcher.command("python", args.image, "sjh-connector-isolation-check")
    if args.development_profile:
        command[command.index("apparmor=sjh-connector-python")] = "apparmor=docker-default"
    code = """
def collect(config,sdk):
    import os,socket,subprocess
    from pathlib import Path
    checks={"nonroot":os.getuid()==10001,"no_app":not Path("/app").exists(),
            "no_docker_socket":not Path("/var/run/docker.sock").exists(),
            "no_credentials":not any("API_KEY" in key for key in os.environ),
            "memory_limit":Path("/sys/fs/cgroup/memory.max").read_text().strip()=="536870912",
            "pid_limit":Path("/sys/fs/cgroup/pids.max").read_text().strip()=="64"}
    try:
        Path("/runtime/forbidden").write_text("x")
        checks["readonly"]=False
    except OSError:
        checks["readonly"]=True
    try:
        socket.create_connection(("169.254.169.254",80),timeout=0.2)
        checks["network_blocked"]=False
    except OSError:
        checks["network_blocked"]=True
    children=[]
    try:
        for _ in range(100):
            children.append(subprocess.Popen(["sleep","2"]))
        checks["process_limit_enforced"]=False
    except OSError:
        checks["process_limit_enforced"]=len(children)<64
    finally:
        for child in children:
            child.terminate()
        for child in children:
            child.wait()
    return checks
"""
    try:
        run = subprocess.run(
            command,
            input=json.dumps({"code": code, "config": {}}) + "\n",
            capture_output=True,
            text=True,
            timeout=45,
            check=True,
        )
        result = json.loads(run.stdout)["result"]
        print(json.dumps(result, indent=2))
        if not all(result.values()):
            raise SystemExit("Isolation check failed")
    finally:
        subprocess.run(
            ["docker", "rm", "-f", "sjh-connector-isolation-check"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


if __name__ == "__main__":
    main()
