import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import deploy
from services.epic_authorization_service import ErrorType


class DeployModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_verify_only_stops_after_successful_login(self):
        class Browser:
            def __init__(self):
                self.pages = [object()]

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        browser = Browser()
        auth = MagicMock()
        auth.invoke = AsyncMock(return_value=ErrorType.SUCCESS)

        with (
            patch.dict(
                os.environ,
                {
                    "EPIC_VERIFY_ONLY": "1",
                    "EPIC_PROFILE_ID": "46f83017-9d38-4b02-a0bc-fd79864e0675",
                },
                clear=False,
            ),
            patch.object(deploy, "AsyncCamoufox", return_value=browser),
            patch.object(deploy, "EpicAuthorization", return_value=auth),
            patch.object(deploy, "EpicAgent") as agent,
            patch.object(
                deploy.settings,
                "EPIC_PROFILE_ID",
                "46f83017-9d38-4b02-a0bc-fd79864e0675",
            ),
        ):
            result = await deploy.execute_browser_tasks()

        self.assertEqual(result, ErrorType.SUCCESS)
        auth.invoke.assert_awaited_once()
        agent.assert_not_called()


if __name__ == "__main__":
    unittest.main()
