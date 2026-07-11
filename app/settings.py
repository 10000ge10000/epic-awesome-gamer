# -*- coding: utf-8 -*-
"""
Epic Kiosk 配置模块
支持 SiliconFlow / OpenAI 兼容格式 API
"""
import os
import re
import sys
import asyncio
import base64
import json
import random
import time
from pathlib import Path
from typing import Any, List, Union

# === 引入所需库 ===
from hcaptcha_challenger.agent import AgentConfig
from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict
from loguru import logger
import redis as redis_client
from provider_router import ProviderRouter, ProviderSpec, ProviderUnavailable


_CAPTCHA_SESSION_UNAVAILABLE_PROVIDERS: set[str] = set()
_CAPTCHA_LAST_CALL_PROVIDER_TIMEOUT = False
_CAPTCHA_PROVIDER_FAILURE_THRESHOLD = int(os.getenv("CAPTCHA_PROVIDER_FAILURE_THRESHOLD", "3"))
_CAPTCHA_PROVIDER_CIRCUIT_SECONDS = int(os.getenv("CAPTCHA_PROVIDER_CIRCUIT_SECONDS", "600"))


def _provider_circuit_open(provider: str) -> bool:
    try:
        client = redis_client.Redis(
            host=os.getenv("REDIS_HOST", "redis"),
            port=6379,
            decode_responses=True,
            socket_connect_timeout=0.2,
            socket_timeout=0.2,
        )
        return bool(client.exists(f"metrics:provider_circuit:{provider}"))
    except Exception:
        return False


def _filter_session_captcha_providers(providers: list[tuple]) -> list[tuple]:
    return [
        provider
        for provider in providers
        if provider[0] not in _CAPTCHA_SESSION_UNAVAILABLE_PROVIDERS
        and not _provider_circuit_open(provider[0])
    ]


def captcha_last_call_provider_timeout() -> bool:
    return _CAPTCHA_LAST_CALL_PROVIDER_TIMEOUT


def _read_secret(env_name: str, file_env_name: str) -> str:
    file_name = os.getenv(file_env_name, "").strip()
    if file_name:
        return Path(file_name).read_text(encoding="utf-8").strip()
    return os.getenv(env_name, "").strip()


def _record_provider_event(event: dict[str, Any]) -> None:
    logger.info(json.dumps(event, ensure_ascii=True, separators=(",", ":")))
    provider = str(event.get("provider", "unknown"))
    outcome = str(event.get("outcome", "unknown"))
    if outcome in {"network_error", "http_error", "invalid_json"}:
        _CAPTCHA_SESSION_UNAVAILABLE_PROVIDERS.add(provider)
    elif outcome == "success":
        _CAPTCHA_SESSION_UNAVAILABLE_PROVIDERS.discard(provider)
    try:
        client = redis_client.Redis(
            host=os.getenv("REDIS_HOST", "redis"),
            port=6379,
            decode_responses=True,
            socket_connect_timeout=0.2,
            socket_timeout=0.2,
        )
        key = f"metrics:provider:{provider}"
        mapping = {
            "last_outcome": str(event.get("outcome", "unknown")),
            "last_elapsed": str(event.get("elapsed", "")),
            "last_request_id": str(event.get("request_id", "")),
            "updated_at": str(int(time.time())),
        }
        client.hset(key, mapping=mapping)
        client.hincrby(key, "attempts", 1)
        if outcome == "success":
            client.hset(key, "consecutive_failures", 0)
            client.delete(f"metrics:provider_circuit:{provider}")
        elif outcome in {"network_error", "http_error", "invalid_json"}:
            failures = client.hincrby(key, "consecutive_failures", 1)
            if failures >= _CAPTCHA_PROVIDER_FAILURE_THRESHOLD:
                client.setex(
                    f"metrics:provider_circuit:{provider}",
                    _CAPTCHA_PROVIDER_CIRCUIT_SECONDS,
                    "1",
                )
        if outcome == "circuit_opened":
            client.setex(
                f"metrics:provider_circuit:{provider}",
                int(event.get("ttl_seconds", _CAPTCHA_PROVIDER_CIRCUIT_SECONDS)),
                "1",
            )
    except Exception:
        pass

# --- 核心路径定义 ---
PROJECT_ROOT = Path(__file__).parent
VOLUMES_DIR = PROJECT_ROOT.joinpath("volumes")
LOG_DIR = VOLUMES_DIR.joinpath("logs")
USER_DATA_DIR = VOLUMES_DIR.joinpath("user_data")
RUNTIME_DIR = VOLUMES_DIR.joinpath("runtime")
RECORD_DIR = VOLUMES_DIR.joinpath("record")

