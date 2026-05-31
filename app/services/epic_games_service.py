# -*- coding: utf-8 -*-
# Time       : 2022/1/16 0:25
# Author     : QIN2DIM
# GitHub     : https://github.com/QIN2DIM
# Description: 游戏商城控制句柄

import json
from contextlib import suppress
from enum import Enum
from json import JSONDecodeError
from typing import Any, List

import httpx
from hcaptcha_challenger.agent import AgentV
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

    # 失败：未知错误
    UNKNOWN_ERROR = "unknown_error"


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

    return promotions


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

    async def _sync_order_history(self):
        if self._orders:
            return
        completed_orders: List[OrderItem] = []
        try:
            await self.page.goto("https://www.epicgames.com/account/v2/payment/ajaxGetOrderHistory")
            text_content = await self.page.text_content("//pre")
            data = json.loads(text_content)
            for _order in data["orders"]:
                order = Order(**_order)
                if order.orderType != "PURCHASE":
                    continue
                for item in order.items:
                    if not item.namespace or len(item.namespace) != 32:
                        continue
                    completed_orders.append(item)
        except Exception as err:
            logger.warning(err)
        self._orders = completed_orders

    async def _check_orders(self):
        await self._sync_order_history()
        self._namespaces = self._namespaces or [order.namespace for order in self._orders]
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
                return GameCollectResult.UNKNOWN_ERROR

        logger.debug("All tasks in the workflow have been completed")
        return GameCollectResult.SUCCESS


