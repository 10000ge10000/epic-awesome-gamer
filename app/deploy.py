# -*- coding: utf-8 -*-
"""
Epic Games Free Game Collection Deployment Module

This module orchestrates the automated collection of free games from Epic Games Store
using browser automation and scheduling capabilities.

@Time    : 2025/7/16 21:28
@Author  : QIN2DIM
@GitHub  : https://github.com/QIN2DIM
"""

import asyncio
import json
import os
import signal
import sys
from contextlib import suppress
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from browserforge.fingerprints import Screen
from camoufox import AsyncCamoufox
from loguru import logger
from playwright.async_api import ViewportSize
from pytz import timezone

from services.epic_authorization_service import EpicAuthorization, ErrorType
from services.epic_games_service import EpicAgent, GameCollectResult
from services.epic_games_service import _is_driver_disconnect_error
from settings import LOG_DIR, RECORD_DIR, RUNTIME_DIR
from settings import settings
from utils import cleanup_debug_artifacts, init_log

# Initialize logging configuration
init_log(
    runtime=LOG_DIR.joinpath("runtime.log"),
    error=LOG_DIR.joinpath("error.log"),
)
with suppress(Exception):
    removed_debug_files = cleanup_debug_artifacts(RUNTIME_DIR, retention_days=7)
    if removed_debug_files:
        logger.info(f"Expired debug artifacts removed count={removed_debug_files}")

# Default timezone for scheduling operations
TIMEZONE = timezone("Asia/Shanghai")


async def execute_browser_tasks(headless: bool = True) -> ErrorType:
    """
    Execute Epic Games free game collection tasks using browser automation.

    This function handles the complete workflow of authenticating with Epic Games
    and collecting available free games through browser automation.

    Args:
        headless: Whether to run browser in headless mode

    Returns:
        ErrorType: 错误类型，用于指示执行结果
    """
    logger.debug("Starting Epic Games collection task")

    # ============================================================
    # 🌐 代理配置：从环境变量读取（支持 WARP 等 HTTP 代理）
    # 格式: HTTP_PROXY=http://host:port
    # ============================================================
    proxy_config = None
    http_proxy = os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY")
    if http_proxy:
        from urllib.parse import urlparse
        parsed = urlparse(http_proxy)
        proxy_config = {
            "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
        }
        if parsed.username:
            proxy_config["username"] = parsed.username
        if parsed.password:
            proxy_config["password"] = parsed.password
        logger.info(f"🌐 使用代理: {parsed.hostname}:{parsed.port}")

    try:
        # Configure browser with anti-detection features
        async with AsyncCamoufox(
            persistent_context=True,
            user_data_dir=settings.user_data_dir,
            screen=Screen(max_width=1920, max_height=1080, min_height=1080, min_width=1920),
            humanize=0.2,
            headless=headless,
            proxy=proxy_config,
        ) as browser:
            # Initialize or reuse existing browser page
            page = browser.pages[0] if browser.pages else await browser.new_page()
            logger.debug("Browser initialized successfully")

            # Handle Epic Games authentication
            logger.debug("Initiating Epic Games authentication")
            auth_agent = EpicAuthorization(page)
            auth_result = await auth_agent.invoke()
            logger.debug(f"Authentication result: {auth_result.value if auth_result else 'None'}")

            # ============================================================
            # 🔥 错误类型处理
            # 根据不同的错误类型输出特定格式的日志，便于 worker.py 解析
            # ============================================================
            if auth_result != ErrorType.SUCCESS:
                # 输出特定格式的错误日志，便于 worker.py 解析
                # 格式: ❌ ERROR_TYPE:xxx 其中 xxx 是 ErrorType 的 value
                auth_error = auth_result or ErrorType.UNKNOWN
                logger.error(f"❌ ERROR_TYPE:{auth_error.value}")
                return auth_error

            logger.debug("Authentication completed successfully")

            if os.getenv("EPIC_VERIFY_ONLY", "").lower() in {"1", "true", "yes", "on"}:
                # 托管验证只确认 Epic 登录，不应顺带领取当前周免游戏。
                logger.success("✅ 登录成功，验证模式跳过领取")
                return ErrorType.SUCCESS

            # 登录 Agent 会注册 hCaptcha response 监听器。使用同一浏览器
            # context 的干净页面继续领取，Cookie 保持共享，同时彻底终止
            # 登录页上仍在运行的 HSW 回调，避免其阻塞商品按钮点击。
            claim_page = await page.context.new_page()
            await page.close()
            page = claim_page

            logger.debug("Starting free games collection process")
            agent = EpicAgent(page)
            game_result = await agent.collect_epic_games()

            # ============================================================
            # 🔥 游戏收集结果处理
            # 根据不同的结果类型输出特定格式的日志
            # ============================================================
            if game_result == GameCollectResult.ALL_OWNED:
                logger.success("✅ 所有周免游戏已在库中")
            elif game_result == GameCollectResult.SUCCESS:
                logger.success("🎉 游戏领取成功！")
            else:
                # 失败情况：输出错误类型供 worker.py 解析
                logger.error(f"❌ GAME_ERROR:{game_result.value}")

            # Cleanup browser resources
            logger.debug("Cleaning up browser resources")
            with suppress(Exception):
                for p in browser.pages:
                    await p.close()

            with suppress(Exception):
                await browser.close()

            logger.debug("Browser tasks execution finished successfully")
            return ErrorType.SUCCESS
    except Exception as exc:
        logger.exception(exc)
        if _is_driver_disconnect_error(exc):
            logger.error("❌ FINAL_ERROR:network_timeout")
            return ErrorType.NETWORK_TIMEOUT
        return ErrorType.UNKNOWN


