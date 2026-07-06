# -*- coding: utf-8 -*-
# Time       : 2022/1/16 0:25
# Author     : QIN2DIM
# GitHub     : https://github.com/QIN2DIM
# Description: 游戏商城控制句柄

import asyncio
import json
import re
import time
from contextlib import suppress
from enum import Enum
from json import JSONDecodeError
from typing import Any, List

import httpx
from hcaptcha_challenger.agent import AgentV
from hcaptcha_challenger.models import ChallengeSignal
from loguru import logger
from playwright.async_api import Page
from playwright.async_api import expect, TimeoutError, FrameLocator
from tenacity import retry, retry_if_exception_type, stop_after_attempt

from models import OrderItem, Order
from models import PromotionGame
from settings import settings, RUNTIME_DIR

URL_CLAIM = "https://store.epicgames.com/en-US/free-games"
URL_LOGIN = (
    f"https://www.epicgames.com/id/login?lang=en-US&noHostRedirect=true&redirectUrl={URL_CLAIM}"
)
URL_CART = "https://store.epicgames.com/en-US/cart"
URL_CART_SUCCESS = "https://store.epicgames.com/en-US/cart/success"
URL_ORDER_HISTORY = "https://www.epicgames.com/account/v2/payment/ajaxGetOrderHistory"


URL_PROMOTIONS = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
URL_PRODUCT_PAGE = "https://store.epicgames.com/en-US/p/"
URL_PRODUCT_BUNDLES = "https://store.epicgames.com/en-US/bundles/"


class GameCollectResult(Enum):
    """
    游戏收集结果枚举

    用于区分不同的执行结果，便于上层调用者判断是否成功
    """
    # 成功：所有游戏已在库中
    ALL_OWNED = "all_owned"

    # 成功：游戏领取成功
    SUCCESS = "success"

    # 失败：EULA 协议未接受
    EULA_FAILED = "eula_failed"

    # 失败：Cookie 无效
    COOKIE_INVALID = "cookie_invalid"

    # 失败：领取阶段验证码未通过或无法确认
    CAPTCHA_FAILED = "captcha_failed"

    # 失败：验证码通过，但无法确认订单完成或商品入库
    CHECKOUT_FAILED = "checkout_failed"

    # 失败：未知错误
    UNKNOWN_ERROR = "unknown_error"

    # 失败：Playwright/Camoufox 驱动断连，可延迟重试
    DRIVER_CRASH = "driver_crash"


def _is_captcha_error(err: Exception | str) -> bool:
    message = str(err).lower()
    return any(
        keyword in message
        for keyword in (
            "captcha",
            "challenge",
            "hcaptcha",
            "getcaptcha",
            "payload to timeout",
            "valid challenge frame",
            "nonetype",
            "object has no attribute 'locator'",
            "wait for captcha response timeout",
        )
    )


def _is_driver_disconnect_error(err: Exception | str) -> bool:
    message = str(err).lower()
    return any(
        keyword in message
        for keyword in (
            "connection closed while reading from the driver",
            "browsercontext.close: connection closed",
            "cannot read properties of undefined",
            "playwright/driver",
            "node.js v",
        )
    )


async def _fetch_order_items(page: Page) -> List[OrderItem]:
    """Read order history with the browser cookie jar without leaving checkout."""
    history_page = await page.context.new_page()
    try:
        response = await history_page.goto(
            URL_ORDER_HISTORY,
            wait_until="domcontentloaded",
            timeout=15000,
        )
        if response and response.status >= 400:
            raise RuntimeError(f"order history page failed: HTTP {response.status}")
        text = await history_page.locator("body").text_content(timeout=8000)
        data = json.loads(text or "{}")
    finally:
        with suppress(Exception):
            await history_page.close()

    completed_orders: List[OrderItem] = []
    for raw_order in data.get("orders", []):
        order = Order(**raw_order)
        if order.orderType != "PURCHASE":
            continue
        for item in order.items:
            if item.namespace and len(item.namespace) == 32:
                completed_orders.append(item)
    return completed_orders


def get_promotions() -> List[PromotionGame]:
    """获取周免游戏数据"""
    def is_discount_game(prot: dict) -> bool | None:
        with suppress(KeyError, IndexError, TypeError):
            offers = prot["promotions"]["promotionalOffers"][0]["promotionalOffers"]
            for i, offer in enumerate(offers):
                if offer["discountSetting"]["discountPercentage"] == 0:
                    return True

    promotions: List[PromotionGame] = []

    resp = httpx.get(URL_PROMOTIONS, params={"local": "zh-CN"})

    try:
        data = resp.json()
    except JSONDecodeError as err:
        logger.error(f"获取促销信息失败: {err}")
        return []

    with suppress(Exception):
        cache_key = RUNTIME_DIR.joinpath("promotions.json")
        cache_key.parent.mkdir(parents=True, exist_ok=True)
        cache_key.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    # Get store promotion data and <this week free> games
    for e in data["data"]["Catalog"]["searchStore"]["elements"]:
        if not is_discount_game(e):
            continue

        # -----------------------------------------------------------
        # 🟢 智能 URL 识别逻辑
        # -----------------------------------------------------------
        is_bundle = False
        if e.get("offerType") == "BUNDLE":
            is_bundle = True
        
        # 补充检测：分类和标题
        if not is_bundle:
            for cat in e.get("categories", []):
                if "bundle" in cat.get("path", "").lower():
                    is_bundle = True
                    break
        if not is_bundle and "Collection" in e.get("title", ""):
             is_bundle = True

        base_url = URL_PRODUCT_BUNDLES if is_bundle else URL_PRODUCT_PAGE

        try:
            if e.get('offerMappings'):
                slug = e['offerMappings'][0]['pageSlug']
                e["url"] = f"{base_url.rstrip('/')}/{slug}"
            elif e.get("productSlug"):
                e["url"] = f"{base_url.rstrip('/')}/{e['productSlug']}"
            else:
                 e["url"] = f"{base_url.rstrip('/')}/{e.get('urlSlug', 'unknown')}"
        except (KeyError, IndexError):
            logger.debug(f"Failed to get URL: {e}")
            continue

        logger.debug(f"发现周免游戏: {e['url']}")
        promotions.append(PromotionGame(**e))

    # Epic can expose multiple offers for the same namespace. One namespace
    # represents one library asset, so processing duplicates only repeats clicks.
    unique_promotions: dict[str, PromotionGame] = {}
    for promotion in promotions:
        unique_promotions.setdefault(promotion.namespace, promotion)
    return list(unique_promotions.values())


