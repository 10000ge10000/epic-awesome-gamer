# -*- coding: utf-8 -*-
"""
@Time    : 2025/7/16 22:13
@Author  : QIN2DIM
@GitHub  : https://github.com/QIN2DIM
@Desc    :
"""
import asyncio
import json
import time
from contextlib import suppress
from enum import Enum

from hcaptcha_challenger.agent import AgentV
from loguru import logger
from playwright.async_api import expect, Page, Response

from settings import settings

URL_CLAIM = "https://store.epicgames.com/en-US/free-games"


class ErrorType(Enum):
    """
    错误类型枚举，用于精细化区分不同错误，便于前端展示不同提示

    设计思路：
    - 每种错误类型对应不同的用户操作建议
    - 前端根据错误类型展示不同的弹窗内容
    - 便于日志分析和问题排查
    """
    # 成功，无错误
    SUCCESS = "success"

    # 账号或密码错误 - 需要用户检查密码重新提交
    INVALID_CREDENTIALS = "invalid_credentials"

    # 账号被锁定 - 需要用户联系 Epic 客服
    ACCOUNT_LOCKED = "account_locked"

    # EULA 协议处理失败 - 需要用户手动登录 Epic 接受协议
    EULA_FAILED = "eula_failed"

    # 验证码识别失败/超时 - 建议用户稍后重试
    CAPTCHA_FAILED = "captcha_failed"

    # 登录超时 - 可能是网络问题，建议稍后重试
    LOGIN_TIMEOUT = "login_timeout"

    # 网络超时 - Epic 服务不可达
    NETWORK_TIMEOUT = "network_timeout"

    # Cookie 无效 - 需要重新登录
    COOKIE_INVALID = "cookie_invalid"

    # 未知错误 - 需要用户查看日志
    UNKNOWN = "unknown"


class LoginFailedException(Exception):
    """
    登录失败异常

    携带错误类型信息，便于上层调用者判断具体失败原因
    """
    def __init__(self, error_type: ErrorType, message: str = ""):
        self.error_type = error_type
        self.message = message
        super().__init__(message)