# ==========================================
# API 提供商配置
# ==========================================
# 默认使用 SiliconFlow；保留 API_PROVIDER 仅用于日志和部署标识。
API_PROVIDER = os.getenv("API_PROVIDER", "siliconflow")

# === 配置类定义 ===
class EpicSettings(AgentConfig):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    # [基础配置] OpenAI 兼容 API Key
    # 新部署使用 API_KEY；兼容旧版 SILICONFLOW_API_KEY，避免已有 .env 直接失效。
    API_KEY: SecretStr | None = Field(
        default_factory=lambda: _read_secret("API_KEY", "API_KEY_FILE")
        or os.getenv("SILICONFLOW_API_KEY"),
        description="OpenAI-compatible API Key",
    )

    # 覆盖父类的 GEMINI_API_KEY，使其变为可选（本项目通过兼容层调用模型）
    GEMINI_API_KEY: SecretStr | None = Field(
        default_factory=lambda: os.getenv("GEMINI_API_KEY", "not_used"),
        description="Gemini API Key（本项目无需配置）",
    )

    # API 基础地址；新部署使用 API_BASE_URL；兼容旧版 SILICONFLOW_BASE_URL。
    API_BASE_URL: str = Field(
        default_factory=lambda: os.getenv("API_BASE_URL") or os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"),
        description="OpenAI-compatible API base URL",
    )

    # === 全局统一模型配置 ===
    # 兼容旧配置（GEMINI_MODEL 作为默认）
    GEMINI_MODEL: str = Field(
        default=os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
        description="默认模型名称",
    )

    # === 验证码模型（需要视觉能力）===
    CAPTCHA_MODEL: str = Field(
        default=os.getenv(
            "CAPTCHA_PRIMARY_MODEL",
            os.getenv("CAPTCHA_MODEL", "meta/llama-4-maverick-17b-128e-instruct"),
        ),
        description="验证码识别模型（主力）",
    )
    CAPTCHA_MODEL_FALLBACK: str = Field(
        default=os.getenv(
            "CAPTCHA_SECONDARY_MODEL",
            os.getenv("CAPTCHA_MODEL_FALLBACK", "Qwen/Qwen3-VL-32B-Instruct"),
        ),
        description="验证码识别模型（备用）",
    )

    CAPTCHA_PRIMARY_BASE_URL: str = Field(
        default_factory=lambda: os.getenv(
            "CAPTCHA_PRIMARY_BASE_URL", "https://integrate.api.nvidia.com/v1"
        )
    )
    CAPTCHA_PRIMARY_API_KEY: SecretStr = Field(
        default_factory=lambda: SecretStr(
            _read_secret("CAPTCHA_NVIDIA_API_KEY", "CAPTCHA_NVIDIA_API_KEY_FILE")
            or _read_secret("API_KEY", "API_KEY_FILE")
        )
    )
    CAPTCHA_PRIMARY_MODEL: str = Field(
        default_factory=lambda: os.getenv(
            "CAPTCHA_PRIMARY_MODEL", "meta/llama-4-maverick-17b-128e-instruct"
        )
    )
    CAPTCHA_SECONDARY_BASE_URL: str = Field(
        default_factory=lambda: os.getenv(
            "CAPTCHA_SECONDARY_BASE_URL", "https://api.siliconflow.cn/v1"
        )
    )
    CAPTCHA_SECONDARY_API_KEY: SecretStr = Field(
        default_factory=lambda: SecretStr(
            _read_secret("CAPTCHA_SILICONFLOW_API_KEY", "CAPTCHA_SILICONFLOW_API_KEY_FILE")
        )
    )
    CAPTCHA_SECONDARY_MODEL: str = Field(
        default_factory=lambda: os.getenv(
            "CAPTCHA_SECONDARY_MODEL", "Qwen/Qwen3-VL-32B-Instruct"
        )
    )

    # === 主力模型（一般文本任务）===
    PRIMARY_MODEL: str = Field(
        default=os.getenv("PRIMARY_MODEL", "deepseek-ai/DeepSeek-V4-Flash"),
        description="主力文本模型",
    )
    PRIMARY_MODEL_FALLBACK: str = Field(
        default=os.getenv("PRIMARY_MODEL_FALLBACK", "deepseek-ai/DeepSeek-V4-Pro"),
        description="主力文本模型（备用）",
    )

    # === hcaptcha-challenger 内置模型配置（必须覆盖默认值）===
    # 这些属性会覆盖 AgentConfig 的默认 gemini 模型名称
    CHALLENGE_CLASSIFIER_MODEL: str = Field(
        default=os.getenv("CAPTCHA_PRIMARY_MODEL", os.getenv("CAPTCHA_MODEL", "meta/llama-4-maverick-17b-128e-instruct")),
        description="挑战分类模型",
    )
    IMAGE_CLASSIFIER_MODEL: str = Field(
        default=os.getenv("CAPTCHA_PRIMARY_MODEL", os.getenv("CAPTCHA_MODEL", "meta/llama-4-maverick-17b-128e-instruct")),
        description="图像分类模型 (image_label_binary)",
    )
    SPATIAL_POINT_REASONER_MODEL: str = Field(
        default=os.getenv("CAPTCHA_PRIMARY_MODEL", os.getenv("CAPTCHA_MODEL", "meta/llama-4-maverick-17b-128e-instruct")),
        description="空间点推理模型 (image_label_area_select)",
    )
    SPATIAL_PATH_REASONER_MODEL: str = Field(
        default=os.getenv("CAPTCHA_PRIMARY_MODEL", os.getenv("CAPTCHA_MODEL", "meta/llama-4-maverick-17b-128e-instruct")),
        description="空间路径推理模型 (image_drag_drop)",
    )

    EPIC_EMAIL: str = Field(default_factory=lambda: os.getenv("EPIC_EMAIL", ""))
    EPIC_PROFILE_ID: str = Field(default_factory=lambda: os.getenv("EPIC_PROFILE_ID", ""))
    EPIC_PASSWORD: SecretStr = Field(
        default_factory=lambda: SecretStr(os.getenv("EPIC_PASSWORD", ""))
    )
    DISABLE_BEZIER_TRAJECTORY: bool = Field(default=True)

    # === hcaptcha-challenger 超时配置 ===
    # 单次验证码处理总超时（秒）
    EXECUTION_TIMEOUT: float = Field(
        default=float(os.getenv("HCAPTCHA_EXECUTION_TIMEOUT", "180")),
        description="验证码处理总超时时间（秒）",
    )

    # 验证码响应超时（秒）
    RESPONSE_TIMEOUT: float = Field(
        default=float(os.getenv("HCAPTCHA_RESPONSE_TIMEOUT", "90")),
        description="验证码响应超时时间（秒）"
    )

    # Epic 结账页的 hCaptcha iframe 渲染有抖动，默认 1.5s 偏短。
    WAIT_FOR_CHALLENGE_VIEW_TO_RENDER_MS: int = Field(
        default=int(os.getenv("HCAPTCHA_RENDER_WAIT_MS", "3000")),
        description="等待验证码视图渲染的时间（毫秒）",
    )

    ignore_request_questions: list[str] = Field(
        default_factory=lambda: [
            item.strip()
            for item in os.getenv(
                "HCAPTCHA_IGNORE_QUESTIONS",
                "",
            ).split("||")
            if item.strip()
        ],
        description="hCaptcha questions to refresh instead of solving",
    )
    RETRY_ON_FAILURE: bool = Field(default=True)
    enable_challenger_debug: bool = Field(
        default=os.getenv("HCAPTCHA_DEBUG", "true").lower() in {"1", "true", "yes", "on"}
    )

    CAPTCHA_PROVIDER: str = Field(
        default=os.getenv("CAPTCHA_PROVIDER", "none").lower(),
        description="验证码服务商 fallback：none / 2captcha",
    )
    CAPTCHA_PROVIDER_API_KEY: SecretStr = Field(
        default_factory=lambda: SecretStr(os.getenv("CAPTCHA_PROVIDER_API_KEY", "")),
        description="验证码服务商 API Key",
    )
    CAPTCHA_PROVIDER_SITE_KEY: str = Field(
        default=os.getenv("CAPTCHA_PROVIDER_SITE_KEY", "91e4137f-95af-4bc9-97af-cdcedce21c8c"),
        description="Epic 登录页 hCaptcha sitekey",
    )
    CAPTCHA_PROVIDER_TIMEOUT: int = Field(
        default=int(os.getenv("CAPTCHA_PROVIDER_TIMEOUT", "180")),
        description="验证码服务商等待超时（秒）",
    )
    CAPTCHA_PROVIDER_POLL_INTERVAL: int = Field(
        default=int(os.getenv("CAPTCHA_PROVIDER_POLL_INTERVAL", "5")),
        description="验证码服务商轮询间隔（秒）",
    )

    # 禁用 hcaptcha 文件保存（使用 /tmp 临时目录）
    cache_dir: Path = Path("/tmp/hcaptcha/.cache")
    challenge_dir: Path = Path("/tmp/hcaptcha/.challenge")
    captcha_response_dir: Path = Path("/tmp/hcaptcha/.captcha")

    ENABLE_APSCHEDULER: bool = Field(default=True)
    TASK_TIMEOUT_SECONDS: int = Field(default=900)
    REDIS_URL: str = Field(default="redis://redis:6379/0")
    CELERY_WORKER_CONCURRENCY: int = Field(default=1)
    CELERY_TASK_TIME_LIMIT: int = Field(default=1200)
    CELERY_TASK_SOFT_TIME_LIMIT: int = Field(default=900)

    @property
    def user_data_dir(self) -> Path:
        profile_id = self.EPIC_PROFILE_ID.strip()
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", profile_id):
            raise ValueError("EPIC_PROFILE_ID is invalid")
        target_ = USER_DATA_DIR.joinpath(profile_id)
        target_.mkdir(parents=True, exist_ok=True)
        return target_

