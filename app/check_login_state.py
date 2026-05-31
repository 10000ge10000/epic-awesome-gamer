# -*- coding: utf-8 -*-
"""Check whether an existing Epic browser profile is still logged in."""
from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from urllib.parse import urlparse

from browserforge.fingerprints import Screen
from camoufox import AsyncCamoufox

from services.epic_games_service import URL_CLAIM
from settings import settings


def proxy_config():
    http_proxy = os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY")
    if not http_proxy:
        return None
    parsed = urlparse(http_proxy)
    cfg = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        cfg["username"] = parsed.username
    if parsed.password:
        cfg["password"] = parsed.password
    return cfg


async def main() -> int:
    async with AsyncCamoufox(
        persistent_context=True,
        user_data_dir=settings.user_data_dir,
        screen=Screen(max_width=1280, max_height=720, min_width=1280, min_height=720),
        humanize=0.2,
        headless=True,
        proxy=proxy_config(),
    ) as browser:
        page = browser.pages[0] if browser.pages else await browser.new_page()
        await page.goto(URL_CLAIM, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        with suppress(Exception):
            status = await page.locator("//egs-navigation").get_attribute("isloggedin", timeout=10000)
            if status == "true":
                print("LOGIN_STATE_VALID", flush=True)
                return 0

    print("LOGIN_STATE_EXPIRED", flush=True)
    return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
