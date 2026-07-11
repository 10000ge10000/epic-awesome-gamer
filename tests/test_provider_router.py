import unittest

import httpx
from unittest.mock import patch

from app.provider_router import ProviderRejected, ProviderRouter, ProviderSpec
from app import settings


class ProviderRouterTests(unittest.IsolatedAsyncioTestCase):
    def test_failed_provider_is_skipped_for_later_calls_in_same_captcha_session(self):
        providers = [
            ("nvidia", "https://primary.test", "a", "model-a", 45.0),
            ("siliconflow", "https://secondary.test", "b", "model-b", 60.0),
        ]
        settings._CAPTCHA_SESSION_UNAVAILABLE_PROVIDERS.clear()
        with patch.object(settings.redis_client, "Redis"):
            settings._record_provider_event(
                {
                    "provider": "nvidia",
                    "outcome": "network_error",
                    "error_type": "ReadTimeout",
                }
            )
        filtered = settings._filter_session_captcha_providers(providers)
        self.assertEqual([provider[0] for provider in filtered], ["siliconflow"])

        with patch.object(settings.redis_client, "Redis"):
            settings._record_provider_event({"provider": "nvidia", "outcome": "success"})
        self.assertEqual(
            [provider[0] for provider in settings._filter_session_captcha_providers(providers)],
            ["nvidia", "siliconflow"],
        )

    def test_provider_timeout_state_is_explicit(self):
        settings._CAPTCHA_LAST_CALL_PROVIDER_TIMEOUT = True
        self.assertTrue(settings.captcha_last_call_provider_timeout())
        settings._CAPTCHA_LAST_CALL_PROVIDER_TIMEOUT = False
        self.assertFalse(settings.captcha_last_call_provider_timeout())

    def test_redis_circuit_skips_provider_across_task_processes(self):
        providers = [
            ("nvidia", "https://primary.test", "a", "model-a", 45.0),
            ("siliconflow", "https://secondary.test", "b", "model-b", 60.0),
        ]
        settings._CAPTCHA_SESSION_UNAVAILABLE_PROVIDERS.clear()
        with patch.object(settings.redis_client, "Redis") as redis_cls:
            redis_cls.return_value.exists.side_effect = lambda key: key.endswith(":nvidia")
            filtered = settings._filter_session_captcha_providers(providers)
        self.assertEqual([provider[0] for provider in filtered], ["siliconflow"])

    async def test_retryable_primary_failure_uses_secondary(self):
        calls = []

        def handler(request):
            calls.append(request.url.host)
            if request.url.host == "primary.test":
                return httpx.Response(503, json={"error": "busy"})
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

        router = ProviderRouter(transport=httpx.MockTransport(handler))
        result = await router.request(
            {"messages": []},
            [
                ProviderSpec("primary", "https://primary.test/v1", "a", "model-a", 45),
                ProviderSpec("secondary", "https://secondary.test/v1", "b", "model-b", 60),
            ],
            110,
        )
        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        self.assertEqual(calls, ["primary.test", "secondary.test"])

    async def test_non_retryable_4xx_does_not_use_secondary(self):
        calls = []

        def handler(request):
            calls.append(request.url.host)
            return httpx.Response(401, json={"error": "unauthorized"})

        router = ProviderRouter(transport=httpx.MockTransport(handler))
        with self.assertRaises(ProviderRejected):
            await router.request(
                {"messages": []},
                [
                    ProviderSpec("primary", "https://primary.test", "a", "a", 45),
                    ProviderSpec("secondary", "https://secondary.test", "b", "b", 60),
                ],
                110,
            )
        self.assertEqual(calls, ["primary.test"])

    async def test_three_failures_open_circuit(self):
        now = [100.0]

        def handler(_request):
            return httpx.Response(503, json={"error": "busy"})

        router = ProviderRouter(
            clock=lambda: now[0], transport=httpx.MockTransport(handler), circuit_seconds=600
        )
        provider = ProviderSpec("primary", "https://primary.test", "a", "a", 45)
        for _ in range(3):
            with self.assertRaises(RuntimeError):
                await router.request({}, [provider], 110)
        self.assertEqual(router.state["primary"]["open_until"], 700.0)

    async def test_invalid_json_uses_secondary_and_emits_request_metadata(self):
        events = []

        def handler(request):
            if request.url.host == "primary.test":
                return httpx.Response(200, text="not-json", headers={"x-request-id": "req-1"})
            return httpx.Response(200, json={"choices": []}, headers={"request-id": "req-2"})

        router = ProviderRouter(
            transport=httpx.MockTransport(handler), event_sink=events.append
        )
        result = await router.request(
            {},
            [
                ProviderSpec("primary", "https://primary.test", "a", "a", 45),
                ProviderSpec("secondary", "https://secondary.test", "b", "b", 60),
            ],
            110,
        )

        self.assertEqual(result, {"choices": []})
        self.assertEqual(events[0]["outcome"], "invalid_json")
        self.assertEqual(events[0]["request_id"], "req-1")
        self.assertEqual(events[1]["provider"], "secondary")
        self.assertEqual(events[1]["request_id"], "req-2")


if __name__ == "__main__":
    unittest.main()
