import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from app.secure_store import CredentialCipher, connect, ensure_schema
from scripts.migrate_secure_storage import main as migration_main, migrate
from scripts.rollback_secure_storage import main as rollback_main


class SecureMigrationTests(unittest.TestCase):
    def test_profile_moves_and_database_changes_roll_back_together(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            db_path = root / "kiosk.db"
            user_data = root / "user_data"
            user_data.mkdir()
            conflict_id = "46f83017-9d38-4b02-a0bc-fd79864e0675"
            for name in ("a@example.com", "b@example.com", conflict_id):
                (user_data / name).mkdir()
            ensure_schema(str(db_path))
            with connect(str(db_path)) as conn:
                conn.execute(
                    "INSERT INTO accounts(email, password) VALUES (?, ?)",
                    ("a@example.com", "secret-a"),
                )
                conn.execute(
                    "INSERT INTO accounts(email, password, profile_id) VALUES (?, ?, ?)",
                    ("b@example.com", "secret-b", conflict_id),
                )

            cipher = CredentialCipher([Fernet.generate_key().decode("ascii")])
            with self.assertRaises(FileExistsError):
                migrate(db_path, user_data, cipher)

            self.assertTrue((user_data / "a@example.com").is_dir())
            self.assertTrue((user_data / "b@example.com").is_dir())
            with connect(str(db_path)) as conn:
                rows = conn.execute(
                    "SELECT password, credential_ciphertext FROM accounts ORDER BY email"
                ).fetchall()
            self.assertEqual([row["password"] for row in rows], ["secret-a", "secret-b"])
            self.assertTrue(all(row["credential_ciphertext"] is None for row in rows))

    def test_plaintext_credentials_and_profile_are_migrated(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            db_path = root / "kiosk.db"
            user_data = root / "user_data"
            user_data.mkdir()
            legacy = user_data / "user@example.com"
            legacy.mkdir()
            (legacy / "cookies.sqlite").write_bytes(b"cookie")

            ensure_schema(str(db_path))
            with connect(str(db_path)) as conn:
                conn.execute(
                    "INSERT INTO accounts(email, password) VALUES (?, ?)",
                    ("user@example.com", "plain-secret"),
                )

            cipher = CredentialCipher([Fernet.generate_key().decode("ascii")])
            migrate(db_path, user_data, cipher)

            with connect(str(db_path)) as conn:
                row = conn.execute("SELECT * FROM accounts").fetchone()
            self.assertIsNone(row["password"])
            self.assertEqual(cipher.decrypt(row["credential_ciphertext"]), "plain-secret")
            self.assertFalse(legacy.exists())
            self.assertTrue((user_data / row["profile_id"] / "cookies.sqlite").exists())

            migrate(db_path, user_data, cipher)
            with connect(str(db_path)) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 1)

    def test_apply_backup_contains_unmodified_legacy_schema(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            db_path = root / "legacy.db"
            user_data = root / "user_data"
            user_data.mkdir()
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "CREATE TABLE accounts(email TEXT PRIMARY KEY, password TEXT)"
                )
                conn.execute(
                    "INSERT INTO accounts(email, password) VALUES (?, ?)",
                    ("legacy@example.com", "secret"),
                )
            legacy_profile = user_data / "legacy@example.com"
            legacy_profile.mkdir()

            key = Fernet.generate_key()
            key_file = root / "keys"
            key_file.write_bytes(key + b"\n")
            backup = root / "backup.enc"
            manifest = root / "migration-manifest.json"
            args = [
                "migrate_secure_storage.py",
                "--db",
                str(db_path),
                "--user-data",
                str(user_data),
                "--key-file",
                str(key_file),
                "--backup",
                str(backup),
                "--migration-manifest",
                str(manifest),
                "--owner-uid",
                str(os.getuid()),
                "--owner-gid",
                str(os.getgid()),
                "--apply",
            ]
            with patch.object(sys, "argv", args):
                self.assertEqual(migration_main(), 0)

            restored = root / "restored.db"
            restored.write_bytes(Fernet(key).decrypt(backup.read_bytes()))
            with sqlite3.connect(restored) as conn:
                legacy_columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(accounts)")
                }
            self.assertEqual(legacy_columns, {"email", "password"})

            with sqlite3.connect(db_path) as conn:
                migrated_columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(accounts)")
                }
            self.assertIn("credential_ciphertext", migrated_columns)
            self.assertFalse(legacy_profile.exists())

            rollback_args = [
                "rollback_secure_storage.py",
                "--db",
                str(db_path),
                "--user-data",
                str(user_data),
                "--key-file",
                str(key_file),
                "--backup",
                str(backup),
                "--manifest",
                str(manifest),
                "--current-backup",
                str(root / "post-migration.enc"),
                "--owner-uid",
                str(os.getuid()),
                "--owner-gid",
                str(os.getgid()),
                "--maintenance-confirmed",
                "--apply",
            ]
            with patch.object(sys, "argv", rollback_args):
                self.assertEqual(rollback_main(), 0)

            self.assertTrue(legacy_profile.is_dir())
            with sqlite3.connect(db_path) as conn:
                rollback_columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(accounts)")
                }
            self.assertEqual(rollback_columns, {"email", "password"})


if __name__ == "__main__":
    unittest.main()
