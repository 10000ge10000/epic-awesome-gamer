import os
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.queue = []

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def setex(self, key, ttl, value):
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)

    def exists(self, key):
        return int(key in self.values)

    def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)

    def rpush(self, key, value):
        self.queue.append((key, value))
        return len(self.queue)

    def smembers(self, key):
        return set()

    def sadd(self, key, value):
        return 1

    def expire(self, key, ttl):
        return True

    def incr(self, key):
        value = int(self.values.get(key, 0)) + 1
        self.values[key] = value
        return value

    def ttl(self, key):
        return -1

    def ping(self):
        return True


class SecurityQueueTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tempdir.name
        os.environ["INTERNAL_API_TOKEN"] = "test-internal-token"
        os.environ["EPIC_CREDENTIAL_KEYS"] = "9QKUvZF-uPSzD4suKpprhwUTUyoj5nrR9BgeJeAs5mM="

        global main
        import main

        cls.main = main

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def setUp(self):
        self.fake_redis = FakeRedis()
        self.redis_patch = patch.object(self.main, "r", self.fake_redis)
        self.redis_patch.start()

    def tearDown(self):
        self.redis_patch.stop()

    def test_internal_api_rejects_missing_or_wrong_token(self):
        with self.assertRaises(self.main.HTTPException) as missing:
            self.main._require_internal_token(None)
        self.assertEqual(missing.exception.status_code, 401)

        with self.assertRaises(self.main.HTTPException) as wrong:
            self.main._require_internal_token("Bearer wrong")
        self.assertEqual(wrong.exception.status_code, 401)

        self.main._require_internal_token("Bearer test-internal-token")

    def test_worker_only_http_endpoint_rejects_public_request(self):
        from fastapi.testclient import TestClient

        client = TestClient(self.main.app)
        response = client.post("/api/nuke_account", json={"email": "victim@example.com"})
        self.assertEqual(response.status_code, 401)

    def test_admin_unban_requires_internal_token_and_clears_all_ban_state(self):
        from fastapi.testclient import TestClient

        client = TestClient(self.main.app)
        self.fake_redis.values.update(
            {
                "ban:203.0.113.10": "1",
                "temp_ban:203.0.113.10": "1",
                "rate:203.0.113.10": "9",
            }
        )
        denied = client.post("/api/admin/unban", json={"ip": "203.0.113.10"})
        self.assertEqual(denied.status_code, 401)

        allowed = client.post(
            "/api/admin/unban",
            json={"ip": "203.0.113.10"},
            headers={"Authorization": "Bearer test-internal-token"},
        )
        self.assertEqual(allowed.status_code, 200)
        self.assertNotIn("ban:203.0.113.10", self.fake_redis.values)
        self.assertNotIn("temp_ban:203.0.113.10", self.fake_redis.values)

    def test_sensitive_path_is_hidden(self):
        from fastapi.testclient import TestClient

        client = TestClient(self.main.app)
        response = client.get("/.env")
        self.assertEqual(response.status_code, 404)

    def test_enqueue_is_atomic_per_account(self):
        run_id = "46f83017-9d38-4b02-a0bc-fd79864e0675"
        self.assertTrue(self.main._enqueue_task(run_id, "user@example.com"))
        self.assertFalse(self.main._enqueue_task(run_id, "user@example.com"))
        self.assertEqual(len(self.fake_redis.queue), 1)
        payload = json.loads(self.fake_redis.queue[0][1])
        self.assertEqual(payload, {"run_id": run_id})
        self.assertNotIn("user@example.com", self.fake_redis.queue[0][1])
        self.assertNotIn("secret", self.fake_redis.queue[0][1])

    async def test_confirmation_token_is_one_time(self):
        cipher = self.main.get_cipher()
        run_id, token = self.main.create_task_run(
            self.main.DB_PATH,
            cipher,
            "confirm@example.com",
            "verify",
            password="secret",
        )
        self.main.update_task(self.main.DB_PATH, run_id, state="succeeded")
        request = self.main.TaskRequest(task_id=run_id)

        result = await self.main.save_account(request, f"Bearer {token}")
        self.assertEqual(result["status"], "saved")
        self.assertNotEqual(result["access_token"], token)
        self.assertIsNone(self.main.account_for_token(self.main.DB_PATH, token))
        self.assertEqual(
            self.main.account_for_token(self.main.DB_PATH, result["access_token"])["email"],
            "confirm@example.com",
        )

        with sqlite3.connect(self.main.DB_PATH) as conn:
            stored = conn.execute(
                "SELECT password, credential_ciphertext FROM accounts WHERE email=?",
                ("confirm@example.com",),
            ).fetchone()
        self.assertIsNone(stored[0])
        self.assertNotIn("secret", stored[1])

        with self.assertRaises(self.main.HTTPException) as reused:
            await self.main.save_account(request, f"Bearer {token}")
        self.assertEqual(reused.exception.status_code, 401)

    async def test_game_reporting_is_idempotent(self):
        log = self.main.GameLog(
            email="idempotent@example.com",
            game_title="Game A",
            image_filename="game-a.jpg",
        )
        authorization = "Bearer test-internal-token"
        first = await self.main.report_game(log, authorization)
        second = await self.main.report_game(log, authorization)

        self.assertEqual(first["status"], "recorded")
        self.assertEqual(second["status"], "skipped")
        with self.main.connect(self.main.DB_PATH) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM logs WHERE email=? AND game_title=?",
                (log.email, log.game_title),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    async def test_partial_promotion_response_does_not_replace_two_game_cycle(self):
        games = [
            {"id": "stable-a", "title": "Game A"},
            {"id": "stable-b", "title": "Game B"},
        ]
        cycle_id, _changed = self.main.set_active_promotion_cycle(self.main.DB_PATH, games)
        with patch.object(
            self.main,
            "_fetch_current_free_game_samples",
            return_value=[[games[0]]],
        ):
            await self.main.refresh_promotion_cycle()

        active = self.main.get_active_promotion_cycle(self.main.DB_PATH)
        self.assertEqual(active[0], cycle_id)
        self.assertEqual(len(active[1]), 2)

    async def test_game_reporting_preserves_worker_result(self):
        email = "authoritative-result@example.com"
        run_id, _ = self.main.create_task_run(
            self.main.DB_PATH,
            self.main.get_cipher(),
            email,
            "verify",
            password="secret",
        )
        self.main.record_game_result(self.main.DB_PATH, run_id, "Game A", "owned")

        result = await self.main.report_game(
            self.main.GameLog(
                email=email,
                game_title="Game A",
                image_filename="game-a.jpg",
                run_id=run_id,
            ),
            "Bearer test-internal-token",
        )

        self.assertEqual(result["status"], "recorded")
        with self.main.connect(self.main.DB_PATH) as conn:
            status = conn.execute(
                "SELECT status FROM task_game_results WHERE run_id=? AND game_title=?",
                (run_id, "Game A"),
            ).fetchone()[0]
        self.assertEqual(status, "owned")

    def test_legacy_status_endpoint_is_gone(self):
        from fastapi.testclient import TestClient

        response = TestClient(self.main.app).get("/api/status/user@example.com")
        self.assertEqual(response.status_code, 410)

    def test_missing_account_cannot_delete_profile(self):
        with self.assertRaises(KeyError):
            self.main._perform_physical_delete("missing@example.com")

    def test_database_profile_path_traversal_is_rejected_without_deleting_account(self):
        email = "traversal@example.com"
        with self.main.connect(self.main.DB_PATH) as conn:
            conn.execute(
                "INSERT INTO accounts(email, profile_id) VALUES (?, ?)",
                (email, "../../outside"),
            )
        with self.assertRaises(ValueError):
            self.main._perform_physical_delete(email)
        self.assertIsNotNone(self.main.get_account(self.main.DB_PATH, email))

    def test_symlink_profile_is_rejected_without_deleting_target_or_account(self):
        email = "symlink@example.com"
        profile_id = "46f83017-9d38-4b02-a0bc-fd79864e0675"
        outside = Path(self.tempdir.name) / "outside-profile"
        outside.mkdir(exist_ok=True)
        marker = outside / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        link = Path(self.main.USER_DATA_DIR) / profile_id
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks are not available")
        with self.main.connect(self.main.DB_PATH) as conn:
            conn.execute(
                "INSERT INTO accounts(email, profile_id) VALUES (?, ?)",
                (email, profile_id),
            )

        with self.assertRaises(ValueError):
            self.main._perform_physical_delete(email)
        self.assertTrue(marker.exists())
        self.assertIsNotNone(self.main.get_account(self.main.DB_PATH, email))

    def test_public_crawler_metadata_endpoints(self):
        from fastapi.testclient import TestClient

        client = TestClient(self.main.app)

        robots = client.get("/robots.txt")
        self.assertEqual(robots.status_code, 200)
        self.assertIn("Disallow: /api/", robots.text)
        self.assertIn("Sitemap: https://epic.910501.xyz/sitemap.xml", robots.text)

        sitemap = client.get("/sitemap.xml")
        self.assertEqual(sitemap.status_code, 200)
        self.assertIn("<loc>https://epic.910501.xyz/</loc>", sitemap.text)

        llms = client.get("/llms.txt")
        self.assertEqual(llms.status_code, 200)
        self.assertNotIn("Oracle-1", llms.text)
        self.assertNotIn("OPERATIONS.md", llms.text)
        self.assertNotIn("/opt/", llms.text)

        security_root = client.get("/security.txt")
        security_well_known = client.get("/.well-known/security.txt")
        self.assertEqual(security_root.status_code, 200)
        self.assertEqual(security_well_known.status_code, 200)
        self.assertIn("github.com/10000ge10000/epic-kiosk", security_root.text)

    def test_noisy_css_url_path_returns_no_content(self):
        from fastapi.testclient import TestClient

        client = TestClient(self.main.app)
        response = client.get("/url%28%27https%3A//fonts.googleapis.com/css2")
        self.assertEqual(response.status_code, 204)


if __name__ == "__main__":
    unittest.main()