class EpicAgent:
    def __init__(self, page: Page):
        self.page = page
        self.epic_games = EpicGames(self.page)
        self._promotions: List[PromotionGame] = []
        self._ctx_cookies_is_available: bool = False
        self._orders: List[OrderItem] = []
        self._namespaces: List[str] = []
        self._cookies = None

    async def _handle_eula_correction(self) -> bool:
        """
        处理 EULA 修正页面

        Epic Games 在某些情况下会将用户重定向到 EULA 修正页面：
        - 新注册账号首次登录
        - Epic 更新服务条款
        - 账号长期未登录
        - 账号在新设备/地区登录

        页面特点：
        - SPA 单页应用（React + Material UI），内容动态渲染
        - 只有"拒绝"和"接受"两个按钮，无复选框
        - 接受按钮特征：id="accept", type="submit"

        Returns:
            bool: True 表示成功处理 EULA，False 表示无需处理或处理失败
        """
        current_url = self.page.url

        # 检测是否在 EULA 修正页面
        if "correction/eula" not in current_url:
            return False

        logger.warning("⚠️ 检测到 EULA 修正页面，尝试自动接受协议...")

        try:
            # SPA 页面需要等待网络完全空闲
            await self.page.wait_for_load_state("networkidle")

            # 额外等待 React 渲染完成
            await self.page.wait_for_timeout(2000)

            # ============================================================
            # EULA 接受按钮选择器（按优先级排序）
            # 按钮特征: <button id="accept" type="submit">接受</button>
            # ============================================================
            accept_selectors = [
                # 最精确：通过 ID 选择（最稳定）
                "#accept",
                "button#accept",
                "//button[@id='accept']",

                # 通过 type=submit（次优）
                "//button[@type='submit']",

                # 通过文本匹配（多语言）
                "//button[normalize-space(text())='Accept']",
                "//button[normalize-space(text())='接受']",
                "//button[normalize-space(text())='Akzeptieren']",
                "//button[normalize-space(text())='Accepter']",
            ]

            # 尝试点击接受按钮
            for selector in accept_selectors:
                try:
                    btn = self.page.locator(selector).first
                    # 增加等待时间，因为 SPA 需要渲染
                    if await btn.is_visible(timeout=5000):
                        btn_text = await btn.text_content()
                        logger.info(f"📋 点击 EULA 接受按钮: '{btn_text}' | 选择器: {selector}")
                        await btn.click()

                        # 等待页面跳转
                        await self.page.wait_for_load_state("networkidle", timeout=15000)

                        # 验证是否成功跳转
                        new_url = self.page.url
                        if "correction/eula" not in new_url:
                            logger.success("✅ EULA 协议已接受，页面已跳转")
                            return True
                        else:
                            logger.warning("⚠️ 点击后仍在 EULA 页面，尝试下一个选择器")
                except Exception as e:
                    logger.debug(f"EULA 选择器 '{selector}' 失败: {e}")
                    continue

            logger.error("❌ 未能找到 EULA 接受按钮")
            return False

        except Exception as e:
            logger.error(f"❌ 处理 EULA 页面异常: {e}")
            return False

    async def _sync_order_history(self, force: bool = False):
        if self._orders and not force:
            return
        try:
            completed_orders = await _fetch_order_items(self.page)
        except Exception as err:
            logger.warning(err)
            completed_orders = []
        self._orders = completed_orders

    async def _check_orders(self, force: bool = False):
        await self._sync_order_history(force=force)
        self._namespaces = [order.namespace for order in self._orders]
        self._promotions = [p for p in get_promotions() if p.namespace not in self._namespaces]

    async def _should_ignore_task(self) -> tuple[bool, GameCollectResult]:
        """
        检查是否应该忽略任务

        Returns:
            tuple[bool, GameCollectResult]:
                - (True, ALL_OWNED): 所有游戏已在库中，无需领取
                - (False, SUCCESS): 有游戏需要领取
                - (False, EULA_FAILED): EULA 处理失败
                - (False, COOKIE_INVALID): Cookie 无效
                - (False, UNKNOWN_ERROR): 未知错误
        """
        self._ctx_cookies_is_available = False
        await self.page.goto(URL_CLAIM, wait_until="domcontentloaded")

        # ============================================================
        # 🔥 关键修复：等待页面稳定，防止 JS 重定向导致检测遗漏
        # Epic Games 可能会通过 JS 异步重定向到 EULA 页面
        # domcontentloaded 触发时重定向可能还没完成
        # ============================================================
        await self.page.wait_for_timeout(2000)  # 等待 JS 执行完成

        # ============================================================
        # 🔥 EULA 修正页面检测与处理
        # Epic Games 可能会重定向到 EULA 页面，需要自动接受协议
        # ============================================================
        max_eula_attempts = 3
        for attempt in range(max_eula_attempts):
            current_url = self.page.url
            logger.debug(f"📍 当前页面 URL: {current_url}")
            if "correction/eula" in current_url or "corrective=" in current_url:
                logger.warning(f"⚠️ 检测到修正页面（尝试 {attempt + 1}/{max_eula_attempts}）")
                if await self._handle_eula_correction():
                    # EULA 处理成功后，重新导航到目标页面
                    await self.page.goto(URL_CLAIM, wait_until="domcontentloaded")
                    await self.page.wait_for_timeout(2000)  # 再次等待稳定
                else:
                    logger.error("❌ EULA 处理失败，跳过此账号")
                    return False, GameCollectResult.EULA_FAILED
            else:
                break

        # 尝试获取登录状态，增加超时处理
        try:
            status = await self.page.locator("//egs-navigation").get_attribute("isloggedin", timeout=10000)
        except Exception as e:
            # 如果超时，可能还在修正页面或有其他问题
            current_url = self.page.url
            if "correction" in current_url or "eula" in current_url:
                logger.error("❌ 仍在修正页面，无法继续")
                return False, GameCollectResult.EULA_FAILED
            logger.error(f"❌ 获取登录状态超时: {e}")
            return False, GameCollectResult.UNKNOWN_ERROR

        if status == "false":
            logger.error("❌ Cookie 无效，账号未登录")
            return False, GameCollectResult.COOKIE_INVALID
        self._ctx_cookies_is_available = True
        await self._check_orders()
        if not self._promotions:
            return True, GameCollectResult.ALL_OWNED
        return False, GameCollectResult.SUCCESS

    async def collect_epic_games(self) -> GameCollectResult:
        """
        收集 Epic Games 周免游戏

        Returns:
            GameCollectResult: 执行结果
        """
        should_ignore, result = await self._should_ignore_task()

        # 所有游戏已在库中
        if should_ignore:
            logger.success("✅ 所有周免游戏已在库中")
            return GameCollectResult.ALL_OWNED

        # 处理错误情况
        if result != GameCollectResult.SUCCESS:
            # 输出特定格式的错误日志，便于 worker.py 解析
            logger.error(f"❌ GAME_ERROR:{result.value}")
            return result

        # 检查是否有游戏需要领取
        if not self._promotions:
            await self._check_orders()

        if not self._promotions:
            logger.success("✅ 所有周免游戏已在库中")
            return GameCollectResult.ALL_OWNED

        # 输出游戏信息供 worker.py 解析（必须用 INFO 级别）
        for p in self._promotions:
            pj = json.dumps({"title": p.title, "url": p.url}, ensure_ascii=False)
            logger.info(f"发现: {pj}")

        # 执行领取
        if self._promotions:
            try:
                await self.epic_games.collect_weekly_games(self._promotions)
                return GameCollectResult.SUCCESS
            except Exception as e:
                logger.exception(e)
                error_message = str(e).lower()
                if _is_driver_disconnect_error(e):
                    return GameCollectResult.DRIVER_CRASH
                if _is_captcha_error(e):
                    return GameCollectResult.CAPTCHA_FAILED
                if "未能确认领取成功" in str(e) or "checkout" in error_message:
                    return GameCollectResult.CHECKOUT_FAILED
                return GameCollectResult.UNKNOWN_ERROR

        logger.debug("All tasks in the workflow have been completed")
        return GameCollectResult.SUCCESS


