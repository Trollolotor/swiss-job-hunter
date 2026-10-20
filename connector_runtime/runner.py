"""Only execute inside the network-disabled, credential-free runner container."""

import contextlib
import json
import sys

from connector_sdk import SDK


def main():
    payload = json.loads(sys.stdin.readline(256 * 1024))
    scope = {"__name__": "generated_connector"}
    sdk = SDK()
    # The SDK keeps its protocol stream; all generated logging goes to capped stderr.
    with contextlib.redirect_stdout(sys.stderr):
        exec(compile(payload["code"], "<generated-connector>", "exec"), scope)
        if payload.get("sdk_version", "1") == "2":
            catalogue = scope["enumerate"](payload["config"], sdk)
            work = sdk._request({"op": "catalogue", "catalogue": catalogue})["entries"]
            result = {k: v for k, v in catalogue.items() if k != "entries"}
            result["postings"] = []
            for entry in work:
                raw = scope["fetch_detail"](payload["config"], entry, sdk)
                posting = scope["normalize"](payload["config"], entry, raw)
                sdk._request({"op": "sighting", "posting": posting})
                result["postings"].append(posting)
        else:
            result = scope["collect"](payload["config"], sdk)
    print(json.dumps({"op": "result", "result": result}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            json.dumps({"op": "error", "error": f"{type(exc).__name__}: {exc}"[:2000]}), flush=True
        )
        sys.exit(1)