class EpicAuthorization:

    def __init__(self, page: Page):
        self.page = page

        self._is_login_success_signal = asyncio.Queue()
        self._is_refresh_csrf_signal = asyncio.Queue()
        self._login_error_code = None  # 存储登录错误码

    async def _on_response_anything(self, r: Response):
        if r.request.method != "POST" or "talon" in r.url:
            return

        with suppress(Exception):
            result = await r.json()

            # 记录所有 POST 响应的 URL，便于调试
            logger.debug(f"📡 API 响应: {r.url} | 状态码: {r.status}")

            if "/id/api/login" in r.url:
                # 记录完整的登录 API 响应
                logger.debug(f"🔍 登录 API 完整响应: {json.dumps(result, ensure_ascii=False, indent=2)}")
                if result.get("errorCode"):
                    # 记录错误码并通知登录失败
                    self._login_error_code = result.get("errorCode")
                    error_msg = result.get("errorMessage", "未知错误")
                    # 记录完整的错误信息
                    logger.error(f"❌ 登录失败: errorCode={self._login_error_code}, message={error_msg}")
                    logger.error(f"❌ 完整错误响应: {json.dumps(result, ensure_ascii=False)}")
                    # 放入失败信号，中断等待
                    self._is_login_success_signal.put_nowait({"error": True, "code": self._login_error_code, "full_response": result})
                else:
                    # 登录成功，记录 accountId
                    if result.get("accountId"):
                        logger.success(f"✅ 登录 API 返回成功: accountId={result.get('accountId')}")
            elif "/id/api/analytics" in r.url and result.get("accountId"):
                self._is_login_success_signal.put_nowait(result)
            elif "/account/v2/refresh-csrf" in r.url and result.get("success", False) is True:
                self._is_refresh_csrf_signal.put_nowait(result)

    async def _handle_right_account_validation(self):
        """
        以下验证仅会在登录成功后出现
        Returns:

        """
        await self.page.goto("https://www.epicgames.com/account/personal", wait_until="networkidle")

        btn_ids = ["#link-success", "#login-reminder-prompt-setup-tfa-skip", "#yes"]

        # == 账号长期不登录需要做的额外验证 == #

        while self._is_refresh_csrf_signal.empty() and btn_ids:
            await self.page.wait_for_timeout(500)
            action_chains = btn_ids.copy()
            for action in action_chains:
                with suppress(Exception):
                    reminder_btn = self.page.locator(action)
                    await expect(reminder_btn).to_be_visible(timeout=1000)
                    await reminder_btn.click(timeout=1000)
                    btn_ids.remove(action)

    async def _login(self) -> tuple[bool, ErrorType] | None:
        """
        执行登录流程

        Returns:
            tuple[bool, ErrorType]: (是否成功, 错误类型)
            - (True, ErrorType.SUCCESS): 登录成功
            - (False, ErrorType.INVALID_CREDENTIALS): 账号或密码错误
            - (False, ErrorType.ACCOUNT_LOCKED): 账号被锁定
            - (False, ErrorType.CAPTCHA_FAILED): 验证码识别失败
            - (False, ErrorType.LOGIN_TIMEOUT): 登录超时
            - None: 异常情况
        """
        # 重置错误码
        self._login_error_code = None

        # 登录 API 通常会在 15 秒内直接返回。仅在首轮等待未成功时
        # 初始化验证码 Agent，避免无验证码账号仍启动昂贵的 HSW 处理。
        agent: AgentV | None = None
        captcha_task: asyncio.Task | None = None
        result_task: asyncio.Task | None = None

        # {{< SIGN IN PAGE >}}
        logger.debug("Login with Email")

        # 用于记录验证码处理是否成功
        captcha_success = False

        try:
            point_url = "https://www.epicgames.com/account/personal?lang=en-US&productName=egs&sessionInvalidated=true"
            await self.page.goto(point_url, wait_until="domcontentloaded")

            # 1. 使用电子邮件地址登录
            email_input = self.page.locator("#email")
            await email_input.clear()
            await email_input.type(settings.EPIC_EMAIL)

            # 2. 点击继续按钮
            await self.page.click("#continue")

            # 3. 输入密码
            password_input = self.page.locator("#password")
            await password_input.clear()
            await password_input.type(settings.EPIC_PASSWORD.get_secret_value())

            # 4. 点击登录按钮
            await self.page.click("#sign-in")

            # 并行启动：验证码处理 + 登录结果等待
            # 关键改进：使用 wait_for 快速检测密码错误
            async def wait_for_login_result():
                """等待登录结果（成功或失败）"""
                return await self._is_login_success_signal.get()

            async def handle_captcha():
                """处理验证码（如果需要）"""
                nonlocal captcha_success
                try:
                    assert agent is not None
                    if agent._captcha_payload_queue.empty():
                        await agent.robotic_arm.refresh_challenge()
                    await agent.wait_for_challenge()
                    captcha_success = True
                except Exception as e:
                    logger.warning(f"验证码处理异常: {e}")
                    pass  # 验证码处理失败不影响登录结果判断

            # 先只等待登录 API；大多数账号不需要启动验证码 Agent。
            result_task = asyncio.create_task(wait_for_login_result())

            # 第一阶段：15秒内快速检测密码错误
            try:
                done, pending = await asyncio.wait(
                    [result_task],
                    timeout=15,
                    return_when=asyncio.FIRST_COMPLETED
                )

                if result_task in done:
                    result = result_task.result()
                    # 检查是否是登录失败信号
                    if result.get("error"):
                        error_code = result.get("code", "")
                        if "invalid_account_credentials" in error_code:
                            logger.error("❌ 账号或密码错误")
                            return (False, ErrorType.INVALID_CREDENTIALS)
                        elif "account_locked" in error_code:
                            logger.error("❌ 账号已被锁定")
                            return (False, ErrorType.ACCOUNT_LOCKED)
                        else:
                            logger.error(f"❌ 登录失败: {error_code}")
                            return (False, ErrorType.UNKNOWN)

                    # 登录成功（无验证码或已通过）
                    if result.get("accountId"):
                        logger.success("✅ 登录成功")
                        await asyncio.wait_for(self._handle_right_account_validation(), timeout=60)
                        logger.success("✅ 账号验证成功")
                        return (True, ErrorType.SUCCESS)
            except asyncio.CancelledError:
                pass

            # 第二阶段：首轮未登录才启动验证码处理，并继续等待结果。
            agent = AgentV(page=self.page, agent_config=settings)
            captcha_task = asyncio.create_task(handle_captcha())
            try:
                result = await asyncio.wait_for(result_task, timeout=60)

                if result.get("error"):
                    error_code = result.get("code", "")
                    if "invalid_account_credentials" in error_code:
                        logger.error("❌ 账号或密码错误")
                        return (False, ErrorType.INVALID_CREDENTIALS)
                    elif "account_locked" in error_code:
                        logger.error("❌ 账号已被锁定")
                        return (False, ErrorType.ACCOUNT_LOCKED)
                    else:
                        logger.error(f"❌ 登录失败: {error_code}")
                        return (False, ErrorType.UNKNOWN)

                logger.success("✅ 登录成功")
                await asyncio.wait_for(self._handle_right_account_validation(), timeout=60)
                logger.success("✅ 账号验证成功")
                return (True, ErrorType.SUCCESS)

            except asyncio.TimeoutError:
                # 判断是验证码问题还是网络问题
                if not captcha_success:
                    logger.error("❌ 验证码识别超时")
                    return (False, ErrorType.CAPTCHA_FAILED)
                logger.error("❌ 登录超时")
                return (False, ErrorType.LOGIN_TIMEOUT)

        except asyncio.TimeoutError:
            logger.error("❌ 登录超时，请检查账号密码")
            return (False, ErrorType.LOGIN_TIMEOUT)
        except Exception as err:
            logger.warning(f"登录异常: {err}")
            return (False, ErrorType.UNKNOWN)
        finally:
            # 登录阶段的 AgentV 监听器不能泄漏到商品页，否则会继续处理
            # 隐藏的 hCaptcha 响应并阻塞后续点击。
            if captcha_task is not None:
                captcha_task.cancel()
                with suppress(asyncio.CancelledError):
                    await captcha_task
            if result_task is not None and not result_task.done():
                result_task.cancel()
                with suppress(asyncio.CancelledError):
                    await result_task
            if agent is not None:
                with suppress(Exception):
                    self.page.remove_listener("response", agent._task_handler)

    async def _handle_eula_correction(self) -> tuple[bool, ErrorType]:
        """
        处理 EULA 修正页面

        Epic Games 在某些情况下会将用户重定向到 EULA 修正页面：
        - 新注册账号首次登录
        - Epic 更新服务条款
        - 账号长期未登录
        - 账号在新设备/地区登录

        页面特征（基于实际 HTML）：
        - URL 包含 "correction/eula" 或 "corrective="
        - 接受按钮: <button id="accept" type="submit" aria-label="接受">接受</button>
        - 拒绝按钮: <button id="decline" type="button" aria-label="拒绝">拒绝</button>
        - 使用 Material UI 组件 (MuiButton-containedPrimary)

        Returns:
            tuple[bool, ErrorType]: (是否成功, 错误类型)
            - (True, SUCCESS): 成功接受 EULA
            - (False, EULA_FAILED): 处理失败，需要用户手动操作
            - (False, SUCCESS): 无需处理（不在 EULA 页面）
        """
        current_url = self.page.url

        # 检测是否在 EULA 修正页面
        if "correction/eula" not in current_url and "corrective=" not in current_url:
            return (False, ErrorType.SUCCESS)  # 无需处理

        logger.warning("⚠️ 检测到 EULA 修正页面，尝试自动接受协议...")
        logger.info(f"📋 当前 URL: {current_url}")

        try:
            # ============================================================
            # SPA 页面需要等待网络完全空闲
            # Material UI 对话框需要额外时间渲染和动画完成
            # ============================================================
            logger.debug("⏳ 等待 EULA 页面加载完成...")
            await self.page.wait_for_load_state("networkidle")

            # 等待 React/Material UI 渲染完成（对话框动画约 225ms）
            await self.page.wait_for_timeout(2000)

            # 等待对话框元素出现（确认页面已渲染）
            try:
                await self.page.wait_for_selector("#accept", timeout=10000)
                logger.debug("✅ EULA 接受按钮已渲染")
            except Exception as e:
                logger.warning(f"⚠️ 等待按钮超时: {e}")

            # ============================================================
            # EULA 接受按钮选择器（按优先级排序）
            # 基于实际 HTML 结构: <button id="accept" type="submit" aria-label="接受">
            # ============================================================
            accept_selectors = [
                # === 最精确：通过 ID 选择（最稳定）===
                "#accept",
                "button#accept",

                # === 通过 aria-label 属性（多语言支持）===
                "//button[@aria-label='接受']",
                "//button[@aria-label='Accept']",

                # === 通过 type=submit（次优）===
                "//button[@type='submit']",

                # === 通过文本匹配（多语言）===
                "//button[normalize-space(text())='接受']",
                "//button[normalize-space(text())='Accept']",

                # === 通过 Material UI class（备用）===
                "//button[contains(@class, 'MuiButton-containedPrimary')]",
            ]

            # 尝试点击接受按钮
            for i, selector in enumerate(accept_selectors, 1):
                try:
                    logger.debug(f"🔍 尝试 EULA 选择器 [{i}/{len(accept_selectors)}]: {selector}")

                    btn = self.page.locator(selector).first

                    # 检查按钮是否存在且可见
                    if not await btn.is_visible(timeout=3000):
                        logger.debug(f"按钮不可见: {selector}")
                        continue

                    btn_text = await btn.text_content()
                    logger.info(f"📋 找到 EULA 接受按钮: '{btn_text}' | 选择器: {selector}")

                    # ============================================================
                    # 🔥 关键修复：使用多种点击方式确保成功
                    # 某些情况下 Playwright 的普通点击会被拦截
                    # ============================================================

                    # 方式1：滚动到按钮位置，确保可见
                    await btn.scroll_into_view_if_needed()
                    await self.page.wait_for_timeout(500)

                    # 方式2：使用 force=True 绕过可操作性检查
                    try:
                        await btn.click(force=True, timeout=5000)
                        logger.info("👆 已点击接受按钮 (force=True)")
                    except Exception as click_err:
                        logger.warning(f"普通点击失败，尝试 JS 点击: {click_err}")
                        # 方式3：使用 JavaScript 直接点击
                        await btn.evaluate("el => el.click()")
                        logger.info("👆 已点击接受按钮 (JS evaluate)")

                    # 等待页面跳转（增加超时时间到 30 秒）
                    logger.info("⏳ 等待页面跳转...")
                    await self.page.wait_for_load_state("networkidle", timeout=30000)

                    # 额外等待，确保重定向完成
                    await self.page.wait_for_timeout(2000)

                    # 验证是否成功跳转
                    new_url = self.page.url
                    logger.debug(f"📋 点击后 URL: {new_url}")

                    if "correction/eula" not in new_url and "corrective=" not in new_url:
                        logger.success("✅ EULA 协议已接受，页面已跳转")
                        return (True, ErrorType.SUCCESS)
                    else:
                        logger.warning("⚠️ 点击后仍在 EULA 页面，尝试下一个选择器")

                except Exception as e:
                    logger.debug(f"EULA 选择器 '{selector}' 失败: {e}")
                    continue

            # ============================================================
            # 所有选择器都失败，记录详细的页面信息便于调试
            # ============================================================
            logger.error("❌ 未能找到 EULA 接受按钮")
            try:
                # 截图保存，便于分析
                screenshot_path = f"/tmp/eula_error_{int(time.time())}.png"
                await self.page.screenshot(path=screenshot_path)
                logger.info(f"📸 EULA 页面截图已保存: {screenshot_path}")

                # 打印页面 HTML，便于调试
                page_content = await self.page.content()
                logger.debug(f"📄 EULA 页面 HTML (前 2000 字符):\n{page_content[:2000]}")
            except Exception as e:
                logger.warning(f"保存调试信息失败: {e}")

            return (False, ErrorType.EULA_FAILED)

        except Exception as e:
            logger.error(f"❌ 处理 EULA 页面异常: {e}")
            return (False, ErrorType.EULA_FAILED)

    async def invoke(self) -> ErrorType:
        """
        执行 Epic 登录认证流程

        流程：
        1. 访问 Epic 免费游戏页面
        2. 检测并处理 EULA 修正页面
        3. 检查登录状态
        4. 如果未登录，执行登录流程
        5. 处理登录后的验证

        Returns:
            ErrorType: 错误类型
            - SUCCESS: 登录成功或已登录
            - 其他错误类型: 对应的失败原因
        """
        self.page.on("response", self._on_response_anything)

        for attempt in range(3):
            logger.info(f"🔄 登录尝试 [{attempt + 1}/3]")

            try:
                await self.page.goto(URL_CLAIM, wait_until="domcontentloaded")
            except Exception as e:
                logger.warning(f"页面加载失败: {e}")
                if "timeout" in str(e).lower():
                    return ErrorType.NETWORK_TIMEOUT
                continue

            # ============================================================
            # 🔥 关键修复：等待页面稳定
            # Epic Games 页面是 SPA，JS 需要时间执行
            # domcontentloaded 触发时重定向可能还没完成
            # ============================================================
            await self.page.wait_for_timeout(3000)  # 等待 3 秒让 JS 执行完成

            # ============================================================
            # 🔥 EULA 修正页面检测与处理
            # 登录后可能被重定向到 EULA 页面，需要自动接受协议
            # ============================================================
            for eula_attempt in range(3):  # 最多处理 3 次 EULA（通常只需要 1 次）
                current_url = self.page.url
                logger.debug(f"📍 当前页面 URL: {current_url}")
                if "correction/eula" in current_url or "corrective=" in current_url:
                    logger.warning(f"⚠️ 检测到修正页面 (EULA 尝试 {eula_attempt + 1}/3): {current_url}")

                    success, error_type = await self._handle_eula_correction()

                    if success:
                        # EULA 处理成功后，重新导航到目标页面
                        await self.page.goto(URL_CLAIM, wait_until="domcontentloaded")
                        await self.page.wait_for_timeout(2000)  # 再次等待稳定
                    else:
                        logger.error(f"❌ EULA 处理失败: {error_type.value}")
                        return error_type  # 返回具体错误类型
                else:
                    break

            # 检查登录状态（增加超时处理）
            try:
                status = await self.page.locator("//egs-navigation").get_attribute("isloggedin", timeout=15000)
            except Exception as e:
                # 超时时检查是否在修正页面
                current_url = self.page.url
                logger.debug(f"📍 获取登录状态超时，当前 URL: {current_url}")
                if "correction" in current_url or "eula" in current_url:
                    logger.error("❌ 仍在修正页面，无法继续")
                    return ErrorType.EULA_FAILED
                logger.error(f"❌ 获取登录状态超时: {e}")

                # 判断是网络问题还是其他问题
                if "timeout" in str(e).lower():
                    return ErrorType.NETWORK_TIMEOUT
                return ErrorType.UNKNOWN

            if status == "true":
                logger.success("✅ Epic Games 已登录")
                return ErrorType.SUCCESS

            # 执行登录
            login_result = await self._login()
            if login_result:
                success, error_type = login_result
                if success:
                    return ErrorType.SUCCESS
                # 登录失败，返回具体错误类型
                return error_type

            # login_result 为 None 时继续下一次尝试
            logger.warning("⚠️ 登录结果为空，尝试下一次...")
            continue

        # 所有尝试都失败
        logger.error("❌ 所有登录尝试都失败")
        return ErrorType.UNKNOWN
