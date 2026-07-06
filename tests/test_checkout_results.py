import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from services.epic_games_service import (
    EpicAgent,
    EpicGames,
    GameCollectResult,
    _fetch_order_items,
)
from hcaptcha_challenger.models import ChallengeSignal


class CheckoutResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_unconfirmed_captcha_checkout_raises_instead_of_succeeding(self):
        payment_button = SimpleNamespace(
            text_content=AsyncMock(return_value="Place Order"),
            click=AsyncMock(),
            is_visible=AsyncMock(return_value=True),
        )
        page = SimpleNamespace(
            url="https://store.epicgames.com/en-US/p/example",
            wait_for_timeout=AsyncMock(),
        )
        games = EpicGames(page)

        with (
            patch.object(
                games,
                "_handle_device_not_supported_modal",
                AsyncMock(return_value=False),
            ),
            patch.object(
                games,
                "_active_purchase_container",
                AsyncMock(return_value=(page, payment_button)),
            ),
            patch.object(
                games,
                "_wait_for_checkout_surface",
                AsyncMock(return_value="web_purchase_iframe"),
            ),
            patch.object(games, "_click_checkout_cta", AsyncMock()),
            patch.object(
                games,
                "_wait_for_checkout_confirmation",
                AsyncMock(side_effect=[False, False]),
            ),
            patch.object(
                games,
                "_has_visible_hcaptcha_challenge",
                AsyncMock(return_value=True),
            ),
            patch(
                "services.epic_games_service.AgentV",
                return_value=SimpleNamespace(
                    wait_for_challenge=AsyncMock(
                        return_value=ChallengeSignal.EXECUTION_TIMEOUT
                    ),
                    _captcha_payload_queue=SimpleNamespace(empty=lambda: False),
                ),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "captcha checkout verification failed"):
                await games._handle_instant_checkout(page, page.url)

    async def test_checkout_confirms_order_without_invoking_captcha_solver(self):
        payment_button = SimpleNamespace(
            text_content=AsyncMock(return_value="Add to library"),
            is_visible=AsyncMock(return_value=False),
        )
        page = SimpleNamespace(
            url="https://store.epicgames.com/en-US/p/example",
            wait_for_timeout=AsyncMock(),
            remove_listener=Mock(),
        )
        games = EpicGames(page)
        solver = SimpleNamespace(wait_for_challenge=AsyncMock(), _task_handler=Mock())

        with (
            patch.object(games, "_handle_device_not_supported_modal", AsyncMock(return_value=False)),
            patch.object(
                games,
                "_active_purchase_container",
                AsyncMock(return_value=(page, payment_button)),
            ),
            patch.object(
                games,
                "_wait_for_checkout_surface",
                AsyncMock(return_value="web_purchase_iframe"),
            ),
            patch.object(games, "_click_checkout_cta", AsyncMock()),
            patch.object(
                games,
                "_wait_for_checkout_confirmation",
                AsyncMock(return_value=True),
            ),
            patch("services.epic_games_service.AgentV", return_value=solver),
        ):
            result = await games._handle_instant_checkout(page, page.url)

        self.assertTrue(result)
        solver.wait_for_challenge.assert_not_awaited()
        page.remove_listener.assert_called_once_with("response", solver._task_handler)

    async def test_order_history_request_does_not_navigate_checkout_page(self):
        namespace = "a" * 32
        payload = {
            "orders": [
                {
                    "orderType": "PURCHASE",
                    "orderId": "order-1",
                    "items": [
                        {
                            "description": "Game A",
                            "offerId": "offer-1",
                            "namespace": namespace,
                        }
                    ],
                }
            ]
        }
        history_page = SimpleNamespace(
            goto=AsyncMock(return_value=SimpleNamespace(status=200)),
            locator=lambda _selector: SimpleNamespace(
                text_content=AsyncMock(return_value=json.dumps(payload))
            ),
            close=AsyncMock(),
        )
        page = SimpleNamespace(
            context=SimpleNamespace(new_page=AsyncMock(return_value=history_page)),
            goto=AsyncMock(),
        )

        orders = await _fetch_order_items(page)

        self.assertEqual([item.namespace for item in orders], [namespace])
        page.goto.assert_not_awaited()
        history_page.goto.assert_awaited_once()
        history_page.close.assert_awaited_once()

    async def test_page_wide_owned_text_does_not_mark_product_owned(self):
        product_button = SimpleNamespace(
            is_visible=AsyncMock(return_value=True),
            text_content=AsyncMock(return_value="Get"),
        )
        page = SimpleNamespace(
            locator=lambda selector: (
                SimpleNamespace(first=product_button)
                if selector == "button[data-testid='purchase-cta-button']"
                else SimpleNamespace(text_content=AsyncMock(return_value="Owned elsewhere"))
            )
        )

        self.assertFalse(await EpicGames._current_product_is_owned(page))

    async def test_failed_game_result_prevents_global_success(self):
        page = SimpleNamespace()
        games = EpicGames(page)
        promotion = SimpleNamespace(title="Game A", url="https://example.test/game-a")

        with patch.object(
            games,
            "add_promotion_to_cart",
            AsyncMock(return_value=(False, {"Game A": "failed"})),
        ):
            with self.assertRaisesRegex(RuntimeError, "Game A"):
                await games.collect_weekly_games([promotion])

    async def test_unconfirmed_checkout_has_explicit_error_type(self):
        agent = EpicAgent(SimpleNamespace())
        agent._promotions = [
            SimpleNamespace(title="Game A", url="https://example.test/game-a")
        ]
        agent._should_ignore_task = AsyncMock(
            return_value=(False, GameCollectResult.SUCCESS)
        )
        agent.epic_games.collect_weekly_games = AsyncMock(
            side_effect=RuntimeError("以下游戏未能确认领取成功: Game A")
        )

        result = await agent.collect_epic_games()

        self.assertEqual(result, GameCollectResult.CHECKOUT_FAILED)

    async def test_wait_for_checkout_surface_waits_for_delayed_purchase_frame(self):
        page = _CheckoutSurfacePage(visible_after=3)

        state = await EpicGames._wait_for_checkout_surface(page, timeout_ms=2000)

        self.assertEqual(state, "purchase_frame")
        self.assertGreaterEqual(page.visible_checks, 3)

    async def test_active_purchase_container_prefers_purchase_iframe(self):
        button = _FakeButton("Place Order")
        purchase_frame = _FakeContainer([button], url="https://store.epicgames.com/purchase?offers=1")
        page = _FakeCheckoutPage(
            frames=[_FakeContainer([], url="https://example.test/ads"), purchase_frame],
            iframe_visible=True,
        )

        with patch.object(EpicGames, "_wait_for_checkout_surface", AsyncMock(return_value="purchase_frame")):
            container, found = await EpicGames._active_purchase_container(page)

        self.assertIs(container, purchase_frame)
        self.assertIs(found, button)

    async def test_active_purchase_container_finds_add_to_library_by_enumeration(self):
        button = _FakeButton("Add to library")
        purchase_frame = _FakeContainer(
            [button],
            url="https://store.epicgames.com/purchase?offers=1",
            fail_has_text=True,
        )
        page = _FakeCheckoutPage(frames=[purchase_frame], iframe_visible=True)

        with patch.object(EpicGames, "_wait_for_checkout_surface", AsyncMock(return_value="purchase_frame")):
            container, found = await EpicGames._active_purchase_container(page)

        self.assertIs(container, purchase_frame)
        self.assertIs(found, button)

    async def test_active_purchase_container_accepts_enumerated_button_with_unstable_visible_check(self):
        button = _FakeButton("Add to library", visible_check_fails=True)
        purchase_frame = _FakeContainer(
            [button],
            url="https://store.epicgames.com/purchase?offers=1",
        )
        page = _FakeCheckoutPage(frames=[purchase_frame], iframe_visible=True)

        with patch.object(EpicGames, "_wait_for_checkout_surface", AsyncMock(return_value="purchase_frame")):
            container, found = await EpicGames._active_purchase_container(page)

        self.assertIs(container, purchase_frame)
        self.assertIs(found, button)

    async def test_captcha_checkout_error_maps_to_captcha_failed(self):
        agent = EpicAgent(SimpleNamespace())
        agent._promotions = [
            SimpleNamespace(title="Game A", url="https://example.test/game-a")
        ]
        agent._should_ignore_task = AsyncMock(
            return_value=(False, GameCollectResult.SUCCESS)
        )
        agent.epic_games.collect_weekly_games = AsyncMock(
            side_effect=RuntimeError("captcha checkout verification failed: captcha timeout")
        )

        result = await agent.collect_epic_games()

        self.assertEqual(result, GameCollectResult.CAPTCHA_FAILED)

    async def test_driver_disconnect_maps_to_driver_crash(self):
        agent = EpicAgent(SimpleNamespace())
        agent._promotions = [
            SimpleNamespace(title="Game A", url="https://example.test/game-a")
        ]
        agent._should_ignore_task = AsyncMock(
            return_value=(False, GameCollectResult.SUCCESS)
        )
        agent.epic_games.collect_weekly_games = AsyncMock(
            side_effect=RuntimeError("Page.goto: Connection closed while reading from the driver")
        )

        result = await agent.collect_epic_games()

        self.assertEqual(result, GameCollectResult.DRIVER_CRASH)

    async def test_product_cta_click_falls_back_to_force_click(self):
        button = SimpleNamespace(
            click=AsyncMock(side_effect=[RuntimeError("normal click timeout"), None]),
            evaluate=AsyncMock(),
        )

        await EpicGames._click_product_cta(button)

        self.assertEqual(button.click.await_count, 2)
        button.evaluate.assert_not_awaited()

    async def test_product_page_cta_uses_keyboard_when_mouse_click_times_out(self):
        button = SimpleNamespace(
            wait_for=AsyncMock(),
            click=AsyncMock(side_effect=RuntimeError("mouse click timeout")),
            evaluate=AsyncMock(side_effect=[RuntimeError("native click timeout"), None]),
        )
        keyboard = SimpleNamespace(press=AsyncMock())
        page = SimpleNamespace(
            locator=lambda _selector: SimpleNamespace(first=button),
            keyboard=keyboard,
            wait_for_timeout=AsyncMock(),
        )

        with patch.object(
            EpicGames,
            "_wait_for_checkout_surface",
            AsyncMock(return_value="web_purchase_iframe"),
        ):
            await EpicGames._click_product_page_cta(page)

        self.assertEqual(button.evaluate.await_count, 2)
        keyboard.press.assert_awaited_once_with("Enter")
        self.assertEqual(button.click.await_count, 1)


class _CheckoutSurfacePage:
    def __init__(self, visible_after):
        self.url = "https://store.epicgames.com/en-US/p/example"
        self.visible_after = visible_after
        self.visible_checks = 0

    @property
    def frames(self):
        self.visible_checks += 1
        if self.visible_checks >= self.visible_after:
            return [SimpleNamespace(url="https://store.epicgames.com/purchase?offers=1")]
        return []


class _TextLocator:
    def __init__(self, text):
        self.text = text

    async def text_content(self, timeout=0):
        return self.text


class _FakeCheckoutPage:
    def __init__(self, frames, iframe_visible=False):
        self.url = "https://store.epicgames.com/en-US/p/example"
        self.main_frame = object()
        self.frames = [self.main_frame, *frames]
        self.iframe_visible = iframe_visible

    def locator(self, selector, **_kwargs):
        if selector == "#webPurchaseContainer iframe":
            return SimpleNamespace(first=_StaticVisibleLocator(self.iframe_visible))
        return _FakeLocator([])

    def frame_locator(self, _selector):
        return _FakeContainer([])


class _StaticVisibleLocator:
    def __init__(self, visible):
        self.visible = visible

    async def is_visible(self, timeout=0):
        return self.visible


class _FakeContainer:
    def __init__(self, buttons=None, url="https://example.test/frame", fail_has_text=False):
        self.buttons = buttons or []
        self.url = url
        self.name = ""
        self.main_frame = None
        self.fail_has_text = fail_has_text

    def locator(self, selector, **kwargs):
        if selector == "button":
            has_text = kwargs.get("has_text")
            if has_text is not None:
                if self.fail_has_text:
                    return _FakeLocator([])
                return _FakeLocator([
                    button for button in self.buttons if has_text.lower() in button.text.lower()
                ])
            return _FakeLocator(self.buttons)
        return _FakeLocator([])


class _FakeLocator:
    def __init__(self, items):
        self.items = items
        self.first = items[0] if items else _MissingButton()

    async def all(self):
        return self.items


class _FakeButton:
    def __init__(self, text, disabled=False, visible_check_fails=False):
        self.text = text
        self.disabled = disabled
        self.visible_check_fails = visible_check_fails

    async def wait_for(self, state="visible", timeout=0):
        if self.visible_check_fails:
            raise RuntimeError("visible check timeout")
        return None

    async def is_disabled(self, timeout=0):
        return self.disabled

    async def text_content(self, timeout=0):
        return self.text

    async def get_attribute(self, _name, timeout=0):
        return None


class _MissingButton:
    async def wait_for(self, state="visible", timeout=0):
        raise RuntimeError("missing")


if __name__ == "__main__":
    unittest.main()