class EpicGames:
    def __init__(self, page: Page):
        self.page = page
        self._promotions: List[PromotionGame] = []
        self._orders: List[OrderItem] = []

    @staticmethod
    async def _agree_license(page: Page):
        logger.debug("Agree license")
        with suppress(TimeoutError):
            await page.click("//label[@for='agree']", timeout=4000)
            accept = page.locator("//button//span[text()='Accept']")
            if await accept.is_enabled():
                await accept.click()

    @staticmethod
    async def _active_purchase_container(page: Page, wait_for_surface: bool = True):
        logger.debug("Scanning for purchase container...")

        if wait_for_surface:
            await EpicGames._wait_for_checkout_surface(page)

        button_texts = [
            "PLACE ORDER",
            "Place Order",
            "GET",
            "Get",
            "ADD TO LIBRARY",
            "Add to library",
            "Add To Library",
            "BUY NOW",
            "Buy Now",
            "CONFIRM",
            "Confirm",
            "Confirm Order",
            "Complete Order",
            "Submit Order",
        ]
        css_selectors = [
            "button[data-testid='purchase-button']",
            "button[data-testid='place-order-button']",
            "button[data-testid='confirm-order-button']",
            "button[data-testid*='purchase']",
            "button[data-testid*='order']",
            "button[data-testid*='confirm']",
            "button.payment-btn",
            "button[class*='payment-confirm']",
            "button[class*='confirm']",
            "button[type='submit']",
        ]

        containers: list[tuple[str, Any]] = []
        web_purchase_iframe = page.locator("#webPurchaseContainer iframe").first
        with suppress(Exception):
            if await web_purchase_iframe.is_visible(timeout=1000):
                containers.append(("frameLocator[#webPurchaseContainer iframe]", page.frame_locator("#webPurchaseContainer iframe")))

        purchase_frames = [
            frame
            for frame in page.frames
            if frame != page.main_frame and "/purchase" in frame.url.lower()
        ]
        other_frames = [
            frame
            for frame in page.frames
            if frame != page.main_frame and frame not in purchase_frames
        ]
        containers.extend(
            (f"frame[purchase:{idx}] {frame.url[:180]}", frame)
            for idx, frame in enumerate(purchase_frames)
        )
        containers.append(("page", page))
        for idx, frame in enumerate(page.frames):
            if frame == page.main_frame or frame in purchase_frames:
                continue
            containers.append((f"frame[{idx}] {frame.url[:180]}", frame))

        logger.info(f"🔎 扫描结账容器: {len(containers)} 个候选")

        async def _button_is_usable(btn) -> bool:
            try:
                await btn.wait_for(state="visible", timeout=2500)
                if await btn.is_disabled(timeout=1000):
                    return False
                return True
            except Exception:
                return False

        target_button_texts = {
            "place order",
            "get",
            "add to library",
            "buy now",
            "confirm",
            "confirm order",
            "complete order",
            "submit order",
        }

        def _normalize_button_text(value: str | None) -> str:
            return " ".join((value or "").strip().lower().split())

        async def _find_by_actual_button_text(label: str, container: Any):
            try:
                buttons = await container.locator("button").all()
            except Exception as e:
                logger.debug(f"Button enumeration failed in {label}: {e}")
                return None

            for idx, btn in enumerate(buttons[:24]):
                try:
                    text = _normalize_button_text(await btn.text_content(timeout=1000))
                    aria = _normalize_button_text(await btn.get_attribute("aria-label", timeout=1000))
                    if text not in target_button_texts and aria not in target_button_texts:
                        continue
                    if await _button_is_usable(btn):
                        logger.info(
                            f"✅ 找到结账按钮: text={text!r} aria={aria!r} | "
                            f"容器: {label} | button[{idx}]"
                        )
                        return btn
                    with suppress(Exception):
                        if not await btn.is_disabled(timeout=1000):
                            logger.warning(
                                f"⚠️ 结账按钮可枚举但 visible 检查不稳定，仍尝试点击: "
                                f"text={text!r} aria={aria!r} | 容器: {label} | button[{idx}]"
                            )
                            return btn
                except Exception as e:
                    logger.debug(f"Button enumeration item failed in {label}: {e}")
            return None

        async def _describe_buttons(label: str, container: Any):
            try:
                buttons = await container.locator("button").all()
                logger.warning(f"🔍 {label} 按钮数量: {len(buttons)}")
                for i, btn in enumerate(buttons[:12]):
                    try:
                        text = (await btn.text_content(timeout=1000) or "").strip()
                        aria = await btn.get_attribute("aria-label", timeout=1000)
                        testid = await btn.get_attribute("data-testid", timeout=1000)
                        disabled = await btn.is_disabled(timeout=1000)
                        logger.warning(
                            f"🔍 {label} button[{i}]: text={text!r}, aria={aria!r}, "
                            f"testid={testid!r}, disabled={disabled}"
                        )
                    except Exception as e:
                        logger.warning(f"🔍 {label} button[{i}] inspect failed: {e}")
            except Exception as e:
                logger.warning(f"🔍 {label} list buttons failed: {e}")

        for label, container in containers:
            logger.info(f"🔎 检查结账容器: {label}")

            btn = await _find_by_actual_button_text(label, container)
            if btn is not None:
                return container, btn

            for text_value in button_texts:
                try:
                    btn = container.locator("button", has_text=text_value).first
                    if await _button_is_usable(btn):
                        btn_text = (await btn.text_content(timeout=1000) or "").strip()
                        logger.info(f"✅ 找到结账按钮: {btn_text!r} | 容器: {label} | 文本: {text_value}")
                        return container, btn
                except Exception as e:
                    logger.debug(f"Button text {text_value!r} failed in {label}: {e}")

            for selector in css_selectors:
                try:
                    btn = container.locator(selector).first
                    if await _button_is_usable(btn):
                        btn_text = (await btn.text_content(timeout=1000) or "").strip()
                        logger.info(f"✅ 找到结账按钮: {btn_text!r} | 容器: {label} | 选择器: {selector}")
                        return container, btn
                except Exception as e:
                    logger.debug(f"Button selector {selector!r} failed in {label}: {e}")

        logger.warning("Primary buttons not found. Debugging checkout containers...")
        for label, container in containers:
            await _describe_buttons(label, container)
            retry_btn = await _find_by_actual_button_text(label, container)
            if retry_btn is not None:
                logger.info(f"✅ 调试枚举后找到结账按钮 | 容器: {label}")
                return container, retry_btn

        await EpicGames._save_checkout_debug(page, "button_not_found")

        raise AssertionError("Could not find Place Order button in checkout containers")

    @staticmethod
    async def _current_product_is_owned(page: Page) -> bool:
        """Check only the product CTA; page-wide text contains unrelated owned labels."""
        try:
            button = page.locator("button[data-testid='purchase-cta-button']").first
            if not await button.is_visible(timeout=1000):
                return False
            text = " ".join((await button.text_content(timeout=1000) or "").upper().split())
            return text in {"IN LIBRARY", "OWNED"}
        except Exception:
            return False

    @staticmethod
    async def _wait_for_checkout_surface(page: Page, timeout_ms: int = 45000) -> str:
        """Wait until Epic exposes a checkout surface or a terminal page state."""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if page.url.startswith(URL_CART_SUCCESS):
                return "success_url"

            if any("/purchase" in frame.url.lower() for frame in page.frames):
                return "purchase_frame"

            # Do not issue DOM commands while Epic is creating the purchase
            # frame; Camoufox can leave those protocol calls pending forever.
            await asyncio.sleep(0.5)

        logger.warning("⚠️ 等待结账界面超时，开始保存结账调试现场")
        await EpicGames._save_checkout_debug(page, "checkout_surface_timeout")
        raise TimeoutError("Timed out waiting for checkout iframe or terminal checkout state")

    @staticmethod
    async def _save_checkout_debug(page: Page, reason: str) -> None:
        safe_reason = re.sub(r"[^a-zA-Z0-9_.-]+", "_", reason).strip("_") or "unknown"
        debug_dir = RUNTIME_DIR.joinpath("checkout_debug")
        with suppress(Exception):
            debug_dir.mkdir(parents=True, exist_ok=True)

        async def _bounded(label: str, coro: Any, timeout: float = 5):
            try:
                return await asyncio.wait_for(coro, timeout=timeout)
            except Exception as err:
                logger.warning(f"🧾 保存结账调试信息失败: {label}: {err}")
                return None

        with suppress(Exception):
            main_path = debug_dir.joinpath(f"{safe_reason}_page.html")
            main_content = await _bounded("page.content", page.content())
            if main_content:
                main_path.write_text(main_content, encoding="utf-8")
                RUNTIME_DIR.joinpath("checkout_debug_last.html").write_text(
                    main_content, encoding="utf-8"
                )
                logger.warning(f"🧾 已保存结账主页面 HTML: {main_path}")

        frame_rows = []
        for idx, frame in enumerate(page.frames):
            frame_rows.append({"index": idx, "name": frame.name, "url": frame.url})
            with suppress(Exception):
                frame_path = debug_dir.joinpath(f"{safe_reason}_frame_{idx}.html")
                frame_content = await _bounded(f"frame[{idx}].content", frame.content())
                if frame_content:
                    frame_path.write_text(frame_content, encoding="utf-8")

        with suppress(Exception):
            frames_path = debug_dir.joinpath(f"{safe_reason}_frames.json")
            frames_path.write_text(json.dumps(frame_rows, indent=2, ensure_ascii=False), encoding="utf-8")
            logger.warning(f"🧾 已保存结账 frame 列表: {frames_path}")

        with suppress(Exception):
            screenshot_path = debug_dir.joinpath(f"{safe_reason}.png")
            screenshot_saved = await _bounded(
                "page.screenshot",
                page.screenshot(path=str(screenshot_path), full_page=True),
            )
            if screenshot_saved is not None:
                logger.warning(f"🧾 已保存结账截图: {screenshot_path}")

    @staticmethod
    async def _handle_device_not_supported_modal(page: Page) -> bool:
        """Continue past Epic's intermediate unsupported-device modal."""
        dialog = page.locator("[role='dialog']").filter(has_text=re.compile("Device not supported", re.I)).first

        try:
            await dialog.wait_for(state="visible", timeout=3000)
        except Exception:
            dialog = page.locator("text=/Device not supported/i").first
            try:
                await dialog.wait_for(state="visible", timeout=1000)
            except Exception:
                return False

        body_text = ""
        with suppress(Exception):
            body_text = (await page.locator("body").text_content(timeout=1000) or "").strip()

        if "device not supported" not in body_text.lower():
            return False

        continue_btn = page.locator("button", has_text=re.compile(r"^\s*Continue\s*$", re.I)).last
        try:
            await continue_btn.wait_for(state="visible", timeout=3000)
            if await continue_btn.is_disabled(timeout=1000):
                logger.warning("⚠️ Epic 设备不支持弹窗的 Continue 按钮不可点击")
                return False

            logger.info("ℹ️ Epic 显示设备不支持提示，点击 Continue 继续领取流程")
            clicked = await asyncio.wait_for(
                page.evaluate(
                    """() => {
                        const buttons = Array.from(document.querySelectorAll('button'));
                        const button = buttons.reverse().find((item) => {
                            const text = (item.textContent || '').trim().toLowerCase();
                            return text === 'continue' && !item.disabled;
                        });
                        if (!button) return false;
                        button.scrollIntoView({block: 'center', inline: 'center'});
                        button.click();
                        return true;
                    }"""
                ),
                timeout=6,
            )
            if not clicked:
                await EpicGames._click_product_cta(continue_btn)
            await page.wait_for_timeout(3000)
            return True
        except Exception as err:
            logger.warning(f"⚠️ 处理 Epic 设备不支持弹窗失败: {err}")
            return False

    @staticmethod
    async def _uk_confirm_order(wpc: Any):
        logger.debug("UK confirm order")
        with suppress(TimeoutError):
            accept = wpc.locator("//button[contains(@class, 'payment-confirm__btn')]")
            if await accept.is_enabled(timeout=5000):
                await accept.click()
                return True

    async def _sync_order_history(self) -> None:
        try:
            completed_orders = await _fetch_order_items(self.page)
        except Exception as err:
            logger.warning(f"⚠️ 刷新订单历史失败: {err}")
            completed_orders = []
        self._orders = completed_orders

    async def _order_history_contains(self, namespace: str | None) -> bool:
        if not namespace:
            return False
        await self._sync_order_history()
        return any(order.namespace == namespace for order in self._orders)

    @staticmethod
    def _emit_game_result(title: str, status: str) -> None:
        payload = json.dumps({"title": title, "status": status}, ensure_ascii=False)
        logger.info(f"GAME_RESULT:{payload}")

    @staticmethod
    async def _product_is_owned(page: Page, product_url: str) -> bool:
        """Verify ownership on the product page after checkout closes."""
        try:
            await page.goto(product_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
            purchase_btn = page.locator("//button[@data-testid='purchase-cta-button']").first
            if await purchase_btn.is_visible(timeout=3000):
                text = (await purchase_btn.text_content(timeout=1000) or "").strip().upper()
                return await purchase_btn.is_disabled(timeout=1000) or text in {
                    "IN LIBRARY",
                    "OWNED",
                }
        except Exception as err:
            logger.warning(f"⚠️ 无法验证商品入库状态: {err}")
        return False

    @staticmethod
    async def _click_product_cta(button: Any) -> None:
        """Click Epic product CTA with fallbacks for sticky overlays/animations."""
        try:
            await asyncio.wait_for(
                button.click(timeout=5000, no_wait_after=True), timeout=6
            )
            logger.debug("商品按钮常规点击完成")
            return
        except Exception as first_error:
            logger.warning(f"⚠️ 商品按钮常规点击失败，尝试 force 点击: {first_error}")

        try:
            await asyncio.wait_for(
                button.click(force=True, timeout=5000, no_wait_after=True), timeout=6
            )
            logger.debug("商品按钮 force 点击完成")
        except Exception as force_error:
            raise RuntimeError(f"checkout cta click failed: {force_error}") from force_error

    @staticmethod
    async def _click_product_page_cta(page: Page, expect_checkout: bool = True) -> None:
        """Click the product page CTA and verify the next checkout surface appears."""
        selector = "button[data-testid='purchase-cta-button']"
        button = page.locator(selector).first

        async def _confirm_surface(label: str, timeout_ms: int = 60000) -> bool:
            if not expect_checkout:
                return True
            try:
                surface = await EpicGames._wait_for_checkout_surface(page, timeout_ms=timeout_ms)
                logger.info(f"✅ 商品页 CTA {label} 后进入结账状态: {surface}")
                return True
            except Exception as err:
                logger.warning(f"⚠️ 商品页 CTA {label} 后未出现结账界面: {err}")
                return False

        async def _checkout_is_pending() -> bool:
            with suppress(Exception):
                progress = button.locator("[role='progressbar']")
                return await button.is_disabled(timeout=1000) and await progress.count() > 0
            return False

        try:
            await button.wait_for(state="visible", timeout=5000)
            clicked_text = await asyncio.wait_for(
                button.evaluate(
                    "button => { const text = button.textContent || ''; "
                    "setTimeout(() => button.click(), 5000); return text; }"
                ),
                timeout=6,
            )
            logger.info(f"商品页 CTA 原生点击完成: {clicked_text.strip()!r}")
            await page.wait_for_timeout(1500)
            if await _confirm_surface("原生点击"):
                return
            if await _checkout_is_pending():
                raise RuntimeError("Epic checkout initialization remained pending after native click")
        except Exception as native_error:
            logger.warning(f"⚠️ 商品页 CTA 原生点击失败: {native_error}")

        try:
            await button.wait_for(state="visible", timeout=5000)
            # Epic opens checkout asynchronously inside an iframe. Waiting for
            # navigation here can hang even though the click was already sent.
            await asyncio.wait_for(
                button.click(timeout=10000, no_wait_after=True), timeout=11
            )
            logger.info("商品页 CTA 常规点击完成")
            await page.wait_for_timeout(1500)
            if await _confirm_surface("常规点击"):
                return
            if await _checkout_is_pending():
                raise RuntimeError("Epic checkout initialization remained pending after click")
        except Exception as click_error:
            logger.warning(f"⚠️ 商品页 CTA 常规点击失败: {click_error}")

        try:
            await asyncio.wait_for(
                button.evaluate("button => button.focus()"),
                timeout=3,
            )
            await asyncio.wait_for(page.keyboard.press("Enter"), timeout=5)
            logger.info("商品页 CTA 键盘 Enter 激活完成")
            await page.wait_for_timeout(1500)
            if await _confirm_surface("键盘 Enter"):
                return
            if await _checkout_is_pending():
                raise RuntimeError("Epic checkout initialization remained pending after Enter")
        except Exception as keyboard_error:
            logger.warning(f"⚠️ 商品页 CTA 键盘 Enter 激活失败: {keyboard_error}")

        try:
            await asyncio.wait_for(
                button.click(force=True, timeout=5000, no_wait_after=True), timeout=6
            )
            logger.info("商品页 CTA force 点击完成")
            await page.wait_for_timeout(2000)
            if await _confirm_surface("force 点击"):
                return
        except Exception as force_error:
            logger.warning(f"⚠️ 商品页 CTA force 点击失败: {force_error}")

        try:
            result = await asyncio.wait_for(
                page.evaluate(
                    """(selector) => {
                        const button = document.querySelector(selector);
                        if (!button) return {clicked: false, reason: 'missing'};
                        button.scrollIntoView({block: 'center', inline: 'center'});
                        const rect = button.getBoundingClientRect();
                        const x = rect.left + rect.width / 2;
                        const y = rect.top + rect.height / 2;
                        for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                            button.dispatchEvent(new MouseEvent(type, {
                                bubbles: true,
                                cancelable: true,
                                view: window,
                                clientX: x,
                                clientY: y,
                            }));
                        }
                        button.click();
                        return {clicked: true, text: button.textContent || ''};
                    }""",
                    selector,
                ),
                timeout=6,
            )
            if result and result.get("clicked"):
                logger.info(f"商品页 CTA JS click 完成: {result.get('text', '').strip()!r}")
                await page.wait_for_timeout(2000)
                if await _confirm_surface("JS click"):
                    return
            logger.warning(f"⚠️ 商品页 CTA JS click 未命中: {result}")
        except Exception as js_error:
            logger.warning(f"⚠️ 商品页 CTA JS click 失败: {js_error}")

        raise RuntimeError("product page cta click did not open checkout surface")

    @staticmethod
    async def _click_checkout_cta(container: Any, button: Any) -> None:
        """Click checkout CTA as a real browser action, with JS only as fallback."""
        try:
            clicked = await asyncio.wait_for(
                button.evaluate(
                    "button => { setTimeout(() => button.click(), 5000); return true; }"
                ),
                timeout=6,
            )
            if clicked:
                logger.info("结账 CTA 原生点击完成")
                return
        except Exception as native_error:
            logger.warning(f"⚠️ 结账 CTA 原生点击失败，尝试 Playwright 点击: {native_error}")

        try:
            await asyncio.wait_for(
                button.click(timeout=10000, no_wait_after=True), timeout=11
            )
            logger.info("结账 CTA 常规点击完成")
            return
        except Exception as click_error:
            logger.warning(f"⚠️ 结账 CTA 常规点击失败，尝试 JS click: {click_error}")

        try:
            clicked = await asyncio.wait_for(
                button.evaluate(
                    """(button) => {
                        const targets = new Set([
                            'add to library',
                            'place order',
                            'get',
                            'confirm',
                            'confirm order',
                            'complete order',
                            'submit order',
                            'buy now',
                        ]);
                        const text = (button.textContent || '').trim().toLowerCase().replace(/\\s+/g, ' ');
                        const aria = (button.getAttribute('aria-label') || '').trim().toLowerCase().replace(/\\s+/g, ' ');
                        if (button.disabled || (!targets.has(text) && !targets.has(aria))) return false;
                        button.scrollIntoView({block: 'center', inline: 'center'});
                        const rect = button.getBoundingClientRect();
                        const x = rect.left + rect.width / 2;
                        const y = rect.top + rect.height / 2;
                        for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                            button.dispatchEvent(new MouseEvent(type, {
                                bubbles: true,
                                cancelable: true,
                                view: window,
                                clientX: x,
                                clientY: y,
                            }));
                        }
                        button.click();
                        return true;
                    }"""
                ),
                timeout=6,
            )
            if clicked:
                logger.info("结账 CTA JS click 完成")
                return
        except Exception as js_error:
            logger.warning(f"⚠️ 结账 CTA JS click 失败，尝试 force click: {js_error}")

            await asyncio.wait_for(
                button.click(force=True, timeout=5000, no_wait_after=True), timeout=6
            )

    @staticmethod
    async def _has_visible_hcaptcha_challenge(page: Page, timeout_ms: int = 10000) -> bool:
        """Only invoke the solver when Epic actually renders a visual challenge."""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            for frame in page.frames:
                url = frame.url or ""
                if "hcaptcha.com" not in url or "frame=challenge" not in url:
                    continue
                with suppress(Exception):
                    challenge = frame.locator("div.challenge-view").first
                    if await asyncio.wait_for(
                        challenge.is_visible(timeout=1500), timeout=2
                    ):
                        return True
            await asyncio.sleep(0.5)
        return False

    async def _confirm_checkout_success(
        self,
        page: Page,
        product_url: str,
        namespace: str | None = None,
        check_order_history: bool = True,
    ) -> bool:
        if page.url.startswith(URL_CART_SUCCESS):
            logger.success("🎉 领取成功：已进入结账成功页面")
            return True

        if check_order_history and await self._order_history_contains(namespace):
            logger.success("🎉 领取成功：订单历史已确认入库")
            return True

        return False

    async def _wait_for_checkout_confirmation(
        self,
        page: Page,
        product_url: str,
        namespace: str | None = None,
        timeout_ms: int = 75000,
        interval_ms: int = 5000,
    ) -> bool:
        """Poll Epic's durable state after checkout/captcha UI becomes unreliable."""
        deadline = time.monotonic() + timeout_ms / 1000
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            if await self._confirm_checkout_success(
                page,
                product_url,
                namespace,
                check_order_history=False,
            ):
                return True
            remaining = max(0, int(deadline - time.monotonic()))
            logger.info(f"🔁 等待入库确认: 第 {attempt} 次复核，剩余约 {remaining}s")
            await asyncio.sleep(interval_ms / 1000)

        return await self._confirm_checkout_success(
            page,
            product_url,
            namespace,
            check_order_history=True,
        )

    async def _handle_instant_checkout(
        self,
        page: Page,
        product_url: str,
        namespace: str | None = None,
    ) -> bool:
        logger.info("🚀 开始即时结账流程...")
        agent = AgentV(page=page, agent_config=settings)
        challenge_error: Exception | None = None

        try:
            await self._handle_device_not_supported_modal(page)
            checkout_state = await self._wait_for_checkout_surface(page)
            if checkout_state == "device_not_supported":
                if not await self._handle_device_not_supported_modal(page):
                    logger.error("❌ Epic 设备不支持弹窗无法继续")
                    return False
                checkout_state = await self._wait_for_checkout_surface(page)

            if checkout_state in {"success_url", "owned_text"} and await self._confirm_checkout_success(
                page, product_url, namespace
            ):
                return True

            wpc, payment_btn = await self._active_purchase_container(page, wait_for_surface=False)
            if await self._handle_device_not_supported_modal(page):
                wpc, payment_btn = await self._active_purchase_container(page)

            logger.debug(f"点击支付按钮: {await payment_btn.text_content()}")
            await self._click_checkout_cta(wpc, payment_btn)
            await asyncio.sleep(2)

            # 大多数免费订单在点击后可直接完成，先用后台请求确认，避免无条件进入验证码库。
            if await self._wait_for_checkout_confirmation(
                page,
                product_url,
                namespace,
                timeout_ms=15000,
                interval_ms=3000,
            ):
                return True

            if await self._has_visible_hcaptcha_challenge(page):
                try:
                    logger.info("检测到可见 hCaptcha 挑战，启动验证码识别")
                    if agent._captcha_payload_queue.empty():
                        logger.info("hCaptcha payload 尚未捕获，刷新挑战后重新获取")
                        await agent.robotic_arm.refresh_challenge()
                        await asyncio.sleep(1.5)
                    signal = await agent.wait_for_challenge()
                    if signal != ChallengeSignal.SUCCESS:
                        raise RuntimeError(f"hcaptcha challenge returned {signal}")
                except Exception as e:
                    challenge_error = e
                    logger.warning(f"⚠️ 验证码流程未确认成功: {e}")
            else:
                logger.info("未出现可见 hCaptcha 挑战，继续等待 Epic 入库结果")

            try:
                if not await payment_btn.is_visible():
                    if await self._confirm_checkout_success(page, product_url, namespace):
                        return True
                    logger.warning("⚠️ 支付按钮已消失，但入库状态未确认")
            except Exception:
                if await self._confirm_checkout_success(page, product_url, namespace):
                    return True
                logger.warning("⚠️ 结账 iframe 已关闭，但入库状态未确认")

            if challenge_error:
                if await self._wait_for_checkout_confirmation(
                    page,
                    product_url,
                    namespace,
                    timeout_ms=25000,
                ):
                    return True
                logger.error(f"❌ 即时结账无法确认，验证码异常: {challenge_error}")
                raise RuntimeError(f"captcha checkout verification failed: {challenge_error}")

            if challenge_error is None:
                with suppress(Exception):
                    await self._click_checkout_cta(wpc, payment_btn)
                    await asyncio.sleep(2)

            if await self._wait_for_checkout_confirmation(
                page,
                product_url,
                namespace,
                timeout_ms=30000,
                interval_ms=3000,
            ):
                return True

        except Exception as err:
            if _is_captcha_error(err):
                raise RuntimeError(f"captcha checkout verification failed: {err}") from err
            if _is_driver_disconnect_error(err):
                raise RuntimeError(f"driver checkout failed: {err}") from err
            logger.error(f"❌ 即时结账失败: {err}")
            return False
        finally:
            # AgentV registers a page response listener; keeping it across games
            # makes it process detached hCaptcha frames from the previous checkout.
            with suppress(Exception):
                page.remove_listener("response", agent._task_handler)

        logger.error("❌ 即时结账无法确认成功")
        return False

    async def add_promotion_to_cart(
        self, page: Page, promotions: List[PromotionGame]
    ) -> tuple[bool, dict[str, str]]:
        has_pending_cart_items = False
        outcomes: dict[str, str] = {}

        # Run the product click inside the page. Epic's click handler can keep
        # Playwright's locator/evaluate command waiting even after the event was
        # delivered; an init-script observer avoids coupling those two steps.
        await page.add_init_script(
            """
            (() => {
                if (window.top !== window) return;
                let observer;
                const arm = () => {
                    const button = document.querySelector(
                        "button[data-testid='purchase-cta-button']"
                    );
                    if (!button || button.disabled || window.__epicKioskClicked) return;
                    const text = (button.textContent || '').trim().toUpperCase();
                    if (text !== 'GET' && text !== 'PURCHASE') return;
                    window.__epicKioskClicked = true;
                    if (observer) observer.disconnect();
                    setTimeout(() => button.click(), 750);
                    let continueAttempts = 0;
                    const continueTimer = setInterval(() => {
                        continueAttempts += 1;
                        const continueButton = Array.from(
                            document.querySelectorAll("[role='dialog'] button, button")
                        ).find((item) => {
                            const label = (item.textContent || '').trim().toUpperCase();
                            const rect = item.getBoundingClientRect();
                            return label === 'CONTINUE' && !item.disabled
                                && rect.width > 0 && rect.height > 0;
                        });
                        if (continueButton) {
                            clearInterval(continueTimer);
                            continueButton.click();
                        } else if (continueAttempts >= 60) {
                            clearInterval(continueTimer);
                        }
                    }, 500);
                };
                const start = () => {
                    observer = new MutationObserver(arm);
                    observer.observe(document.documentElement, {
                        childList: true,
                        subtree: true,
                        attributes: true,
                        attributeFilter: ['disabled'],
                    });
                    arm();
                };
                if (document.documentElement) start();
                else document.addEventListener('DOMContentLoaded', start, {once: true});
            })();
            """
        )

        for promotion in promotions:
            url = promotion.url
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as err:
                if _is_driver_disconnect_error(err):
                    raise RuntimeError(f"driver navigation failed: {err}") from err
                raise

            # 404 检测
            page_title = await page.title()
            if "404" in page_title or "Page Not Found" in page_title:
                logger.error(f"❌ Invalid URL (404 Page): {url}")
                outcomes[promotion.title] = "failed"
                self._emit_game_result(promotion.title, "failed")
                continue

            # 处理年龄限制弹窗
            try:
                continue_btn = page.locator("//button//span[text()='Continue']")
                if await continue_btn.is_visible(timeout=5000):
                    await continue_btn.click()
            except Exception:
                pass 

            # ------------------------------------------------------------
            # 🔥 按钮识别与状态判断
            # ------------------------------------------------------------

            # 1. 尝试找到主按钮
            purchase_btn = page.locator("//button[@data-testid='purchase-cta-button']").first

            # 2. 检查按钮可见性
            try:
                if not await purchase_btn.is_visible(timeout=5000):
                    all_text = await asyncio.wait_for(
                        page.locator("body").text_content(timeout=5000),
                        timeout=6,
                    )
                    if "In Library" in all_text or "Owned" in all_text:
                        logger.success("✅ 游戏已在库中")
                        outcomes[promotion.title] = "owned"
                        self._emit_game_result(promotion.title, "owned")
                        continue
                    logger.warning(f"⚠️ 找不到购买按钮")
                    outcomes[promotion.title] = "failed"
                    self._emit_game_result(promotion.title, "failed")
                    continue
            except Exception as err:
                logger.warning(f"⚠️ 检查购买按钮失败: {err}")
                outcomes[promotion.title] = "failed"
                self._emit_game_result(promotion.title, "failed")
                continue

            # 3. 获取按钮信息
            btn_text = await asyncio.wait_for(
                purchase_btn.text_content(timeout=5000),
                timeout=6,
            )
            if not btn_text: btn_text = ""
            btn_text = btn_text.strip()
            btn_text_upper = btn_text.upper()
            is_disabled = await asyncio.wait_for(
                purchase_btn.is_disabled(timeout=5000),
                timeout=6,
            )

            # 4. 打印按钮状态（关键信息）
            logger.info(f"📋 按钮状态: '{btn_text}' | 禁用: {is_disabled}")

            # 5. 根据状态判断
            if any(s in btn_text_upper for s in ["IN LIBRARY", "OWNED"]):
                logger.success(f"✅ 游戏已在库中")
                outcomes[promotion.title] = "owned"
                self._emit_game_result(promotion.title, "owned")
                continue

            if any(s in btn_text_upper for s in ["UNAVAILABLE", "COMING SOON"]):
                logger.warning(f"⚠️ 当前不可领取: {btn_text}")
                outcomes[promotion.title] = "failed"
                self._emit_game_result(promotion.title, "failed")
                continue

            if "CART" in btn_text_upper:
                logger.info(f"🛒 加入购物车")
                await self._click_product_page_cta(page, expect_checkout=False)
                has_pending_cart_items = True
                outcomes[promotion.title] = "cart"
                continue

            # 6. 尝试领取
            # 单个游戏的结账/验证码失败不应中断整轮任务；记录失败后继续处理后续游戏。
            try:
                # 初始化脚本已经在页面内部点击 Get；这里只等待结账界面。
                surface = await self._wait_for_checkout_surface(page, timeout_ms=60000)
                logger.info(f"✅ 商品页进入结账状态: {surface}")

                # 点击后，转入即时结账流程
                checkout_success = await self._handle_instant_checkout(
                    page,
                    url,
                    promotion.namespace,
                )
            except Exception as err:
                if _is_driver_disconnect_error(err):
                    raise RuntimeError(f"driver checkout failed: {err}") from err
                logger.warning(f"⚠️ 游戏领取失败，继续处理其他周免游戏: {promotion.title} - {err}")
                outcomes[promotion.title] = "failed"
                self._emit_game_result(promotion.title, "failed")
                continue

            if checkout_success:
                outcomes[promotion.title] = "claimed"
                self._emit_game_result(promotion.title, "claimed")
            else:
                outcomes[promotion.title] = "failed"
                self._emit_game_result(promotion.title, "failed")
            # ------------------------------------------------------------

        return has_pending_cart_items, outcomes

    async def _empty_cart(self, page: Page, wait_rerender: int = 30) -> bool | None:
        has_paid_free = False
        try:
            cards = await page.query_selector_all("//div[@data-testid='offer-card-layout-wrapper']")
            for card in cards:
                is_free = await card.query_selector("//span[text()='Free']")
                if not is_free:
                    has_paid_free = True
                    wishlist_btn = await card.query_selector(
                        "//button//span[text()='Move to wishlist']"
                    )
                    await wishlist_btn.click()

            if has_paid_free and wait_rerender:
                wait_rerender -= 1
                await page.wait_for_timeout(2000)
                return await self._empty_cart(page, wait_rerender)
            return True
        except TimeoutError as err:
            logger.warning(f"清空购物车失败: {err}")
            return False

    async def _purchase_free_game(self, max_attempts: int = 2) -> bool:
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            await self.page.goto(URL_CART, wait_until="domcontentloaded")
            logger.debug("Move ALL paid games from the shopping cart out")
            await self._empty_cart(self.page)

            agent = AgentV(page=self.page, agent_config=settings)
            try:
                await self.page.click("//button//span[text()='Check Out']")
                await self._agree_license(self.page)
                logger.debug("Move to webPurchaseContainer iframe")
                wpc, payment_btn = await self._active_purchase_container(self.page)
                logger.debug("Click payment button")
                await self._uk_confirm_order(wpc)
                if await payment_btn.is_visible():
                    await payment_btn.click(force=True)
                await agent.wait_for_challenge()
                await self.page.wait_for_url(f"{URL_CART_SUCCESS}**", timeout=30000)
                return True
            except Exception as err:
                last_error = err
                if _is_driver_disconnect_error(err):
                    raise RuntimeError(f"driver cart checkout failed: {err}") from err
                if self.page.url.startswith(URL_CART_SUCCESS):
                    return True
                logger.warning(f"⚠️ 购物车结账失败 [{attempt}/{max_attempts}]: {err}")
                if attempt < max_attempts:
                    await self.page.reload()

        if last_error and (
            "captcha" in str(last_error).lower() or "challenge" in str(last_error).lower()
        ):
            raise RuntimeError(f"captcha cart checkout failed: {last_error}")
        return False

    @retry(retry=retry_if_exception_type(TimeoutError), stop=stop_after_attempt(2), reraise=True)
    async def collect_weekly_games(self, promotions: List[PromotionGame]):
        has_cart_items, outcomes = await self.add_promotion_to_cart(self.page, promotions)

        if has_cart_items:
            cart_success = await self._purchase_free_game()
            cart_promotions = [p for p in promotions if outcomes.get(p.title) == "cart"]
            if cart_success:
                for promotion in cart_promotions:
                    outcomes[promotion.title] = "claimed"
                    self._emit_game_result(promotion.title, "claimed")
                logger.success("🎉 购物车游戏领取成功")
            else:
                for promotion in cart_promotions:
                    outcomes[promotion.title] = "failed"
                    self._emit_game_result(promotion.title, "failed")
                logger.error("❌ 购物车游戏领取失败")

        failed = [title for title, status in outcomes.items() if status == "failed"]
        if failed:
            raise RuntimeError(f"以下游戏未能确认领取成功: {', '.join(failed)}")

        logger.success("🎉 任务完成（已领取或已在库中）")
