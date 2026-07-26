#!/usr/bin/env python3
from __future__ import annotations

import argparse
import errno
import json
import os
import shutil
from contextlib import closing
import sqlite3
import sys
import uuid
from pathlib import Path

from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.secure_store import CredentialCipher, connect, ensure_schema, normalize_email


def checkpoint_wal(db_path: Path) -> None:
    """把 WAL 里的内容折叠回主库文件，并确认没有别的连接在写。

    app/secure_store.connect() 现在统一开启 WAL，最近的写入可能只存在于 -wal
    文件里。而本脚本是用 db_path.read_bytes() 整文件读取来做加密备份的 ——
    不先 checkpoint 就会备出一个缺最新数据的库，这是灾难恢复路径上的静默数据丢失。

    此前这里用的是"只要 -wal/-shm 文件存在就拒绝运行"。开启 WAL 之后这个判据不再
    成立：SQLite 只在最后一个连接干净关闭时才删除这两个文件，于是检查会随着当时
    是否恰好有连接打开而间歇性触发，比稳定失败更难排查。改为主动 checkpoint，
    只有真的拿不到锁（说明还有活跃写者）才报错。
    """
    if not db_path.exists():
        return
    with closing(sqlite3.connect(str(db_path), timeout=30)) as conn:
        busy, _log, _checkpointed = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if busy:
        raise RuntimeError(
            "WAL checkpoint blocked by another connection; stop services and retry"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate Epic Kiosk credentials and profiles")
    parser.add_argument("--db", required=True)
    parser.add_argument("--user-data", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--backup", help="Required encrypted backup path with --apply")
    parser.add_argument("--orphan-report")
    parser.add_argument("--quarantine-dir")
    parser.add_argument("--migration-manifest")
    parser.add_argument("--owner-uid", type=int, default=1002)
    parser.add_argument("--owner-gid", type=int, default=1002)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def load_keys(path: Path) -> list[str]:
    keys = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    CredentialCipher(keys)
    return keys


def encrypted_backup(db_path: Path, backup_path: Path, primary_key: str) -> None:
    backup_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if backup_path.exists():
        raise FileExistsError(f"Backup already exists: {backup_path}")
    payload = Fernet(primary_key.encode("ascii")).encrypt(db_path.read_bytes())
    with backup_path.open("xb") as handle:
        handle.write(payload)
    os.chmod(backup_path, 0o600)


def collect_plan(db_path: Path, user_data: Path) -> tuple[list[sqlite3.Row], list[Path]]:
    with connect(str(db_path)) as conn:
        rows = conn.execute("SELECT * FROM accounts ORDER BY email").fetchall()
    columns = set(rows[0].keys()) if rows else set()
    known = {row["email"] for row in rows}
    if "profile_id" in columns:
        known.update(row["profile_id"] for row in rows if row["profile_id"])
    orphaned = [
        entry
        for entry in user_data.iterdir()
        if (entry.is_dir() or entry.is_symlink()) and entry.name not in known
    ]
    return rows, orphaned


def quarantine_orphans(orphaned: list[Path], quarantine_dir: Path) -> list[tuple[Path, Path]]:
    quarantine_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    moved: list[tuple[Path, Path]] = []
    try:
        for source in orphaned:
            target = quarantine_dir / source.name
            if target.exists() or target.is_symlink():
                raise FileExistsError(f"Quarantine target already exists: {target}")
            try:
                os.replace(source, target)
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                shutil.move(str(source), str(target))
            moved.append((target, source))
    except Exception:
        restore_quarantine(moved)
        raise
    return moved


def restore_quarantine(moved: list[tuple[Path, Path]]) -> None:
    for target, source in reversed(moved):
        if (target.exists() or target.is_symlink()) and not source.exists():
            try:
                os.replace(target, source)
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                shutil.move(str(target), str(source))


def plan_profile_mappings(db_path: Path) -> list[dict[str, str]]:
    with connect(str(db_path)) as conn:
        rows = conn.execute("SELECT email, profile_id FROM accounts ORDER BY email").fetchall()
    return [
        {
            "email": normalize_email(row["email"]),
            "profile_id": row["profile_id"] or str(uuid.uuid4()),
        }
        for row in rows
    ]


def migrate(
    db_path: Path,
    user_data: Path,
    cipher: CredentialCipher,
    mappings: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    moved: list[tuple[Path, Path]] = []
    mappings = mappings or plan_profile_mappings(db_path)
    profile_ids = {item["email"]: item["profile_id"] for item in mappings}
    conn = connect(str(db_path))
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute("SELECT * FROM accounts ORDER BY email").fetchall()
        for row in rows:
            email = normalize_email(row["email"])
            ciphertext = row["credential_ciphertext"]
            if not ciphertext:
                if not row["password"]:
                    raise RuntimeError("Account has neither plaintext nor encrypted credential")
                ciphertext = cipher.encrypt(row["password"])
            else:
                ciphertext = cipher.rotate(ciphertext)
            if not cipher.decrypt(ciphertext):
                raise RuntimeError("Credential verification failed")

            profile_id = profile_ids[email]
            old_path = user_data / email
            new_path = user_data / profile_id
            if old_path.exists() and old_path != new_path:
                if new_path.exists():
                    raise FileExistsError(f"Profile target already exists: {profile_id}")
                os.replace(old_path, new_path)
                moved.append((new_path, old_path))

            conn.execute(
                """
                UPDATE accounts
                SET password=NULL, credential_ciphertext=?, credential_version=1, profile_id=?
                WHERE email=?
                """,
                (ciphertext, profile_id, email),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        for new_path, old_path in reversed(moved):
            if new_path.exists() and not old_path.exists():
                os.replace(new_path, old_path)
        raise
    finally:
        conn.close()
    return mappings


def write_manifest(
    path: Path,
    db_path: Path,
    mappings: list[dict[str, str]],
    orphaned: list[Path],
    quarantine_dir: Path | None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "version": 1,
        "database": str(db_path),
        "profiles": mappings,
        "orphans": [entry.name for entry in orphaned],
        "quarantine_dir": str(quarantine_dir) if quarantine_dir else None,
    }
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
        handle.write("\n")
    os.chmod(path, 0o600)


def harden_permissions(
    db_path: Path, user_data: Path, owner_uid: int, owner_gid: int
) -> None:
    os.chown(db_path, owner_uid, owner_gid)
    os.chmod(db_path, 0o600)
    os.chown(user_data, owner_uid, owner_gid)
    os.chmod(user_data, 0o700)
    for path in user_data.rglob("*"):
        if path.is_symlink():
            continue
        os.chown(path, owner_uid, owner_gid)
        os.chmod(path, 0o700 if path.is_dir() else 0o600)


def main() -> int:
    args = parse_args()
    db_path = Path(args.db).resolve()
    user_data = Path(args.user_data).resolve()
    key_file = Path(args.key_file).resolve()
    if not db_path.is_file():
        raise SystemExit(f"Database does not exist: {db_path}")
    if not user_data.is_dir():
        raise SystemExit(f"User data directory does not exist: {user_data}")
    keys = load_keys(key_file)

    rows, orphaned = collect_plan(db_path, user_data)
    columns = set(rows[0].keys()) if rows else set()
    plaintext = sum(bool(row["password"]) for row in rows)
    encrypted = (
        sum(bool(row["credential_ciphertext"]) for row in rows)
        if "credential_ciphertext" in columns
        else 0
    )
    print(
        f"accounts={len(rows)} plaintext={plaintext} encrypted={encrypted} "
        f"orphan_profiles={len(orphaned)} apply={args.apply}"
    )

    if args.orphan_report:
        report = Path(args.orphan_report).resolve()
        report.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        report.write_text("\n".join(entry.name for entry in orphaned) + "\n", encoding="utf-8")
        os.chmod(report, 0o600)

    if not args.apply:
        return 0
    if not args.backup:
        raise SystemExit("--backup is required with --apply")
    if orphaned and not args.orphan_report:
        raise SystemExit("--orphan-report is required when orphan profiles exist")
    if orphaned and not args.quarantine_dir:
        raise SystemExit("--quarantine-dir is required when orphan profiles exist")
    if not args.migration_manifest:
        raise SystemExit("--migration-manifest is required with --apply")

    manifest_path = Path(args.migration_manifest).resolve()
    if manifest_path.exists():
        raise SystemExit(f"Migration manifest already exists: {manifest_path}")

    # 先 checkpoint，避免备份出缺 WAL 数据的库
    checkpoint_wal(db_path)
    encrypted_backup(db_path, Path(args.backup).resolve(), keys[0])
    ensure_schema(str(db_path))
    mappings = plan_profile_mappings(db_path)
    write_manifest(
        manifest_path,
        db_path,
        mappings,
        orphaned,
        Path(args.quarantine_dir).resolve() if args.quarantine_dir else None,
    )
    quarantined: list[tuple[Path, Path]] = []
    if orphaned:
        quarantined = quarantine_orphans(orphaned, Path(args.quarantine_dir).resolve())
    try:
        migrate(db_path, user_data, CredentialCipher(keys), mappings)
        harden_permissions(db_path, user_data, args.owner_uid, args.owner_gid)
    except Exception:
        restore_quarantine(quarantined)
        raise

    rows_after, _ = collect_plan(db_path, user_data)
    if any(row["password"] or not row["credential_ciphertext"] or not row["profile_id"] for row in rows_after):
        raise RuntimeError("Post-migration verification failed")
    print(f"migration_complete accounts={len(rows_after)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
