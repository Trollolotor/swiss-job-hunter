"""Versioned synchronous IPC SDK, installed inside the untrusted-code image."""

import json
import sys


class SDK:
    version = "2"

    def __init__(self):
        self._protocol = sys.stdout

    def _request(self, message):
        print(json.dumps(message), flush=True, file=self._protocol)
        line = sys.stdin.readline(8 * 1024 * 1024)
        if not line or not line.endswith("\n"):
            raise RuntimeError("Controller disconnected")
        answer = json.loads(line)
        if not answer.get("ok"):
            raise RuntimeError(answer.get("error", "Tool rejected"))
        return answer

    def fetch(self, url, method="GET", body=None):
        return self._request({"op": "fetch", "url": url, "method": method, "body": body})

    def browser(self, url, actions=None):
        return self._request({"op": "browser", "request": {"url": url, "actions": actions or []}})[
            "result"
        ]

    def json(self, url, method="GET", body=None):
        response = self.fetch(url, method, body)
        return json.loads(response["text"]), response["key"]
