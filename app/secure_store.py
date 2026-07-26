from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from contextlib import suppress
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}$")
TERMINAL_TASK_STATES = {"succeeded", "failed", "deferred", "manual_required"}
SUCCESSFUL_GAME_STATES = {"claimed", "owned"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    if len(email) > 254 or not EMAIL_RE.fullmatch(email):
        raise ValueError("Invalid email address")
    return email


def account_ref(email: str) -> str:
    return hashlib.sha256(normalize_email(email).encode("utf-8")).hexdigest()[:12]


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def read_secret(env_name: str, file_env_name: str) -> str:
    file_name = os.getenv(file_env_name, "").strip()
    if file_name:
        return Path(file_name).read_text(encoding="utf-8").strip()
    return os.getenv(env_name, "").strip()


class CredentialError(RuntimeError):
    pass


class CredentialCipher:
    def __init__(self, keys: list[str]):
        if not keys:
            raise CredentialError("No credential encryption key is configured")
        try:
            fernets = [Fernet(key.encode("ascii")) for key in keys]
        except (ValueError, TypeError) as exc:
            raise CredentialError("Credential encryption key is invalid") from exc
        self._cipher = MultiFernet(fernets)

    @classmethod
    def from_environment(cls) -> "CredentialCipher":
        raw = read_secret("EPIC_CREDENTIAL_KEYS", "EPIC_CREDENTIAL_KEYS_FILE")
        keys = [line.strip() for line in raw.splitlines() if line.strip()]
        return cls(keys)

    def encrypt(self, value: str) -> str:
        if not value:
            raise CredentialError("Credential must not be empty")
        return self._cipher.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self._cipher.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise CredentialError("Credential cannot be decrypted") from exc

    def rotate(self, value: str) -> str:
        try:
            return self._cipher.rotate(value.encode("ascii")).decode("ascii")
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise CredentialError("Credential cannot be rotated") from exc


_WAL_READY = False


def connect(db_path: str) -> sqlite3.Connection:
    global _WAL_READY
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    if not _WAL_READY:
        # 默认的 delete 模式下写事务会拿 EXCLUSIVE 锁、阻塞所有读者，
        # 而 web 与 worker 是两个容器并发读写同一个库文件。
        # WAL 是持久化在库文件头里的，设置一次即对两端永久生效。
        with suppress(sqlite3.Error):
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        _WAL_READY = True
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def ensure_schema(db_path: str) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                email TEXT PRIMARY KEY,
                password TEXT,
                auth_method TEXT DEFAULT 'password',
                login_updated_at TEXT,
                login_expires_hint TEXT
            )
            """
        )
        account_columns = _columns(conn, "accounts")
        additions = {
            "credential_ciphertext": "TEXT",
            "credential_version": "INTEGER NOT NULL DEFAULT 1",
            "profile_id": "TEXT",
            "access_token_hash": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in account_columns:
                conn.execute(f'ALTER TABLE accounts ADD COLUMN "{name}" {declaration}')

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT,
                game_title TEXT,
                image_url TEXT,
                claim_time TEXT
            );
            CREATE TABLE IF NOT EXISTS task_runs (
                run_id TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                mode TEXT NOT NULL CHECK(mode IN ('verify', 'claim')),
                state TEXT NOT NULL,
                access_token_hash TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 0,
                parent_run_id TEXT,
                retry_data TEXT NOT NULL DEFAULT '{}',
                error_type TEXT,
                status_message TEXT,
                hint TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(parent_run_id) REFERENCES task_runs(run_id)
            );
            CREATE INDEX IF NOT EXISTS idx_task_runs_email_created
                ON task_runs(email, created_at DESC);

            CREATE TABLE IF NOT EXISTS pending_credentials (
                run_id TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                credential_ciphertext TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES task_runs(run_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS task_game_results (
                run_id TEXT NOT NULL,
                game_title TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(run_id, game_title),
                FOREIGN KEY(run_id) REFERENCES task_runs(run_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS promotion_state (
                singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
                cycle_id TEXT NOT NULL,
                games_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS claim_cycle_assignments (
                email TEXT NOT NULL,
                cycle_id TEXT NOT NULL,
                run_id TEXT NOT NULL UNIQUE,
                scheduled_for INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(email, cycle_id),
                FOREIGN KEY(email) REFERENCES accounts(email) ON DELETE CASCADE,
                FOREIGN KEY(run_id) REFERENCES task_runs(run_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_cycle_assignments_schedule
                ON claim_cycle_assignments(cycle_id, scheduled_for);

            CREATE TABLE IF NOT EXISTS claim_cycle_completions (
                email TEXT NOT NULL,
                cycle_id TEXT NOT NULL,
                expected_games_json TEXT NOT NULL,
                run_id TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                PRIMARY KEY(email, cycle_id),
                FOREIGN KEY(email) REFERENCES accounts(email) ON DELETE CASCADE,
                FOREIGN KEY(run_id) REFERENCES task_runs(run_id)
            );
            """
        )


