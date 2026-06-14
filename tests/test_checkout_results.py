import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services.epic_games_service import EpicAgent, EpicGames, GameCollectResult


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
            patch.object(games, "_product_is_owned", AsyncMock(return_value=False)),
            patch(
                "services.epic_games_service.AgentV",
                return_value=SimpleNamespace(
                    wait_for_challenge=AsyncMock(side_effect=RuntimeError("captcha timeout"))
                ),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "captcha checkout verification failed"):
                await games._handle_instant_checkout(page, page.url)

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


if __name__ == "__main__":
    unittest.main()
