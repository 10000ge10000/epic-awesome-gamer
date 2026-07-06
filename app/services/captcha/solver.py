# -*- coding: utf-8 -*-
"""Captcha solver strategy layer."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

import requests
from hcaptcha_challenger.agent import AgentV
from hcaptcha_challenger.models import ChallengeSignal
from loguru import logger
from playwright.async_api import Page


class CaptchaSolveStatus(str, Enum):
    SUCCESS = "success"
    RETRY = "retry"
    TIMEOUT = "timeout"
    FAILED = "failed"
    MANUAL_REQUIRED = "manual_required"
    DISABLED = "disabled"


@dataclass(slots=True)
class CaptchaSolveResult:
    status: CaptchaSolveStatus
    signal: str | None = None
    message: str = ""
    token: str | None = None
    provider: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == CaptchaSolveStatus.SUCCESS


class CaptchaSolver(Protocol):
    async def solve(self) -> CaptchaSolveResult:
        ...


class HCaptchaChallengerSolver:
    """Minimal hcaptcha-challenger provider wrapper."""

    def __init__(self, agent: AgentV):
        self.agent = agent

    async def solve(self) -> CaptchaSolveResult:
        try:
            signal = await self.agent.wait_for_challenge()
        except TimeoutError as exc:
            logger.warning(f"Captcha solver timeout: {exc}")
            return CaptchaSolveResult(CaptchaSolveStatus.TIMEOUT, message=str(exc))
        except Exception as exc:
            logger.warning(f"Captcha solver exception: {exc}")
            return CaptchaSolveResult(CaptchaSolveStatus.FAILED, message=str(exc))

        signal_value = getattr(signal, "value", str(signal))
        if signal == ChallengeSignal.SUCCESS:
            return CaptchaSolveResult(CaptchaSolveStatus.SUCCESS, signal_value)
        if signal in {ChallengeSignal.RETRY, ChallengeSignal.START}:
            return CaptchaSolveResult(CaptchaSolveStatus.RETRY, signal_value)
        if signal in {ChallengeSignal.EXECUTION_TIMEOUT, ChallengeSignal.RESPONSE_TIMEOUT}:
            return CaptchaSolveResult(CaptchaSolveStatus.TIMEOUT, signal_value)
        return CaptchaSolveResult(CaptchaSolveStatus.FAILED, signal_value)


class TwoCaptchaTokenSolver:
    """2Captcha hCaptcha token solver.

    This solver is disabled unless an API key is explicitly configured. It only
    returns a hCaptcha response token; the caller decides how to apply it to the
    current browser page.
    """

    IN_URL = "https://2captcha.com/in.php"
    RES_URL = "https://2captcha.com/res.php"

    def __init__(
        self,
        *,
        api_key: str,
        site_key: str,
        page_url: str,
        timeout_seconds: int = 180,
        poll_interval_seconds: int = 5,
        session: Any | None = None,
    ):
        self.api_key = api_key.strip()
        self.site_key = site_key.strip()
        self.page_url = page_url.strip()
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.session = session or requests.Session()

    async def solve(self) -> CaptchaSolveResult:
        if not self.api_key:
            return CaptchaSolveResult(
                CaptchaSolveStatus.DISABLED,
                provider="2captcha",
                message="2Captcha API key is not configured",
            )
        if not self.site_key or not self.page_url:
            return CaptchaSolveResult(
                CaptchaSolveStatus.FAILED,
                provider="2captcha",
                message="hCaptcha sitekey or page URL is missing",
            )
        return await asyncio.to_thread(self._solve_sync)

    def _solve_sync(self) -> CaptchaSolveResult:
        try:
            submit = self.session.post(
                self.IN_URL,
                data={
                    "key": self.api_key,
                    "method": "hcaptcha",
                    "sitekey": self.site_key,
                    "pageurl": self.page_url,
                    "json": 1,
                },
                timeout=30,
            )
            submit.raise_for_status()
            submitted = submit.json()
        except Exception as exc:
            return CaptchaSolveResult(
                CaptchaSolveStatus.FAILED,
                provider="2captcha",
                message=f"submit failed: {exc}",
            )

        if submitted.get("status") != 1:
            return CaptchaSolveResult(
                CaptchaSolveStatus.FAILED,
                provider="2captcha",
                message=str(submitted.get("request", "submit rejected")),
            )

        captcha_id = str(submitted.get("request", ""))
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            time.sleep(self.poll_interval_seconds)
            try:
                response = self.session.get(
                    self.RES_URL,
                    params={
                        "key": self.api_key,
                        "action": "get",
                        "id": captcha_id,
                        "json": 1,
                    },
                    timeout=30,
                )
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                return CaptchaSolveResult(
                    CaptchaSolveStatus.FAILED,
                    provider="2captcha",
                    message=f"poll failed: {exc}",
                )

            request_value = str(payload.get("request", ""))
            if payload.get("status") == 1 and request_value:
                return CaptchaSolveResult(
                    CaptchaSolveStatus.SUCCESS,
                    provider="2captcha",
                    token=request_value,
                )
            if request_value != "CAPCHA_NOT_READY":
                return CaptchaSolveResult(
                    CaptchaSolveStatus.FAILED,
                    provider="2captcha",
                    message=request_value or "poll rejected",
                )

        return CaptchaSolveResult(
            CaptchaSolveStatus.TIMEOUT,
            provider="2captcha",
            message="2Captcha solve timed out",
        )


async def inject_hcaptcha_token(page: Page, token: str) -> None:
    """Inject a provider token and unblock Epic's login form."""
    await page.evaluate(
        """
        (token) => {
          const touched = new Set();
          const writeToken = (field) => {
            if (!field || touched.has(field)) {
              return;
            }
            touched.add(field);
            field.value = token;
            field.innerHTML = token;
            field.textContent = token;
            field.dispatchEvent(new Event('input', { bubbles: true }));
            field.dispatchEvent(new Event('change', { bubbles: true }));
          };

          const selectors = [
            'textarea[name="h-captcha-response"]',
            'textarea[name="g-recaptcha-response"]',
            'textarea[id^="h-captcha-response"]',
            'textarea[id^="g-recaptcha-response"]',
          ];
          selectors.forEach((selector) => {
            document.querySelectorAll(selector).forEach(writeToken);
          });

          document.querySelectorAll('iframe[data-hcaptcha-widget-id]').forEach((frame) => {
            frame.setAttribute('data-hcaptcha-response', token);
            frame.dataset.hcaptchaResponse = token;
          });

          document.querySelectorAll('[data-callback]').forEach((node) => {
            const callbackName = node.getAttribute('data-callback');
            if (!callbackName) {
              return;
            }
            const callback = callbackName.split('.').reduce((current, key) => current && current[key], window);
            if (typeof callback === 'function') {
              try {
                callback(token);
              } catch (_error) {}
            }
          });

          const signInButton = document.querySelector('#sign-in');
          if (signInButton) {
            signInButton.disabled = false;
            signInButton.removeAttribute('disabled');
            signInButton.removeAttribute('aria-disabled');
            signInButton.tabIndex = 0;
            signInButton.dispatchEvent(new Event('input', { bubbles: true }));
            signInButton.dispatchEvent(new Event('change', { bubbles: true }));
          }

          const talonOverlay = document.querySelector('#talon_container_login_prod');
          if (talonOverlay) {
            talonOverlay.style.display = 'none';
            talonOverlay.style.visibility = 'hidden';
            talonOverlay.setAttribute('aria-hidden', 'true');
          }

          const challengeContainer = document.querySelector('#h_captcha_challenge_login_prod');
          if (challengeContainer) {
            challengeContainer.style.display = 'none';
            challengeContainer.style.visibility = 'hidden';
          }

          if (window.talon && typeof window.talon.close === 'function') {
            try {
              window.talon.close('login_prod');
            } catch (_error) {}
          }

          document.querySelectorAll('form').forEach((form) => {
            form.dispatchEvent(new Event('input', { bubbles: true }));
            form.dispatchEvent(new Event('change', { bubbles: true }));
          });
        }
        """,
        token,
    )
