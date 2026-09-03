# -*- coding: utf-8 -*-
"""
日志配置模块

控制台日志策略：
- 只显示关键信息（启动、登录、验证码、游戏领取、错误）
- 过滤冗长的详细日志
- 中文显示

文件日志策略：
- 按日期分类存储，方便查找和清理
- 文件名格式：runtime-2026-03-22.log / error-2026-03-22.log
- 单个日志文件最大 1 MB，超过后自动轮转
- 保留 30 天并压缩
"""
from __future__ import annotations
from contextlib import suppress
import os
import re
import sys
from pathlib import Path
from datetime import datetime
from time import time
from zoneinfo import ZoneInfo
from loguru import logger


LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "30"))


def cleanup_debug_artifacts(runtime_dir: Path, retention_days: int = 7) -> int:
    """删除过期的登录/结账调试文件，不触碰其他运行数据。"""
    cutoff = time() - retention_days * 86400
    removed = 0
    for name in ("login_debug", "checkout_debug"):
        root = runtime_dir / name
        if not root.is_dir() or root.is_symlink():
            continue
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
    last_html = runtime_dir / "checkout_debug_last.html"
    if (
        last_html.is_file()
        and not last_html.is_symlink()
        and last_html.stat().st_mtime < cutoff
    ):
        last_html.unlink()
        removed += 1
    return removed

def cleanup_old_logs(log_dir: Path, retention_days: int = 30) -> int:
    """删除过期的应用日志。

    loguru 的 retention 在这里指望不上：它靠从文件名模板推导出的 glob 去匹配同族
    文件，而本项目的日志名是按日期分文件的（runtime-YYYY-MM-DD.log），实测即便
    把日期换成 {time} 占位符，40 天前的历史文件在 sink 初始化时也不会被清掉。
    线上实际结果是 data/logs 累积了 201 个文件、75MB、0 个 .gz，
    自 2026-03-22 起从未清理过。这里按 mtime 做确定性清理。
    """
    if not log_dir.is_dir() or log_dir.is_symlink():
        return 0
    cutoff = time() - retention_days * 86400
    removed = 0
    for path in log_dir.iterdir():
        if not path.is_file() or path.is_symlink():
            continue
        if not (path.name.startswith("runtime-") or path.name.startswith("error-")):
            continue
        if path.stat().st_mtime < cutoff:
            with suppress(OSError):
                path.unlink()
                removed += 1
    return removed


def redact_record(record):
    message = str(record["message"])
    for value in (os.getenv("EPIC_EMAIL", ""), os.getenv("EPIC_PASSWORD", "")):
        if value:
            message = message.replace(value, "<redacted>")
    message = re.sub(
        r"(?i)(authorization|api[_-]?key|cookie|token)(\s*[:=]\s*)([^\s,;]+)",
        r"\1\2<redacted>",
        message,
    )
    message = re.sub(r"\b[A-Za-z0-9_-]{80,}\b", "<token>", message)
    record["message"] = message
    return True


def timezone_filter(record):
    """时区转换过滤器"""
    record["time"] = record["time"].astimezone(ZoneInfo("Asia/Shanghai"))
    return redact_record(record)

# 控制台只显示的关键日志关键词
CONSOLE_KEYWORDS = [
    # 启动配置
    "API 提供商",
    "验证码模型",
    "主力模型",
    "补丁加载成功",
    # 登录状态
    "已登录",
    "登录成功",
    # 验证码结果
    "验证码通过",
    "验证码超时",
    # 游戏领取
    "已在库中",
    "领取成功",
    "任务完成",
    "按钮状态",
    "发现:",
    "GAME_RESULT:",
    # 错误
    "错误",
    "失败",
    "警告",
    # 网络探针（Mechanism C 观测）：让点击 CTA 前后的 Epic 请求/响应
    # 透传到控制台（docker logs），便于肉眼复盘黑盒。
    "网络探针",
    "REQ ",
    "RES ",
]

# 控制台要过滤掉的详细日志关键词（即使级别匹配也不显示）
SUPPRESS_KEYWORDS = [
    "原始响应",
    "JSON 解析",
    "调用 OpenAI 兼容 API",
    "文件已缓存",
    "response_schema",
    "备用模型",
    "hsw script",
    "is read-only",
    "btoa",
]

def console_filter(record):
    """
    控制台过滤器：只显示关键日志

    规则：
    1. ERROR 及以上级别：始终显示
    2. SUCCESS 级别：显示关键操作结果
    3. WARNING 级别：显示重要警告
    4. INFO 级别：只显示包含关键词的日志
    5. DEBUG 级别：不显示在控制台
    """
    level = record["level"].name
    redact_record(record)
    message = record["message"]

    # DEBUG 级别不显示在控制台
    if level == "DEBUG":
        return False

    # ERROR 及以上始终显示
    if level in ("ERROR", "CRITICAL"):
        return True

    # 检查是否在抑制列表中
    for keyword in SUPPRESS_KEYWORDS:
        if keyword in message:
            return False

    # SUCCESS 级别显示关键操作
    if level == "SUCCESS":
        return True

    # WARNING 级别过滤掉次要警告
    if level == "WARNING":
        # 过滤掉重试警告（太多）
        if "try to retry" in message or "retry the strategy" in message:
            return False
        return True

    # INFO 级别：只显示包含关键词的日志
    for keyword in CONSOLE_KEYWORDS:
        if keyword in message:
            return True

    return False

def init_log(**sink_channel):
    """
    初始化日志系统

    控制台：精简输出，只显示关键信息
    文件：按日期分类存储，保留 7 天
    """
    logger.remove()

    # 启动时先按 mtime 清一次历史日志（loguru 自己的 retention 对本项目
    # 的按日期分文件命名不生效，见 cleanup_old_logs 的说明）。
    for _path in sink_channel.values():
        if _path:
            with suppress(Exception):
                cleanup_old_logs(Path(_path).parent, LOG_RETENTION_DAYS)
            break

    # 控制台：使用过滤器，只显示关键日志
    logger.add(
        sink=sys.stdout,
        level="INFO",
        filter=console_filter,
        format="<green>{time:MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
    )

    # 错误日志文件：按日期存储，格式 error-2026-03-22.log
    if sink_channel.get("error"):
        try:
            error_path = Path(sink_channel.get("error"))
            log_dir = error_path.parent
            log_dir.mkdir(parents=True, exist_ok=True)

            # 日期必须用 loguru 的 {time} 占位符，不能在应用层写死。
            error_log_file = log_dir / "error-{time:YYYY-MM-DD}.log"

            logger.add(
                sink=str(error_log_file),
                level="ERROR",
                rotation="1 MB",
                filter=timezone_filter,
                retention="30 days",
                compression="gz",
                format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
                encoding="utf-8",
            )
        except (OSError, PermissionError):
            pass

    # 运行时日志文件：按日期存储，格式 runtime-2026-03-22.log
    if sink_channel.get("runtime"):
        try:
            runtime_path = Path(sink_channel.get("runtime"))
            log_dir = runtime_path.parent
            log_dir.mkdir(parents=True, exist_ok=True)

            # 日期必须用 loguru 的 {time} 占位符，不能在应用层写死。
            runtime_log_file = log_dir / "runtime-{time:YYYY-MM-DD}.log"

            logger.add(
                sink=str(runtime_log_file),
                level="INFO",
                rotation="1 MB",
                filter=timezone_filter,
                retention="30 days",
                compression="gz",
                format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
                encoding="utf-8",
            )
        except (OSError, PermissionError):
            pass

    return logger
