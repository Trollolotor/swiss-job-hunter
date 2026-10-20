"""Only execute inside the network-disabled, credential-free runner container."""

import contextlib
import json
import sys

from connector_sdk import SDK


def main():
    payload = json.loads(sys.stdin.readline(256 * 1024))
    scope = {"__name__": "generated_connector"}
    # Generated stdout belongs to capped stderr, not the tool protocol.
    with contextlib.redirect_stdout(sys.stderr):
        exec(compile(payload["code"], "<generated-connector>", "exec"), scope)
    # SDK prints must continue to reach the protocol channel.
    result = scope["collect"](payload["config"], SDK())
    print(json.dumps({"op": "result", "result": result}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            json.dumps({"op": "error", "error": f"{type(exc).__name__}: {exc}"[:2000]}), flush=True
        )
        sys.exit(1)
