import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet
from app.utils import cleanup_debug_artifacts

from app.secure_store import (
    CredentialCipher,
    account_for_token,
    confirm_pending_account,
    create_task_run,
    ensure_schema,
    issue_account_token,
    load_task_context,
    normalize_email,
    safe_profile_path,
    task_for_token,
    update_task,
)


class SecureStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tempdir.name) / "kiosk.db")
        self.key1 = Fernet.generate_key().decode("ascii")
        self.key2 = Fernet.generate_key().decode("ascii")
        self.cipher = CredentialCipher([self.key1])
        ensure_schema(self.db_path)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_ciphertext_round_trip_and_rotation(self):
        encrypted = self.cipher.encrypt("secret-value")
        self.assertNotIn("secret-value", encrypted)
        rotated = CredentialCipher([self.key2, self.key1])
        self.assertEqual(rotated.decrypt(encrypted), "secret-value")
        self.assertEqual(rotated.decrypt(rotated.encrypt("new-value")), "new-value")
        rotated_ciphertext = rotated.rotate(encrypted)
        self.assertEqual(
            Fernet(self.key2.encode("ascii")).decrypt(rotated_ciphertext.encode("ascii")),
            b"secret-value",
        )

    def test_task_context_uses_encrypted_pending_credential(self):
        run_id, token = create_task_run(
            self.db_path,
            self.cipher,
            "User@Example.com",
            "verify",
            password="secret",
        )
        context = load_task_context(self.db_path, self.cipher, run_id)
        self.assertEqual(context.email, "user@example.com")
        self.assertEqual(context.password, "secret")
        self.assertIsNotNone(task_for_token(self.db_path, run_id, token))
        self.assertIsNone(task_for_token(self.db_path, run_id, "wrong"))

        with sqlite3.connect(self.db_path) as conn:
            pending = conn.execute(
                "SELECT credential_ciphertext FROM pending_credentials WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        self.assertNotIn("secret", pending)

    def test_confirmation_moves_ciphertext_and_issues_account_session(self):
        run_id, token = create_task_run(
            self.db_path, self.cipher, "confirm@example.com", "verify", password="secret"
        )
        update_task(self.db_path, run_id, state="succeeded")
        self.assertEqual(confirm_pending_account(self.db_path, run_id, token), "confirm@example.com")

        session = issue_account_token(self.db_path, "confirm@example.com")
        account = account_for_token(self.db_path, session)
        self.assertEqual(account["email"], "confirm@example.com")
        self.assertIsNone(account["password"])
        self.assertIsNone(account_for_token(self.db_path, token))

    def test_expired_pending_credential_is_deleted_and_cannot_be_loaded(self):
        run_id, _ = create_task_run(
            self.db_path, self.cipher, "expired@example.com", "verify", password="secret"
        )
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(
            timespec="seconds"
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE pending_credentials SET expires_at=? WHERE run_id=?", (expired, run_id)
            )

        with self.assertRaises(RuntimeError):
            load_task_context(self.db_path, self.cipher, run_id)
        with sqlite3.connect(self.db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM pending_credentials WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            self.assertEqual(count, 0)

    def test_success_clears_previous_error_and_running_refreshes_attempt_start(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO accounts(email, credential_ciphertext, profile_id)
                VALUES ('retry@example.com', 'ciphertext', 'profile-id')
                """
            )
        run_id, _ = create_task_run(
            self.db_path, self.cipher, "retry@example.com", "claim"
        )
        update_task(
            self.db_path,
            run_id,
            state="failed",
            error_type="session_invalid",
            hint="old hint",
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE task_runs SET started_at='2000-01-01T00:00:00+00:00' WHERE run_id=?",
                (run_id,),
            )
        update_task(self.db_path, run_id, state="running")
        update_task(self.db_path, run_id, state="succeeded")
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT state, error_type, hint, started_at FROM task_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
        self.assertEqual(row[0], "succeeded")
        self.assertIsNone(row[1])
        self.assertIsNone(row[2])
        self.assertNotEqual(row[3], "2000-01-01T00:00:00+00:00")

    def test_email_and_profile_path_validation(self):
        self.assertEqual(normalize_email(" User@Example.COM "), "user@example.com")
        for bad in ("../data", "/tmp/x", "missing-at.example.com"):
            with self.assertRaises(ValueError):
                normalize_email(bad)
        with self.assertRaises(ValueError):
            safe_profile_path(self.tempdir.name, "../../etc")

        target = Path(self.tempdir.name) / "target"
        target.mkdir()
        profile_id = "46f83017-9d38-4b02-a0bc-fd79864e0675"
        symlink = Path(self.tempdir.name) / profile_id
        try:
            symlink.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks are not available")
        with self.assertRaises(ValueError):
            safe_profile_path(self.tempdir.name, profile_id)

    def test_debug_artifact_retention_only_removes_expired_debug_files(self):
        root = Path(self.tempdir.name) / "runtime"
        debug = root / "login_debug"
        debug.mkdir(parents=True)
        expired = debug / "expired.html"
        current = debug / "current.html"
        unrelated = root / "promotions.json"
        expired.write_text("old", encoding="utf-8")
        current.write_text("new", encoding="utf-8")
        unrelated.write_text("{}", encoding="utf-8")
        old = time.time() - 8 * 86400
        os.utime(expired, (old, old))

        self.assertEqual(cleanup_debug_artifacts(root, retention_days=7), 1)
        self.assertFalse(expired.exists())
        self.assertTrue(current.exists())
        self.assertTrue(unrelated.exists())


if __name__ == "__main__":
    unittest.main()