class EpicGames:
    def __init__(self, page: Page):
        self.page = page
        self._promotions: List[PromotionGame] = []

    @staticmethod
    async def _agree_license(page: Page):
        logger.debug("Agree license")
        with suppress(TimeoutError):
            await page.click("//label[@for='agree']", timeout=4000)
            accept = page.locator("//button//span[text()='Accept']")
            if await accept.is_enabled():
                await accept.click()

    @staticmethod
    async def _active_purchase_container(page: Page):
        logger.debug("Scanning for purchase container...")

        # Epic 的新结账页不稳定：确认按钮可能在 webPurchase iframe、
        # 其它 purchase iframe、甚至主页面弹层里。这里不再只选第一个 iframe，
        # 而是扫描主页面和所有 frame，避免命中无关 iframe 后误报。
        await page.wait_for_timeout(3000)

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

        containers: list[tuple[str, Any]] = [("page", page)]
        for idx, frame in enumerate(page.frames):
            if frame == page.main_frame:
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

        with suppress(Exception):
            debug_path = RUNTIME_DIR.joinpath("checkout_debug_last.html")
            debug_path.parent.mkdir(parents=True, exist_ok=True)
            debug_path.write_text(await page.content(), encoding="utf-8")
            logger.warning(f"🧾 已保存结账页调试 HTML: {debug_path}")

        raise AssertionError("Could not find Place Order button in checkout containers")

    @staticmethod
    async def _handle_device_not_supported_modal(page: Page) -> bool:
        """Continue past Epic's intermediate unsupported-device modal."""
        dialog = page.locator("[role='dialog']").filter(has_text="Device not supported").first

        try:
            await dialog.wait_for(state="visible", timeout=3000)
        except Exception:
            return False

        body_text = ""
        with suppress(Exception):
            body_text = (await dialog.text_content(timeout=1000) or "").strip()

        if "not compatible with your current device" not in body_text:
            return False

        continue_btn = dialog.locator("button", has_text="Continue").first
        try:
            await continue_btn.wait_for(state="visible", timeout=3000)
            if await continue_btn.is_disabled(timeout=1000):
                logger.warning("⚠️ Epic 设备不支持弹窗的 Continue 按钮不可点击")
                return False

            logger.info("ℹ️ Epic 显示设备不支持提示，点击 Continue 继续领取流程")
            await continue_btn.click(force=True)
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

    async def _handle_instant_checkout(self, page: Page):
        logger.info("🚀 开始即时结账流程...")
        agent = AgentV(page=page, agent_config=settings)

        try:
            await self._handle_device_not_supported_modal(page)
            wpc, payment_btn = await self._active_purchase_container(page)
            if await self._handle_device_not_supported_modal(page):
                wpc, payment_btn = await self._active_purchase_container(page)

            logger.debug(f"点击支付按钮: {await payment_btn.text_content()}")
            await payment_btn.click(force=True)
            await page.wait_for_timeout(3000)

            try:
                logger.debug("检查验证码...")
                await agent.wait_for_challenge()
            except Exception as e:
                logger.debug(f"验证码检测跳过: {e}")

            try:
                if not await payment_btn.is_visible():
                     logger.success("🎉 领取成功：支付按钮已消失")
                     return
            except Exception:
                logger.success("🎉 领取成功：iframe 已关闭")
                return

            with suppress(Exception):
                await payment_btn.click(force=True)
                await page.wait_for_timeout(2000)

            logger.success("🎉 游戏领取成功！")

        except Exception as err:
            logger.warning(f"⚠️ 即时结账警告（游戏可能已领取）: {err}")
            await page.reload()

    async def add_promotion_to_cart(self, page: Page, urls: List[str]) -> bool:
        has_pending_cart_items = False

        for url in urls:
            await page.goto(url, wait_until="load")

            # 404 检测
            title = await page.title()
            if "404" in title or "Page Not Found" in title:
                logger.error(f"❌ Invalid URL (404 Page): {url}")
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
                    all_text = await page.locator("body").text_content()
                    if "In Library" in all_text or "Owned" in all_text:
                         logger.success(f"✅ 游戏已在库中")
                         continue
                    logger.warning(f"⚠️ 找不到购买按钮")
                    continue
            except Exception:
                pass

            # 3. 获取按钮信息
            btn_text = await purchase_btn.text_content()
            if not btn_text: btn_text = ""
            btn_text = btn_text.strip()
            btn_text_upper = btn_text.upper()
            is_disabled = await purchase_btn.is_disabled()

            # 4. 打印按钮状态（关键信息）
            logger.info(f"📋 按钮状态: '{btn_text}' | 禁用: {is_disabled}")

            # 5. 根据状态判断
            if is_disabled:
                logger.success(f"✅ 游戏已在库中")
                continue

            if any(s in btn_text_upper for s in ["IN LIBRARY", "OWNED", "UNAVAILABLE", "COMING SOON"]):
                logger.success(f"✅ 游戏已在库中")
                continue

            if "CART" in btn_text_upper:
                logger.info(f"🛒 加入购物车")
                await purchase_btn.click()
                has_pending_cart_items = True
                continue

            # 6. 尝试领取
            # 只要不是黑名单，也不是购物车，统统当做 "Get/Purchase" 直接点击！
            logger.debug(f"⚡️ 尝试点击按钮: {btn_text}")
            await purchase_btn.click()

            # 点击后，转入即时结账流程
            await self._handle_instant_checkout(page)
            # ------------------------------------------------------------

        return has_pending_cart_items

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

    async def _purchase_free_game(self):
        await self.page.goto(URL_CART, wait_until="domcontentloaded")
        logger.debug("Move ALL paid games from the shopping cart out")
        await self._empty_cart(self.page)

        agent = AgentV(page=self.page, agent_config=settings)
        await self.page.click("//button//span[text()='Check Out']")
        await self._agree_license(self.page)

        try:
            logger.debug("Move to webPurchaseContainer iframe")
            wpc, payment_btn = await self._active_purchase_container(self.page)
            logger.debug("Click payment button")
            await self._uk_confirm_order(wpc)
            await agent.wait_for_challenge()
        except Exception as err:
            logger.warning(f"验证码解决失败: {err}")
            await self.page.reload()
            return await self._purchase_free_game()

    @retry(retry=retry_if_exception_type(TimeoutError), stop=stop_after_attempt(2), reraise=True)
    async def collect_weekly_games(self, promotions: List[PromotionGame]):
        urls = [p.url for p in promotions]
        has_cart_items = await self.add_promotion_to_cart(self.page, urls)

        if has_cart_items:
            await self._purchase_free_game()
            try:
                await self.page.wait_for_url(URL_CART_SUCCESS)
                logger.success("🎉 购物车游戏领取成功")
            except TimeoutError:
                logger.warning("购物车游戏领取失败")
        else:
            logger.success("🎉 任务完成（已领取或已在库中）")