@dataclass(frozen=True)
class TaskContext:
    run_id: str
    email: str
    password: str
    profile_id: str
    mode: str
    attempt: int
    retry_data: dict[str, object]

    @property
    def account_ref(self) -> str:
        return account_ref(self.email)


def get_account(db_path: str, email: str) -> sqlite3.Row | None:
    with connect(db_path) as conn:
        return conn.execute("SELECT * FROM accounts WHERE email=?", (normalize_email(email),)).fetchone()


def verify_account_password(
    db_path: str, cipher: CredentialCipher, email: str, password: str
) -> bool:
    row = get_account(db_path, email)
    if not row or not row["credential_ciphertext"]:
        return False
    actual = cipher.decrypt(row["credential_ciphertext"])
    return secrets.compare_digest(actual, password)


def create_task_run(
    db_path: str,
    cipher: CredentialCipher,
    email: str,
    mode: str,
    password: str | None = None,
    parent_run_id: str | None = None,
    attempt: int = 0,
) -> tuple[str, str]:
    email = normalize_email(email)
    if mode not in {"verify", "claim"}:
        raise ValueError("Unsupported task mode")
    run_id = str(uuid.uuid4())
    access_token = secrets.token_urlsafe(32)
    now = utc_now()
    with connect(db_path) as conn:
        conn.execute("DELETE FROM pending_credentials WHERE expires_at <= ?", (now,))
        account = conn.execute(
            "SELECT email, profile_id FROM accounts WHERE email=?", (email,)
        ).fetchone()
        if mode == "claim" and not account:
            raise ValueError("Account does not exist")
        conn.execute(
            """
            INSERT INTO task_runs (
                run_id, email, mode, state, access_token_hash, attempt,
                parent_run_id, created_at, updated_at
            ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?)
            """,
            (run_id, email, mode, hash_token(access_token), attempt, parent_run_id, now, now),
        )
        if password is not None:
            expires_at = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(timespec="seconds")
            conn.execute(
                """
                INSERT INTO pending_credentials(
                    run_id, email, credential_ciphertext, profile_id, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    email,
                    cipher.encrypt(password),
                    account["profile_id"] if account and account["profile_id"] else str(uuid.uuid4()),
                    expires_at,
                ),
            )
    return run_id, access_token


def load_task_context(db_path: str, cipher: CredentialCipher, run_id: str) -> TaskContext:
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT tr.*, pc.credential_ciphertext AS pending_ciphertext,
                   a.credential_ciphertext AS account_ciphertext,
                   COALESCE(pc.profile_id, a.profile_id) AS resolved_profile_id
            FROM task_runs tr
            LEFT JOIN pending_credentials pc ON pc.run_id=tr.run_id
            LEFT JOIN accounts a ON a.email=tr.email
            WHERE tr.run_id=?
            """,
            (run_id,),
        ).fetchone()
        if not row:
            raise KeyError("Task does not exist")
        pending = conn.execute(
            "SELECT expires_at FROM pending_credentials WHERE run_id=?", (run_id,)
        ).fetchone()
        if pending and pending["expires_at"] <= utc_now():
            conn.execute("DELETE FROM pending_credentials WHERE run_id=?", (run_id,))
            conn.commit()
            raise CredentialError("Task credential has expired")
        ciphertext = row["pending_ciphertext"] or row["account_ciphertext"]
        if not ciphertext:
            raise CredentialError("Task credential is unavailable")
        profile_id = row["resolved_profile_id"] or str(uuid.uuid4())
        retry_data = json.loads(row["retry_data"] or "{}")
    return TaskContext(
        run_id=row["run_id"],
        email=row["email"],
        password=cipher.decrypt(ciphertext),
        profile_id=profile_id,
        mode=row["mode"],
        attempt=row["attempt"],
        retry_data=retry_data,
    )