settings = EpicSettings()

# 记录当前配置
logger.info(f"🎯 API 提供商: {API_PROVIDER}")
logger.info(f"🔐 验证码模型: {settings.CAPTCHA_MODEL} (备用: {settings.CAPTCHA_MODEL_FALLBACK})")
logger.info(f"🤖 主力模型: {settings.PRIMARY_MODEL} (备用: {settings.PRIMARY_MODEL_FALLBACK})")

# ==========================================
# OpenAI 兼容 API 补丁
# 注意：部分视觉模型不支持 response_format: json_object
# 解决方案：从响应中提取 JSON 代码块
# ==========================================
def _apply_openai_compatible_patch():
    """
    OpenAI 兼容 API 调用层。

    默认使用 SiliconFlow：
    - Base URL: https://api.siliconflow.cn/v1
    - API Key 获取地址: https://cloud.siliconflow.cn/i/OVI2n57p
    - 视觉和文本模型均通过 /v1/chat/completions 调用
    """
    if not settings.API_KEY:
        logger.warning("⚠️ 未配置 API_KEY，请从 https://cloud.siliconflow.cn/i/OVI2n57p 获取 SiliconFlow API Key")
        return

    try:
        from google import genai
        from google.genai import types
        import httpx

        # 获取 API Key
        if hasattr(settings.API_KEY, 'get_secret_value'):
            api_key = settings.API_KEY.get_secret_value()
        else:
            api_key = str(settings.API_KEY)

        base_url = settings.API_BASE_URL.rstrip('/')
        if base_url.endswith('/v1'):
            base_url = base_url[:-3]

        logger.info(f"🚀 OpenAI 兼容补丁加载中... | 地址: {base_url}")

        # ==========================================
        # 辅助函数：将 Gemini contents 转换为 OpenAI messages
        # ==========================================
        def _convert_gemini_to_openai(contents: List, model: str) -> tuple:
            """
            将 Gemini 格式的 contents 转换为 OpenAI 格式的 messages
            返回: (messages, has_images)
            """
            messages = []
            has_images = False

            for content in contents:
                # 处理字符串类型（简单的文本消息）
                if isinstance(content, str):
                    if content.strip():
                        messages.append({"role": "user", "content": content})

                # 处理 Gemini Content 对象
                elif hasattr(content, 'parts'):
                    text_parts = []
                    image_parts = []

                    for part in content.parts:
                        # 处理文本
                        if hasattr(part, 'text') and part.text:
                            text_parts.append(part.text)

                        # 处理内联图片 (inline_data)
                        if hasattr(part, 'inline_data') and part.inline_data:
                            has_images = True
                            blob = part.inline_data
                            if hasattr(blob, 'data'):
                                if isinstance(blob.data, bytes):
                                    img_data = blob.data
                                else:
                                    img_data = bytes(blob.data)

                                mime_type = getattr(blob, 'mime_type', 'image/png') or 'image/png'
                                b64_data = base64.b64encode(img_data).decode('utf-8')
                                data_url = f"data:{mime_type};base64,{b64_data}"

                                image_parts.append({
                                    "type": "image_url",
                                    "image_url": {"url": data_url}
                                })

                        # 处理 file_data（来自 upload 的文件）
                        if hasattr(part, 'file_data') and part.file_data:
                            has_images = True

                    # 构建 OpenAI 消息格式
                    if text_parts or image_parts:
                        msg_content = []
                        if text_parts:
                            combined_text = "\n".join(text_parts)
                            msg_content.append({"type": "text", "text": combined_text})
                        msg_content.extend(image_parts)
                        messages.append({
                            "role": "user",
                            "content": msg_content if len(msg_content) > 1 else (msg_content[0] if msg_content else "")
                        })

                elif hasattr(content, 'role') and hasattr(content, 'parts'):
                    role = 'assistant' if content.role == 'model' else content.role
                    text = " ".join([p.text for p in content.parts if hasattr(p, 'text')])
                    if text:
                        messages.append({"role": role, "content": text})

            return messages, has_images

        # ==========================================
        # 辅助函数：从响应文本中提取 JSON
        # ==========================================
        def _extract_json_from_response(response_text: str, response_schema=None):
            """
            从模型响应中提取 JSON（支持多种格式）

            尝试顺序：
            1. 直接解析整个响应
            2. 提取 ```json 代码块
            3. 提取 ``` 代码块
            4. 提取 { } 范围内的内容
            """
            if not response_text:
                return None

            # 方法 1：直接解析
            try:
                json_data = json.loads(response_text.strip())
                if response_schema:
                    return response_schema(**json_data)
                return json_data
            except (json.JSONDecodeError, Exception):
                pass

            # 方法 2：提取 ```json 代码块
            json_match = re.search(r'```json\s*([\s\S]*?)```', response_text)
            if json_match:
                try:
                    json_data = json.loads(json_match.group(1).strip())
                    if response_schema:
                        return response_schema(**json_data)
                    return json_data
                except (json.JSONDecodeError, Exception):
                    pass

            # 方法 3：提取 ``` 代码块（无语言标记）
            code_match = re.search(r'```\s*([\s\S]*?)```', response_text)
            if code_match:
                try:
                    json_data = json.loads(code_match.group(1).strip())
                    if response_schema:
                        return response_schema(**json_data)
                    return json_data
                except (json.JSONDecodeError, Exception):
                    pass

            # 方法 4：提取 { } 范围内的内容
            brace_match = re.search(r'\{[\s\S]*\}', response_text)
            if brace_match:
                try:
                    json_data = json.loads(brace_match.group(0))
                    if response_schema:
                        return response_schema(**json_data)
                    return json_data
                except (json.JSONDecodeError, Exception):
                    pass

            return None

        # ==========================================
        # 辅助函数：调用 OpenAI API（不使用 JSON mode）
        # ==========================================
        provider_router = ProviderRouter(event_sink=_record_provider_event)

        async def _call_openai_api(
            model: str,
            messages: List[dict],
            temperature: float = 0.7,
            max_tokens: int = 4096,
            response_schema=None,
            system_instruction: str = None,
        ) -> Any:
            """
            调用 OpenAI 兼容 API
            注意：不使用 response_format，因为部分视觉模型不支持
            """
            global _CAPTCHA_LAST_CALL_PROVIDER_TIMEOUT
            request_base_url = base_url
            use_opencode_free = str(model).endswith("-free")
            if use_opencode_free:
                request_base_url = os.getenv("OPENCODE_BASE_URL", "https://opencode.ai/zen").rstrip("/")
            url = f"{request_base_url}/v1/chat/completions"

            headers = {"Content-Type": "application/json"}
            if not use_opencode_free and os.getenv("OPENCODE_NO_AUTH", "").lower() not in {"1", "true", "yes", "on"}:
                headers["Authorization"] = f"Bearer {api_key}"

            # 构建消息列表
            final_messages = []

            # 添加 system instruction
            if system_instruction:
                final_messages.append({"role": "system", "content": system_instruction})

            # 如果有 response_schema，在 system 消息中添加格式要求
            if response_schema:
                schema_json = response_schema.model_json_schema()
                schema_str = json.dumps(schema_json, indent=2, ensure_ascii=False)
                spatial_instruction = ""
                if getattr(response_schema, "__name__", "") == "ImageAreaSelectChallenge":
                    spatial_instruction = """
坐标任务额外要求：
- 使用图片上标注的 X/Y 坐标轴读数，不要使用图片像素尺寸。
- 坐标必须落在目标物体的中心区域，不要点击边缘、空白、坐标轴或标题。
- 先比较所有候选目标，再估算目标完整包围框的左、右、上、下边界。
- 返回包围框的算术中心；花朵任务应点击花瓣汇聚的中心核心，绝不点击花瓣尖端。"""
                elif getattr(response_schema, "__name__", "") == "ImageDragDropChallenge":
                    spatial_instruction = """
Drag-and-drop coordinate rules:
- Drag only the movable animal icons in the left source column marked Move; start_point must be the center of a left source icon.
- end_point must be the center of an empty cell in the right grid; never drop onto a cell that already contains an animal icon.
- Do not use the left source column as a target area. The target is usually to the right of the source, so end_point.x should be clearly larger than start_point.x.
- Return coordinates in the full browser screenshot coordinate system used by the captcha page, not cropped-image coordinates.
- Every coordinate must be inside the visible hCaptcha panel. Do not return negative coordinates or coordinates outside the screenshot.
- Use at most one path for each movable source icon. If two source icons are needed, return exactly two paths.
- First infer the row and column pattern of the target grid and locate the empty cells, then place the matching animal in the center of the missing cell.
- Treat the target as a 4x4 grid when animal icons form four rows and four columns. The left source column is outside the 4x4 target grid.
- Empty cells are background-only squares inside the right grid. Existing animal cells already contain an icon and must not be used as end_point.
- Solve by table completion: write the animal type of every visible cell in the right grid, mark blanks as EMPTY, then infer each EMPTY from the repeated row/column pattern.
- Only choose an EMPTY cell if its inferred animal type matches one of the movable source icons.
- Common pattern example: if complete rows use [octopus, chicken, duck, frog] and a row is [octopus, EMPTY, duck, EMPTY], then column 2 requires chicken and column 4 requires frog.
- Common pattern example: if rows are [duck, bear, penguin, octopus] and a row is [duck, EMPTY, penguin, EMPTY], then column 2 requires bear and column 4 requires octopus.
- If the source icons are octopus and penguin, drag octopus only to the EMPTY cell whose row/column requires octopus, and penguin only to the EMPTY cell whose row/column requires penguin.
- Do not choose a visually empty cell simply because it is empty; it must also match the source animal and complete the pattern."""
                schema_instruction = f"""你必须严格按照以下 JSON Schema 格式返回响应。
返回的 JSON 必须包含在 ```json 代码块中。
不要输出分析过程、思考过程、解释文字或 Markdown 标题；只返回一个 ```json 代码块。

JSON Schema:
```json
{schema_str}
```

重要：请确保返回有效的 JSON 格式，包含在代码块中。
{spatial_instruction}"""
                if final_messages and final_messages[0].get("role") == "system":
                    final_messages[0]["content"] += "\n\n" + schema_instruction
                else:
                    final_messages.insert(0, {"role": "system", "content": schema_instruction})

            final_messages.extend(messages)

            payload = {
                "model": model,
                "messages": final_messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                # 注意：不使用 response_format，部分视觉模型不支持
            }

            is_structured_captcha = response_schema is not None
            if is_structured_captcha:
                providers = [
                    (
                        "nvidia",
                        settings.CAPTCHA_PRIMARY_BASE_URL,
                        settings.CAPTCHA_PRIMARY_API_KEY.get_secret_value(),
                        settings.CAPTCHA_PRIMARY_MODEL,
                        45.0,
                    ),
                    (
                        "siliconflow",
                        settings.CAPTCHA_SECONDARY_BASE_URL,
                        settings.CAPTCHA_SECONDARY_API_KEY.get_secret_value(),
                        settings.CAPTCHA_SECONDARY_MODEL,
                        60.0,
                    ),
                ]
                total_budget = float(os.getenv("CAPTCHA_TOTAL_API_BUDGET", "110"))
            else:
                providers = [(API_PROVIDER, request_base_url, api_key, model, 120.0)]
                total_budget = 120.0

            if is_structured_captcha:
                providers = _filter_session_captcha_providers(providers)
            specs = [ProviderSpec(*provider) for provider in providers]
            started = asyncio.get_running_loop().time()
            try:
                result = await provider_router.request(payload, specs, total_budget)
                logger.info(
                    f"captcha_provider_result=success elapsed="
                    f"{asyncio.get_running_loop().time() - started:.2f}s"
                )
                _CAPTCHA_LAST_CALL_PROVIDER_TIMEOUT = False
                return result
            except Exception as exc:
                _CAPTCHA_LAST_CALL_PROVIDER_TIMEOUT = isinstance(exc, ProviderUnavailable)
                logger.error(
                    f"captcha_provider_result=failed error={type(exc).__name__} "
                    f"elapsed={asyncio.get_running_loop().time() - started:.2f}s"
                )
                raise

        # ==========================================
        # 劫持 Client 初始化
        # ==========================================
        orig_init = genai.Client.__init__
        def new_init(self, *args, **kwargs):
            kwargs['api_key'] = api_key
            kwargs['http_options'] = types.HttpOptions(base_url="https://generativelanguage.googleapis.com")
            current_model = kwargs.get('model', settings.GEMINI_MODEL)
            logger.info(f"🚀 OpenAI 兼容补丁已应用 | 模型: {current_model}")
            orig_init(self, *args, **kwargs)

        genai.Client.__init__ = new_init

        # ==========================================
        # 劫持文件上传（存储到内存缓存）
        # ==========================================
        file_cache = {}

        # ==========================================
        # 验证码模型切换机制
        # ==========================================
        # 跟踪验证码调用状态，实现智能模型切换
        captcha_call_state = {
            'call_count': 0,          # 当前会话验证码调用次数
            'last_call_time': 0,      # 上次调用时间戳
            'use_fallback': False,    # 是否应该使用备用模型
            'success_count': 0,       # 成功次数
            'failure_count': 0,       # 失败次数（通过调用频率推断）
        }
        CAPTCHA_FAILURE_THRESHOLD = int(os.getenv("CAPTCHA_FALLBACK_AFTER_CALLS", "0"))
        CAPTCHA_TIME_WINDOW = int(os.getenv("CAPTCHA_FALLBACK_TIME_WINDOW", "300"))

        async def patched_upload(self_files, file, **kwargs):
            """将文件内容存储到内存缓存，返回伪造的文件 ID"""
            if hasattr(file, 'read'):
                content = file.read()
                if asyncio.iscoroutine(content):
                    content = await content
            elif isinstance(file, (str, Path)):
                with open(file, 'rb') as f:
                    content = f.read()
            else:
                content = bytes(file)

            if asyncio.iscoroutine(content):
                content = await content

            file_id = f"sf_{id(content)}_{len(content)}"
            file_cache[file_id] = content
            pass  # 文件缓存日志已移除
            return types.File(name=file_id, uri=file_id, mime_type="image/png")

        genai.files.AsyncFiles.upload = patched_upload

        # ==========================================
        # 劫持 generate_content：核心转换逻辑
        # ==========================================
        orig_generate = genai.models.AsyncModels.generate_content

        async def patched_generate(self_models, model, contents, **kwargs):
            """
            将 Gemini API 调用转换为 OpenAI API 调用
            从响应中提取 JSON 代码块
            支持模型自动切换（验证码任务 vs 普通任务）
            """
            # 用于跟踪是否需要使用备用模型
            use_fallback = False

            try:
                # 标准化 contents
                normalized = contents if isinstance(contents, list) else [contents]

                # 检查是否有缓存文件需要处理
                has_cached_files = False
                for content in normalized:
                    if hasattr(content, 'parts'):
                        for part in content.parts:
                            if hasattr(part, 'file_data') and part.file_data:
                                file_uri = getattr(part.file_data, 'file_uri', None) or getattr(part.file_data, 'uri', None)
                                if file_uri and file_uri in file_cache:
                                    has_cached_files = True
                                    data = file_cache[file_uri]
                                    if not hasattr(part, 'inline_data') or part.inline_data is None:
                                        part.inline_data = types.Blob(data=data, mime_type="image/png")
                                    else:
                                        part.inline_data.data = data

                # 转换为 OpenAI 格式
                messages, has_images = _convert_gemini_to_openai(normalized, model)

                if not messages:
                    raise ValueError("无法从 contents 中提取有效消息")

                # 判断任务类型并选择合适的模型
                is_captcha_task = has_images or has_cached_files

                # 获取当前时间戳
                import time
                current_time = time.time()

                if is_captcha_task:
                    # 检查是否需要重置计数器（超过时间窗口）
                    if current_time - captcha_call_state['last_call_time'] > CAPTCHA_TIME_WINDOW:
                        captcha_call_state['call_count'] = 0
                        captcha_call_state['use_fallback'] = False
                        logger.debug("🔄 验证码计数器已重置（超过时间窗口）")

                    # 更新调用计数
                    captcha_call_state['call_count'] += 1
                    captcha_call_state['last_call_time'] = current_time

                    # 判断是否应该使用备用模型
                    # 当连续调用次数超过阈值时，切换到备用模型
                    if CAPTCHA_FAILURE_THRESHOLD > 0 and captcha_call_state['call_count'] > CAPTCHA_FAILURE_THRESHOLD:
                        captcha_call_state['use_fallback'] = True
                        logger.info(f"🔄 验证码重试次数过多（{captcha_call_state['call_count']}次），切换到备用模型")
                        selected_model = settings.CAPTCHA_MODEL_FALLBACK
                    else:
                        selected_model = settings.CAPTCHA_MODEL

                    logger.debug(f"🎯 验证码调用 #{captcha_call_state['call_count']} | 模型: {selected_model}")
                else:
                    selected_model = settings.PRIMARY_MODEL

                logger.debug(f"🤖 调用 OpenAI 兼容 API | 模型: {selected_model} | 图片: {is_captcha_task}")

                # 提取配置参数
                config = kwargs.get('config', {})
                temperature = getattr(config, 'temperature', 0.7) if hasattr(config, 'temperature') else 0.7
                max_tokens = getattr(config, 'max_output_tokens', 4096) if hasattr(config, 'max_output_tokens') else 4096

                # 提取 response_schema（结构化输出）
                response_schema = None
                if hasattr(config, 'response_schema'):
                    response_schema = config.response_schema
                    logger.debug(f"📋 检测到 response_schema: {response_schema.__name__ if hasattr(response_schema, '__name__') else response_schema}")
                    max_tokens = min(max_tokens if isinstance(max_tokens, int) else 4096, int(os.getenv("CAPTCHA_RESPONSE_MAX_TOKENS", "1200")))
                    temperature = min(temperature if isinstance(temperature, (int, float)) else 0.7, 0.2)

                # 提取 system_instruction
                system_instruction = None
                if hasattr(config, 'system_instruction'):
                    if hasattr(config.system_instruction, 'parts'):
                        for part in config.system_instruction.parts:
                            if hasattr(part, 'text'):
                                system_instruction = part.text
                                break

                # 调用 OpenAI API
                result = await _call_openai_api(
                    model=selected_model,
                    messages=messages,
                    temperature=temperature if isinstance(temperature, (int, float)) else 0.7,
                    max_tokens=max_tokens if isinstance(max_tokens, int) else 4096,
                    response_schema=response_schema,
                    system_instruction=system_instruction,
                )

                # 提取响应文本
                message = result.get('choices', [{}])[0].get('message', {})
                response_text = message.get('content') or message.get('reasoning') or ''
                if not response_text and message.get('reasoning_details'):
                    response_text = "\n".join(
                        str(item.get('text', ''))
                        for item in message.get('reasoning_details', [])
                        if isinstance(item, dict)
                    )
                logger.debug(f"📄 原始响应: {repr(response_text[:300])}")

                # 处理结构化输出
                parsed_response = None
                if response_schema and response_text:
                    parsed_response = _extract_json_from_response(response_text, response_schema)
                    if parsed_response:
                        logger.debug(f"✅ JSON 解析成功")
                    else:
                        logger.debug(f"⚠️ JSON 解析失败，返回原始文本")

                # 构建 Gemini 格式的响应
                response = types.GenerateContentResponse(
                    candidates=[
                        types.Candidate(
                            content=types.Content(
                                parts=[types.Part(text=response_text)],
                                role='model'
                            ),
                            finish_reason='STOP'
                        )
                    ]
                )

                # 如果有解析好的结构化响应，设置 parsed 属性
                if parsed_response:
                    response.parsed = parsed_response

                return response

            except Exception as e:
                error_str = str(e)
                logger.error(f"❌ API 调用异常: {error_str}")

                # 验证码请求已在 _call_openai_api 内完成跨供应商故障转移，禁止重复调用。
                if is_captcha_task:
                    raise

                # 尝试使用备用模型重试
                fallback_model = settings.PRIMARY_MODEL_FALLBACK
                logger.debug(f"⚠️ 尝试使用备用主力模型: {fallback_model}")

                # 重试一次
                try:
                    result = await _call_openai_api(
                        model=fallback_model,
                        messages=messages,
                        temperature=temperature if isinstance(temperature, (int, float)) else 0.7,
                        max_tokens=max_tokens if isinstance(max_tokens, int) else 4096,
                        response_schema=response_schema,
                        system_instruction=system_instruction,
                    )

                    message = result.get('choices', [{}])[0].get('message', {})
                    response_text = message.get('content') or message.get('reasoning') or ''
                    if not response_text and message.get('reasoning_details'):
                        response_text = "\n".join(
                            str(item.get('text', ''))
                            for item in message.get('reasoning_details', [])
                            if isinstance(item, dict)
                        )
                    logger.debug(f"📄 备用模型响应: {repr(response_text[:300])}")

                    # 处理结构化输出
                    parsed_response = None
                    if response_schema and response_text:
                        parsed_response = _extract_json_from_response(response_text, response_schema)
                        if parsed_response:
                            logger.debug(f"✅ 备用模型 JSON 解析成功")

                    response = types.GenerateContentResponse(
                        candidates=[
                            types.Candidate(
                                content=types.Content(
                                    parts=[types.Part(text=response_text)],
                                    role='model'
                                ),
                                finish_reason='STOP'
                            )
                        ]
                    )

                    if parsed_response:
                        response.parsed = parsed_response

                    return response

                except Exception as fallback_error:
                    logger.error(f"❌ 备用模型也失败: {fallback_error}")
                    raise

        genai.models.AsyncModels.generate_content = patched_generate
        logger.info("✅ OpenAI 兼容补丁加载成功")

    except Exception as e:
        logger.error(f"❌ 严重：OpenAI 兼容补丁加载失败! 原因: {e}")
        import traceback
        traceback.print_exc()

# ==========================================
# 加载 OpenAI 兼容补丁
# ==========================================
_apply_openai_compatible_patch()

# 导出
__all__ = ['settings']
