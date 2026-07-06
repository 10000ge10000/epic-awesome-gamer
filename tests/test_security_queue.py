import os
import sqlite3
import tempfile
import unittest
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


class SecurityQueueTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tempdir.name
        os.environ["INTERNAL_API_TOKEN"] = "test-internal-token"

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

    def test_sensitive_path_is_hidden(self):
        from fastapi.testclient import TestClient

        client = TestClient(self.main.app)
        response = client.get("/.env")
        self.assertEqual(response.status_code, 404)

    def test_enqueue_is_atomic_per_account(self):
        task = {"email": "user@example.com", "password": "secret", "mode": "verify"}
        self.assertTrue(self.main._enqueue_task(task))
        self.assertFalse(self.main._enqueue_task(task))
        self.assertEqual(len(self.fake_redis.queue), 1)

    async def test_confirmation_token_is_one_time(self):
        email = "confirm@example.com"
        self.fake_redis.setex(f"confirm_token:{email}", 60, "one-time-token")
        account = self.main.ConfirmAccount(
            email=email,
            password="secret",
            confirmation_token="one-time-token",
        )

        result = await self.main.save_account(account)
        self.assertEqual(result["status"], "saved")
        self.assertIsNone(self.fake_redis.get(f"confirm_token:{email}"))

        with sqlite3.connect(self.main.DB_PATH) as conn:
            stored = conn.execute(
                "SELECT password FROM accounts WHERE email=?",
                (email,),
            ).fetchone()
        self.assertEqual(stored, ("secret",))

        with self.assertRaises(self.main.HTTPException) as reused:
            await self.main.save_account(account)
        self.assertEqual(reused.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
