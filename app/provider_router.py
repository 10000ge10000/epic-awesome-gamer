from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    base_url: str
    api_key: str
    model: str
    read_timeout: float


class ProviderRejected(RuntimeError):
    pass


class ProviderUnavailable(RuntimeError):
    pass


class ProviderRouter:
    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        circuit_seconds: float = 600,
        clock: Callable[[], float] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.failure_threshold = failure_threshold
        self.circuit_seconds = circuit_seconds
        self.clock = clock or time.monotonic
        self.transport = transport
        self.event_sink = event_sink
        self.state: dict[str, dict[str, float]] = {}

    def _emit(self, **event: Any) -> None:
        if self.event_sink:
            self.event_sink(event)

    async def request(
        self, payload: dict[str, Any], providers: list[ProviderSpec], total_budget: float
    ) -> dict[str, Any]:
        deadline = self.clock() + total_budget
        failures: list[str] = []
        for provider in providers:
            if not provider.api_key:
                failures.append(f"{provider.name}:missing_key")
                self._emit(
                    event="captcha_provider_attempt",
                    provider=provider.name,
                    outcome="missing_key",
                )
                continue
            state = self.state.setdefault(provider.name, {"failures": 0, "open_until": 0})
            now = self.clock()
            if state["open_until"] > now:
                failures.append(f"{provider.name}:circuit_open")
                self._emit(
                    event="captcha_provider_attempt",
                    provider=provider.name,
                    outcome="circuit_open",
                    remaining=round(state["open_until"] - now, 3),
                )
                continue
            remaining = deadline - now
            if remaining <= 0:
                failures.append("total_budget_exhausted")
                break

            base_url = provider.base_url.rstrip("/")
            if base_url.endswith("/v1"):
                base_url = base_url[:-3]
            request_payload = dict(payload)
            request_payload["model"] = provider.model
            timeout = httpx.Timeout(
                min(provider.read_timeout, remaining),
                connect=min(10.0, remaining),
                write=min(20.0, remaining),
                pool=min(10.0, remaining),
            )
            started = self.clock()
            try:
                async with httpx.AsyncClient(
                    timeout=timeout,
                    trust_env=False,
                    transport=self.transport,
                ) as client:
                    async with asyncio.timeout(remaining):
                        response = await client.post(
                            f"{base_url}/v1/chat/completions",
                            headers={
                                "Content-Type": "application/json",
                                "Authorization": f"Bearer {provider.api_key}",
                            },
                            json=request_payload,
                        )
                request_id = response.headers.get("x-request-id") or response.headers.get(
                    "request-id", ""
                )
                if response.status_code == 200:
                    try:
                        result = response.json()
                    except ValueError:
                        failures.append(f"{provider.name}:invalid_json")
                        self._emit(
                            event="captcha_provider_attempt",
                            provider=provider.name,
                            outcome="invalid_json",
                            status=200,
                            request_id=request_id,
                            elapsed=round(self.clock() - started, 3),
                        )
                    else:
                        state["failures"] = 0
                        state["open_until"] = 0
                        self._emit(
                            event="captcha_provider_attempt",
                            provider=provider.name,
                            outcome="success",
                            status=200,
                            request_id=request_id,
                            elapsed=round(self.clock() - started, 3),
                        )
                        return result
                elif response.status_code in {401, 403}:
                    # 鉴权类错误才快速失败：三级 provider 目前共用同一把 SiliconFlow
                    # key，换一级重试必然同样是 401/403，只会白白耗掉 total_budget。
                    self._emit(
                        event="captcha_provider_attempt",
                        provider=provider.name,
                        outcome="rejected",
                        status=response.status_code,
                        request_id=request_id,
                        elapsed=round(self.clock() - started, 3),
                    )
                    raise ProviderRejected(
                        f"provider_rejected: provider={provider.name} status={response.status_code}"
                    )
                else:
                    # 其余状态码一律计入失败并继续尝试下一级 provider。除了 429/5xx，
                    # 这里还包括 400/404/413/422 这类**模型相关**的拒绝 —— 例如 GLM-4.5V
                    # 会对尺寸不合规的图返回 400 "height or width must be larger than 28"，
                    # 而同链路上的 Kimi 模型能正常处理。此前这类响应会直接中断整条降级链。
                    failures.append(f"{provider.name}:http_{response.status_code}")
                    self._emit(
                        event="captcha_provider_attempt",
                        provider=provider.name,
                        outcome="http_error",
                        status=response.status_code,
                        request_id=request_id,
                        elapsed=round(self.clock() - started, 3),
                    )
            except (TimeoutError, httpx.TimeoutException, httpx.NetworkError) as exc:
                failures.append(f"{provider.name}:{type(exc).__name__}")
                self._emit(
                    event="captcha_provider_attempt",
                    provider=provider.name,
                    outcome="network_error",
                    error_type=type(exc).__name__,
                    elapsed=round(self.clock() - started, 3),
                )

            state["failures"] += 1
            if state["failures"] >= self.failure_threshold:
                state["open_until"] = self.clock() + self.circuit_seconds
                self._emit(
                    event="captcha_provider_attempt",
                    provider=provider.name,
                    outcome="circuit_opened",
                    ttl_seconds=self.circuit_seconds,
                )

        raise ProviderUnavailable("provider_timeout: " + ",".join(failures))
