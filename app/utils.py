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
- 保留 7 天
"""
from __future__ import annotations
import os
import sys
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from loguru import logger

def timezone_filter(record):
    """时区转换过滤器"""
    record["time"] = record["time"].astimezone(ZoneInfo("Asia/Shanghai"))
    return True

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

    # 控制台：使用过滤器，只显示关键日志
    logger.add(
        sink=sys.stdout,
        level="INFO",
        filter=console_filter,
        format="<green>{time:MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
    )

    # 错误日志文件：按日期存储，格式 error-2026-03-22.log
    if sink_channel.get("error"):
        error_path = Path(sink_channel.get("error"))
        log_dir = error_path.parent
        log_dir.mkdir(parents=True, exist_ok=True)

        # 使用日期作为文件名后缀
        date_str = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
        error_log_file = log_dir / f"error-{date_str}.log"

        logger.add(
            sink=str(error_log_file),
            level="ERROR",
            rotation="1 MB",
            filter=timezone_filter,
            retention="7 days",
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
            encoding="utf-8",
        )

    # 运行时日志文件：按日期存储，格式 runtime-2026-03-22.log
    if sink_channel.get("runtime"):
        runtime_path = Path(sink_channel.get("runtime"))
        log_dir = runtime_path.parent
        log_dir.mkdir(parents=True, exist_ok=True)

        # 使用日期作为文件名后缀
        date_str = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
        runtime_log_file = log_dir / f"runtime-{date_str}.log"

        logger.add(
            sink=str(runtime_log_file),
            level="DEBUG",
            rotation="1 MB",
            filter=timezone_filter,
            retention="7 days",
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} | {message}",
            encoding="utf-8",
        )

    return logger
