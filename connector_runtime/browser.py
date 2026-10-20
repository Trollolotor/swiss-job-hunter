"""Trusted browser observation harness; all HTTP is fulfilled through the broker."""

import asyncio
import json
import sys

from connector_sdk import SDK
from playwright.async_api import async_playwright


async def main():
    payload = json.loads(sys.stdin.readline(256 * 1024))
    sdk = SDK()
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            chromium_sandbox=True,
            env={"HOME": "/tmp", "PATH": "/usr/local/bin:/usr/bin:/bin"},
        )
        context = await browser.new_context(service_workers="block", accept_downloads=False)

        async def route_handler(route):
            request = route.request
            if request.resource_type in {"media", "font", "image"}:
                await route.abort()
                return
            body = None
            if request.method == "POST":
                try:
                    body = request.post_data_json
                except Exception:
                    await route.abort()
                    return
            response = sdk.fetch(request.url, request.method, body)
            await route.fulfill(
                status=response["status"], headers=response["headers"], body=response["text"]
            )

        await context.route("**/*", route_handler)
        await context.route_web_socket("**/*", lambda ws: ws.close())
        page = await context.new_page()
        await page.goto(payload["url"], wait_until="domcontentloaded", timeout=45000)
        for action in payload.get("actions", [])[:20]:
            # Only observation/wait is supported. No click, fill, eval, or submit.
            if set(action) != {"wait_for"}:
                raise ValueError("Unsupported browser observation action")
            await page.locator(action["wait_for"]).first.wait_for(timeout=15000)
        result = {"html": await page.content(), "url": page.url}
        await browser.close()
        print(json.dumps({"op": "result", "result": result}), flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(
            json.dumps({"op": "error", "error": f"{type(exc).__name__}: {exc}"[:8000]}), flush=True
        )
        sys.exit(1)
