# -*- coding: utf-8 -*-
"""Manual Epic login session driven by Web screenshots and input events."""
from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlparse

import redis
from browserforge.fingerprints import Screen
from camoufox import AsyncCamoufox

from services.epic_games_service import URL_CLAIM, URL_LOGIN
from settings import settings


SESSION_ID = os.getenv("MANUAL_LOGIN_SESSION_ID", "")
EMAIL = os.getenv("EPIC_EMAIL", "")
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
SESSION_TTL_SECONDS = int(os.getenv("MANUAL_LOGIN_TTL_SECONDS", "900"))
SCREENSHOT_INTERVAL_SECONDS = float(os.getenv("MANUAL_LOGIN_SCREENSHOT_INTERVAL", "1.0"))
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
SESSION_DIR = DATA_DIR / "manual_login"

r = redis.Redis(host=REDIS_HOST, port=6379, decode_responses=True)


def session_key(name: str) -> str:
    return f"manual_login:{name}:{SESSION_ID}"


def set_status(status: str, msg: str, **extra):
    payload = {
        "status": status,
        "msg": msg,
        "email": EMAIL,
        "session_id": SESSION_ID,
        "updated_at": int(time.time()),
        **extra,
    }
    r.setex(session_key("status"), SESSION_TTL_SECONDS, json.dumps(payload, ensure_ascii=False))
    r.setex(f"status:{EMAIL}", SESSION_TTL_SECONDS, msg)


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


async def is_logged_in(page) -> bool:
    with suppress(Exception):
        status = await page.locator("//egs-navigation").get_attribute("isloggedin", timeout=2000)
        return status == "true"
    with suppress(Exception):
        body = await page.locator("body").text_content(timeout=1000)
        if body and ("Discover" in body and "Wishlist" in body and "Sign In" not in body):
            return True
    return False


async def apply_control(page, command: dict):
    kind = command.get("type")
    if kind == "click":
        await page.mouse.click(float(command.get("x", 0)), float(command.get("y", 0)))
    elif kind == "type":
        text = str(command.get("text", ""))
        if text:
            await page.keyboard.type(text, delay=30)
    elif kind == "press":
        key = str(command.get("key", "Enter"))
        await page.keyboard.press(key)
    elif kind == "goto":
        url = str(command.get("url", URL_LOGIN))
        if url.startswith("https://"):
            await page.goto(url, wait_until="domcontentloaded")


async def main() -> int:
    if not SESSION_ID or not EMAIL:
        raise SystemExit("MANUAL_LOGIN_SESSION_ID and EPIC_EMAIL are required")

    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    screenshot_path = SESSION_DIR / f"{SESSION_ID}.jpg"
    set_status("starting", "正在启动远程登录浏览器...")

    async with AsyncCamoufox(
        persistent_context=True,
        user_data_dir=settings.user_data_dir,
        screen=Screen(max_width=1280, max_height=720, min_width=1280, min_height=720),
        humanize=0.2,
        headless=True,
        proxy=proxy_config(),
    ) as browser:
        page = browser.pages[0] if browser.pages else await browser.new_page()
        await page.set_viewport_size({"width": 1280, "height": 720})
        await page.goto(URL_LOGIN, wait_until="domcontentloaded")
        set_status("waiting", "请在远程浏览器中完成 Epic 登录", screenshot=f"/manual_login/{SESSION_ID}.jpg")

        deadline = time.time() + SESSION_TTL_SECONDS
        last_shot = 0.0
        while time.time() < deadline:
            while True:
                raw = r.lpop(session_key("control"))
                if not raw:
                    break
                with suppress(Exception):
                    command = json.loads(raw)
                    if command.get("type") == "cancel":
                        set_status("cancelled", "浏览器授权会话已取消")
                        print("MANUAL_LOGIN_CANCELLED", flush=True)
                        return 3
                    await apply_control(page, command)

            now = time.time()
            if now - last_shot >= SCREENSHOT_INTERVAL_SECONDS:
                with suppress(Exception):
                    await page.screenshot(path=str(screenshot_path), type="jpeg", quality=70, full_page=False)
                    r.expire(session_key("status"), SESSION_TTL_SECONDS)
                last_shot = now

            if await is_logged_in(page):
                with suppress(Exception):
                    await page.goto(URL_CLAIM, wait_until="domcontentloaded")
                    await page.wait_for_timeout(1500)
                    await page.screenshot(path=str(screenshot_path), type="jpeg", quality=70, full_page=False)
                set_status(
                    "success",
                    "✅ 登录态已保存，后续将复用浏览器登录信息",
                    screenshot=f"/manual_login/{SESSION_ID}.jpg",
                )
                print("MANUAL_LOGIN_SUCCESS", flush=True)
                return 0

            await asyncio.sleep(0.2)

    set_status("expired", "登录会话已过期，请重新发起浏览器授权")
    print("MANUAL_LOGIN_EXPIRED", flush=True)
    return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