def update_task(
    db_path: str,
    run_id: str,
    *,
    state: str | None = None,
    error_type: str | None = None,
    status_message: str | None = None,
    hint: str | None = None,
    retry_data: dict[str, object] | None = None,
) -> None:
    fields: list[str] = ["updated_at=?"]
    values: list[object] = [utc_now()]
    for name, value in (
        ("state", state),
        ("error_type", error_type),
        ("status_message", status_message),
        ("hint", hint),
    ):
        if value is not None:
            fields.append(f"{name}=?")
            values.append(value)
    if retry_data is not None:
        fields.append("retry_data=?")
        values.append(json.dumps(retry_data, ensure_ascii=True, sort_keys=True))
    if state == "running":
        fields.append("started_at=?")
        values.append(utc_now())
        fields.append("finished_at=NULL")
    elif state == "queued":
        fields.append("finished_at=NULL")
    if state == "succeeded":
        if error_type is None:
            fields.append("error_type=NULL")
        if hint is None:
            fields.append("hint=NULL")
    if state in TERMINAL_TASK_STATES:
        fields.append("finished_at=?")
        values.append(utc_now())
    values.append(run_id)
    with connect(db_path) as conn:
        cursor = conn.execute(f"UPDATE task_runs SET {', '.join(fields)} WHERE run_id=?", values)
        if cursor.rowcount != 1:
            raise KeyError("Task does not exist")
        if state in {"failed", "manual_required"}:
            conn.execute("DELETE FROM pending_credentials WHERE run_id=?", (run_id,))


def task_for_token(db_path: str, run_id: str, token: str) -> sqlite3.Row | None:
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM task_runs WHERE run_id=?", (run_id,)).fetchone()
    if not row or not secrets.compare_digest(row["access_token_hash"], hash_token(token)):
        return None
    return row


def issue_account_token(db_path: str, email: str) -> str:
    token = secrets.token_urlsafe(32)
    with connect(db_path) as conn:
        cursor = conn.execute(
            "UPDATE accounts SET access_token_hash=? WHERE email=?",
            (hash_token(token), normalize_email(email)),
        )
        if cursor.rowcount != 1:
            raise KeyError("Account does not exist")
    return token


def account_for_token(db_path: str, token: str) -> sqlite3.Row | None:
    with connect(db_path) as conn:
        return conn.execute(
            "SELECT * FROM accounts WHERE access_token_hash=?", (hash_token(token),)
        ).fetchone()


def confirm_pending_account(db_path: str, run_id: str, token: str) -> str:
    row = task_for_token(db_path, run_id, token)
    if not row or row["mode"] != "verify" or row["state"] != "succeeded":
        raise PermissionError("Task cannot be confirmed")
    profile_id = str(uuid.uuid4())
    with connect(db_path) as conn:
        pending = conn.execute(
            "SELECT * FROM pending_credentials WHERE run_id=?", (run_id,)
        ).fetchone()
        if not pending:
            raise KeyError("Pending credential does not exist")
        if pending["expires_at"] <= utc_now():
            conn.execute("DELETE FROM pending_credentials WHERE run_id=?", (run_id,))
            raise PermissionError("Pending credential has expired")
        profile_id = pending["profile_id"]
        conn.execute(
            """
            INSERT INTO accounts (
                email, password, auth_method, credential_ciphertext,
                credential_version, profile_id
            ) VALUES (?, NULL, 'password', ?, 1, ?)
            ON CONFLICT(email) DO UPDATE SET
                password=NULL,
                credential_ciphertext=excluded.credential_ciphertext,
                credential_version=excluded.credential_version,
                profile_id=COALESCE(accounts.profile_id, excluded.profile_id)
            """,
            (
                row["email"],
                pending["credential_ciphertext"],
                profile_id,
            ),
        )
        conn.execute("DELETE FROM pending_credentials WHERE run_id=?", (run_id,))
    return row["email"]


def record_game_result(
    db_path: str,
    run_id: str,
    game_title: str,
    status: str,
    detail: str | None = None,
    *,
    overwrite: bool = True,
) -> None:
    if status not in {"claimed", "owned", "failed", "deferred", "unconfirmed"}:
        raise ValueError("Unsupported game result")
    conflict_clause = (
        """ON CONFLICT(run_id, game_title) DO UPDATE SET
                status=excluded.status, detail=excluded.detail, updated_at=excluded.updated_at"""
        if overwrite
        else "ON CONFLICT(run_id, game_title) DO NOTHING"
    )
    with connect(db_path) as conn:
        conn.execute(
            f"""
            INSERT INTO task_game_results(run_id, game_title, status, detail, updated_at)
            VALUES (?, ?, ?, ?, ?)
            {conflict_clause}
            """,
            (run_id, game_title, status, detail, utc_now()),
        )


