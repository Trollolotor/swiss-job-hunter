"""Guarded public HTTP with DNS pinning, per-hop policies, and byte budgets."""

import asyncio
import ipaddress
import json
import socket
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from httpcore._backends.anyio import AnyIOBackend

from employers.contracts import digest


class PolicyError(ValueError):
    pass


class CollectionLimit(RuntimeError):
    pass


def public_url(url):
    if not isinstance(url, str) or len(url) > 8192:
        raise PolicyError("Invalid or oversized public URL")
    p = urlsplit(url)
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise PolicyError("Only public HTTP(S) URLs without credentials are supported")
    if p.port not in (None, 80, 443) or p.fragment:
        raise PolicyError("Nonstandard ports and URL fragments are not allowed")
    host = p.hostname.lower().rstrip(".")
    if host in {"localhost", "metadata.google.internal"} or host.endswith(
        (".localhost", ".local", ".internal")
    ):
        raise PolicyError("Internal destination is forbidden")
    try:
        if not ipaddress.ip_address(host).is_global:
            raise PolicyError("Nonpublic IP address is forbidden")
    except ValueError as exc:
        if isinstance(exc, PolicyError):
            raise
    return p


class PinnedBackend(AnyIOBackend):
    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if isinstance(host, bytes):
            host = host.decode("ascii")
        public_url(f"https://{('[' + host + ']') if ':' in host else host}")
        addresses = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )
        ips = list(dict.fromkeys(r[4][0] for r in addresses))
        if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
            raise PolicyError("DNS resolved to a nonpublic destination")
        # TLS SNI/hostname validation is still performed for the original pool host.
        return await super().connect_tcp(ips[0], port, timeout, None, socket_options)


@dataclass
class Observation:
    url: str
    method: str
    status: int
    text: str
    headers: dict
    request_body: dict | None = None

    @property
    def key(self):
        return digest([self.url, self.method, self.status, self.text])

    def json(self):
        return json.loads(self.text)


class CollectionContext:
    _locks = {}
    _last = {}

    def __init__(self, policy, settings, *, transport=None, known=None):
        self.policy, self.settings = policy, settings
        self.observations: list[Observation] = []
        self.requests, self.bytes = 0, 0
        self.started = time.monotonic()
        self.known = known or {}
        if transport is None:
            transport = httpx.AsyncHTTPTransport(retries=0)
            # httpx delegates connection establishment to httpcore. The pinned
            # backend changes only TCP destinations, retaining TLS SNI/cert checks.
            transport._pool._network_backend = PinnedBackend()
        self.client = httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            timeout=25,
            follow_redirects=False,
            headers={
                "User-Agent": "SwissJobHunter/0.1 (+public-career-collection)",
                "Accept-Encoding": "identity",
            },
        )

    def allowed(self, url, method="GET", body=None):
        p = public_url(url)
        forbidden = {
            "apply",
            "application",
            "applications",
            "candidate",
            "account",
            "login",
            "logout",
            "register",
            "subscribe",
            "unsubscribe",
            "contact",
            "message",
            "messages",
            "submit",
            "delete",
            "sign-in",
            "oauth",
        }
        decoded_path = p.path
        for _ in range(5):
            decoded = unquote(decoded_path)
            if decoded == decoded_path:
                break
            decoded_path = decoded
        else:
            raise PolicyError("Repeated URL encoding is outside collection scope")
        if {segment.casefold() for segment in decoded_path.split("/")} & forbidden or {
            key.casefold() for key in parse_qs(p.query)
        } & {"email", "password", "csrf", "submit", "message", "action"}:
            raise PolicyError(
                "Account, messaging, and application actions are outside collection scope"
            )
        for rule in self.policy:
            if (
                p.hostname.lower() == rule.get("host", "").lower()
                and method in rule.get("methods", ["GET"])
                and (p.path or "/").startswith(rule.get("path", "/"))
            ):
                if method == "POST":
                    # POST is limited to a particular read-only endpoint with
                    # bounded known fields; GraphQL mutations cannot sneak through.
                    if p.path != rule.get("path") or not isinstance(body, dict):
                        continue
                    if set(body) - set(rule.get("body_keys", [])):
                        continue
                return
        raise PolicyError("Destination or method is outside the verified source policy")

    async def fetch(self, url, method="GET", body=None):
        from urllib.parse import urljoin

        for _ in range(6):
            if getattr(self, "fence", None):
                self.fence()
            self.allowed(url, method, body)
            if (
                self.requests >= self.settings.max_requests
                or time.monotonic() - self.started > self.settings.runtime_seconds
            ):
                raise CollectionLimit("Collection request/time budget exhausted")
            key = (id(asyncio.get_running_loop()), urlsplit(url).hostname)
            lock = self._locks.setdefault(key, asyncio.Lock())
            async with lock:
                await asyncio.sleep(
                    max(
                        0,
                        self.settings.domain_delay_seconds
                        - (time.monotonic() - self._last.get(key, 0)),
                    )
                )
                self.requests += 1
                async with self.client.stream(
                    method, url, json=body if method == "POST" else None
                ) as r:
                    chunks, size = [], 0
                    async for chunk in r.aiter_bytes():
                        size += len(chunk)
                        self.bytes += len(chunk)
                        if (
                            size > self.settings.response_bytes
                            or self.bytes + getattr(self, "cached_bytes", 0) > self.settings.total_bytes
                        ):
                            raise CollectionLimit("Collection response byte budget exhausted")
                        chunks.append(chunk)
                    self._last[key] = time.monotonic()
                    obs = Observation(
                        str(r.url),
                        method,
                        r.status_code,
                        b"".join(chunks).decode("utf-8", "replace"),
                        {
                            k: v
                            for k, v in r.headers.items()
                            if k.lower() in {"content-type", "etag", "last-modified", "location"}
                        },
                        body,
                    )
                    self.observations.append(obs)
            if obs.status in {301, 302, 303, 307, 308}:
                url = urljoin(url, obs.headers.get("location", ""))
                if obs.status == 303:
                    method, body = "GET", None
                continue
            if obs.status in {401, 403}:
                raise PolicyError("Authentication wall or access challenge; source must be paused")
            if obs.status == 429 or obs.status >= 500:
                raise httpx.HTTPStatusError(
                    f"Transient portal response {obs.status}",
                    request=httpx.Request(method, url),
                    response=httpx.Response(obs.status),
                )
            if obs.status >= 400:
                raise ValueError(f"Portal returned HTTP {obs.status}")
            return obs
        raise PolicyError("Too many redirects")

    async def close(self):
        await self.client.aclose()
