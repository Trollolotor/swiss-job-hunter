"""Bounded deterministic portal resolution from explicit board organization links."""

import re
from urllib.parse import urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup

from employers.config import get_config
from employers.network import CollectionContext
from employers.service import detect_platform, verify_and_create


async def from_official_links(task, evidence, fence):
    candidates = list(
        dict.fromkeys(
            [
                o.get("payload", {}).get("official_url")
                for o in evidence["observations"]
                if o.get("payload", {}).get("official_url")
            ]
            + evidence.get("candidate_official_urls", [])
        )
    )[:2]
    for official_url in candidates:
        pending = [official_url]
        visited = set()
        while pending and len(visited) < 3:
            url = pending.pop(0)
            visited.add(url)
            context = CollectionContext(
                [{"host": urlsplit(url).hostname, "path": "/"}], get_config()
            )
            try:
                observation = await context.fetch(url)
                evidence["observations"].append(
                    {"url": observation.url, "html": observation.text[:40000]}
                )
                soup = BeautifulSoup(observation.text, "html.parser")
                links = [
                    urldefrag(urljoin(observation.url, a["href"]))[0]
                    for a in soup.select("a[href]")
                ]
                for link in links:
                    if urlsplit(link).scheme not in {"https", "http"}:
                        continue
                    if detect_platform(link)[0] != "custom":
                        fence(task)
                        return await verify_and_create(
                            task["employer_id"],
                            url,
                            link,
                            evidence.get("classification", "direct_employer"),
                        )
                for link in links:
                    if (
                        urlsplit(link).hostname == urlsplit(official_url).hostname
                        and re.search(
                            r"/(careers?|karriere|vacancies|stellen|jobs)(?:[/.?]|$)",
                            urlsplit(link).path,
                            re.IGNORECASE,
                        )
                        and link not in visited
                    ):
                        pending.append(link)
            except (ValueError, OSError):
                continue
            finally:
                await context.close()
    return None