def canonicalize_promotion_games(games: list[dict[str, str]]) -> list[dict[str, str]]:
    canonical: dict[str, dict[str, str]] = {}
    for game in games:
        game_id = str(game.get("id", "")).strip()
        title = str(game.get("title", "")).strip()
        if not game_id or not title:
            raise ValueError("Promotion game id and title are required")
        normalized_id = game_id.casefold()
        existing = canonical.get(normalized_id)
        if existing and existing["title"].casefold() != title.casefold():
            raise ValueError("Promotion game id is duplicated with a different title")
        canonical[normalized_id] = {"id": game_id, "title": title}
    if not canonical:
        raise ValueError("At least one promotion game is required")
    return [canonical[key] for key in sorted(canonical)]


def promotion_cycle_id(games: list[dict[str, str]]) -> str:
    canonical = canonicalize_promotion_games(games)
    identity = [game["id"].casefold() for game in canonical]
    payload = json.dumps(identity, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_active_promotion_cycle(db_path: str) -> tuple[str, list[dict[str, str]]] | None:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT cycle_id, games_json FROM promotion_state WHERE singleton_id=1"
        ).fetchone()
    if not row:
        return None
    return row["cycle_id"], canonicalize_promotion_games(json.loads(row["games_json"]))


def set_active_promotion_cycle(
    db_path: str, games: list[dict[str, str]]
) -> tuple[str, bool]:
    canonical = canonicalize_promotion_games(games)
    cycle_id = promotion_cycle_id(canonical)
    games_json = json.dumps(canonical, ensure_ascii=True, sort_keys=True)
    now = utc_now()
    with connect(db_path) as conn:
        previous = conn.execute(
            "SELECT cycle_id FROM promotion_state WHERE singleton_id=1"
        ).fetchone()
        changed = not previous or previous["cycle_id"] != cycle_id
        conn.execute(
            """
            INSERT INTO promotion_state(singleton_id, cycle_id, games_json, updated_at)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(singleton_id) DO UPDATE SET
                cycle_id=excluded.cycle_id,
                games_json=excluded.games_json,
                updated_at=excluded.updated_at
            """,
            (cycle_id, games_json, now),
        )
    return cycle_id, changed