async def deploy():
    """
    Main deployment function that executes Epic Games collection tasks.

    This function runs the collection process immediately and optionally
    sets up a scheduled task for automatic recurring execution.
    """
    headless = True

    # Log current configuration for debugging
    sj = settings.model_dump(mode="json")
    sj["headless"] = headless
    logger.debug(
        f"Starting deployment with configuration: {json.dumps(sj, indent=2, ensure_ascii=False)}"
    )

    # Execute an immediate collection task
    result = await execute_browser_tasks(headless=headless)
    if result is None:
        logger.error("❌ 浏览器任务未返回明确结果，按未知错误处理")
        result = ErrorType.UNKNOWN

    # 如果任务失败，输出最终错误类型（便于 worker.py 解析）
    if result != ErrorType.SUCCESS:
        logger.error(f"❌ FINAL_ERROR:{result.value}")

    # Skip scheduler setup if disabled in configuration
    if not settings.ENABLE_APSCHEDULER:
        logger.debug("Scheduler is disabled, deployment completed")
        return

    # Initialize and configure async scheduler
    scheduler = AsyncIOScheduler()

    # Strategy 1: Thursday 23:30 to Friday 03:30, every hour (Beijing Time)
    scheduler.add_job(
        execute_browser_tasks,
        trigger=CronTrigger(
            day_of_week="thu", hour="23,0,1,2,3", minute="30", timezone="Asia/Shanghai"
        ),
        id="weekly_epic_games_task",
        name="weekly_epic_games_task",
        args=[headless],
        replace_existing=False,
        max_instances=1,
    )

    # Strategy 2: Daily at 12:00 PM (Beijing Time)
    scheduler.add_job(
        execute_browser_tasks,
        trigger=CronTrigger(hour="12", minute="0", timezone="Asia/Shanghai"),
        id="daily_epic_games_task",
        name="daily_epic_games_task",
        args=[headless],
        replace_existing=False,
        max_instances=1,
    )

    # Set up graceful shutdown signal handlers
    shutdown_event = asyncio.Event()

    def signal_handler(signum, frame):
        logger.debug(f"Received signal {signal.Signals(signum).name}, initiating graceful shutdown")
        shutdown_event.set()

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start scheduler and log status information
    scheduler.start()
    logger.debug("Epic Games scheduler started successfully")
    logger.debug(f"Current time: {datetime.now(TIMEZONE).strftime('%Y-%m-%d %H:%M:%S %Z')}")

    # Log next execution times for all scheduled jobs
    for j in scheduler.get_jobs():
        if next_run := j.next_run_time:
            logger.debug(
                f"Next execution scheduled: {next_run.strftime('%Y-%m-%d %H:%M:%S %Z')} (job_id: {j.id})"
            )

    # Keep scheduler running until shutdown signal received
    logger.debug("Scheduler is running, send SIGINT or SIGTERM to stop gracefully")
    try:
        await shutdown_event.wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        scheduler.shutdown(wait=True)
        logger.success("Scheduler stopped gracefully")


if __name__ == '__main__':
    asyncio.run(deploy())