def create_cycle_assignments(
    db_path: str,
    cycle_id: str,
    expected_games: list[dict[str, str]],
    *,
    start_at: int,
    batch_size: int,
    batch_interval_seconds: int,
) -> list[tuple[str, int]]:
    if batch_size < 1 or batch_interval_seconds < 0:
        raise ValueError("Invalid claim batch configuration")
    canonical = canonicalize_promotion_games(expected_games)
    if promotion_cycle_id(canonical) != cycle_id:
        raise ValueError("Promotion cycle id does not match expected games")
    retry_data = json.dumps(
        {
            "cycle_id": cycle_id,
            "expected_games": canonical,
            "target_games": [game["title"] for game in canonical],
        },
        ensure_ascii=True,
        sort_keys=True,
    )
    created: list[tuple[str, int]] = []
    now = utc_now()
    with connect(db_path) as conn:
        active = conn.execute(
            "SELECT cycle_id FROM promotion_state WHERE singleton_id=1"
        ).fetchone()
        if not active or active["cycle_id"] != cycle_id:
            raise RuntimeError("Promotion cycle changed before scheduling")
        rows = conn.execute(
            """
            SELECT a.email
            FROM accounts a
            LEFT JOIN claim_cycle_completions c
              ON c.email=a.email AND c.cycle_id=?
            LEFT JOIN claim_cycle_assignments s
              ON s.email=a.email AND s.cycle_id=?
            WHERE a.credential_ciphertext IS NOT NULL
              AND c.email IS NULL AND s.email IS NULL
            ORDER BY a.email
            """,
            (cycle_id, cycle_id),
        ).fetchall()
        for index, row in enumerate(rows):
            run_id = str(uuid.uuid4())
            scheduled_for = int(start_at + (index // batch_size) * batch_interval_seconds)
            conn.execute(
                """
                INSERT INTO task_runs(
                    run_id, email, mode, state, access_token_hash, retry_data,
                    created_at, updated_at
                ) VALUES (?, ?, 'claim', 'scheduled', ?, ?, ?, ?)
                """,
                (run_id, row["email"], hash_token(secrets.token_urlsafe(32)), retry_data, now, now),
            )
            conn.execute(
                """
                INSERT INTO claim_cycle_assignments(
                    email, cycle_id, run_id, scheduled_for, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (row["email"], cycle_id, run_id, scheduled_for, now),
            )
            created.append((run_id, scheduled_for))
    return created


def scheduled_cycle_runs(db_path: str, cycle_id: str) -> list[tuple[str, int]]:
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT a.run_id, a.scheduled_for
            FROM claim_cycle_assignments a
            JOIN task_runs tr ON tr.run_id=a.run_id
            WHERE a.cycle_id=? AND tr.state='scheduled'
            ORDER BY a.scheduled_for, a.email
            """,
            (cycle_id,),
        ).fetchall()
    return [(row["run_id"], int(row["scheduled_for"])) for row in rows]


def cancel_obsolete_scheduled_runs(db_path: str, active_cycle_id: str) -> list[str]:
    now = utc_now()
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT tr.run_id
            FROM task_runs tr
            JOIN claim_cycle_assignments a ON a.run_id=tr.run_id
            WHERE tr.state='scheduled' AND a.cycle_id<>?
            """,
            (active_cycle_id,),
        ).fetchall()
        run_ids = [row["run_id"] for row in rows]
        if run_ids:
            placeholders = ",".join("?" for _ in run_ids)
            conn.execute(
                f"""
                UPDATE task_runs
                SET state='failed', error_type='cycle_obsolete',
                    status_message='周免周期已变化，旧任务已取消',
                    finished_at=?, updated_at=?
                WHERE run_id IN ({placeholders})
                """,
                (now, now, *run_ids),
            )
    return run_ids


def cycle_run_dispatch_status(db_path: str, run_id: str) -> tuple[str, str]:
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT tr.email, tr.state, a.cycle_id,
                   (SELECT cycle_id FROM promotion_state WHERE singleton_id=1) AS active_cycle_id
            FROM task_runs tr
            JOIN claim_cycle_assignments a ON a.run_id=tr.run_id
            WHERE tr.run_id=?
            """,
            (run_id,),
        ).fetchone()
    if not row:
        raise KeyError("Scheduled cycle task does not exist")
    if row["state"] != "scheduled":
        raise RuntimeError("Scheduled cycle task is no longer dispatchable")
    if row["cycle_id"] != row["active_cycle_id"]:
        raise RuntimeError("Promotion cycle is obsolete")
    return row["email"], row["cycle_id"]


def task_cycle_is_active(db_path: str, retry_data: dict[str, object]) -> bool:
    cycle_id = str(retry_data.get("cycle_id", "")).strip()
    if not cycle_id:
        return True
    active = get_active_promotion_cycle(db_path)
    return bool(active and active[0] == cycle_id)


def mark_cycle_complete_if_ready(db_path: str, run_id: str) -> bool:
    with connect(db_path) as conn:
        task = conn.execute(
            "SELECT email, retry_data FROM task_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if not task:
            raise KeyError("Task does not exist")
        retry_data = json.loads(task["retry_data"] or "{}")
        cycle_id = str(retry_data.get("cycle_id", "")).strip()
        if not cycle_id:
            return True
        expected = canonicalize_promotion_games(retry_data.get("expected_games", []))
        if promotion_cycle_id(expected) != cycle_id:
            return False
        active = conn.execute(
            "SELECT cycle_id FROM promotion_state WHERE singleton_id=1"
        ).fetchone()
        if not active or active["cycle_id"] != cycle_id:
            return False
        results = {
            row["game_title"].strip().casefold(): row["status"]
            for row in conn.execute(
                "SELECT game_title, status FROM task_game_results WHERE run_id=?", (run_id,)
            )
        }
        if any(
            results.get(game["title"].casefold()) not in SUCCESSFUL_GAME_STATES
            for game in expected
        ):
            return False
        expected_json = json.dumps(expected, ensure_ascii=True, sort_keys=True)
        conn.execute(
            """
            INSERT INTO claim_cycle_completions(
                email, cycle_id, expected_games_json, run_id, completed_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(email, cycle_id) DO NOTHING
            """,
            (task["email"], cycle_id, expected_json, run_id, utc_now()),
        )
    return True


def delete_account_record(db_path: str, email: str) -> str:
    email = normalize_email(email)
    with connect(db_path) as conn:
        row = conn.execute("SELECT profile_id FROM accounts WHERE email=?", (email,)).fetchone()
        if not row:
            raise KeyError("Account does not exist")
        conn.execute("DELETE FROM accounts WHERE email=?", (email,))
    return row["profile_id"] or ""


def safe_profile_path(user_data_dir: str, profile_id: str) -> Path:
    try:
        uuid.UUID(profile_id)
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid profile id") from exc
    base = Path(user_data_dir).resolve()
    candidate = base / profile_id
    if candidate.is_symlink():
        raise ValueError("Profile path must not be a symlink")
    target = candidate.resolve()
    if target.parent != base:
        raise ValueError("Profile path escapes user data directory")
    return candidate
