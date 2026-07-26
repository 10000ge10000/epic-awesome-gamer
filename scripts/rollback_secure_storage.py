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
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.secure_store import normalize_email, safe_profile_path


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
    parser = argparse.ArgumentParser(description="Rollback Epic Kiosk secure storage migration")
    parser.add_argument("--db", required=True)
    parser.add_argument("--user-data", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--backup", required=True, help="Encrypted pre-migration database backup")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--current-backup", help="Encrypted backup of the current database")
    parser.add_argument("--owner-uid", type=int, default=1002)
    parser.add_argument("--owner-gid", type=int, default=1002)
    parser.add_argument("--maintenance-confirmed", action="store_true")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def load_primary_key(path: Path) -> bytes:
    key = next((line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()), "")
    if not key:
        raise ValueError("Key file is empty")
    Fernet(key.encode("ascii"))
    return key.encode("ascii")


def load_manifest(path: Path) -> dict:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("version") != 1 or not isinstance(manifest.get("profiles"), list):
        raise ValueError("Unsupported migration manifest")
    return manifest


def verify_sqlite_bytes(payload: bytes) -> None:
    fd, name = tempfile.mkstemp(prefix="epic-kiosk-rollback-", suffix=".db")
    temp_path = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        with sqlite3.connect(temp_path) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"Backup database integrity check failed: {result}")
    finally:
        temp_path.unlink(missing_ok=True)


def encrypted_backup(source: Path, target: Path, key: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.exists():
        raise FileExistsError(f"Backup already exists: {target}")
    with target.open("xb") as handle:
        handle.write(Fernet(key).encrypt(source.read_bytes()))
    os.chmod(target, 0o600)


def move_profiles_back(user_data: Path, manifest: dict) -> list[tuple[Path, Path]]:
    moves: list[tuple[Path, Path]] = []
    planned: list[tuple[Path, Path]] = []
    for item in manifest["profiles"]:
        source = safe_profile_path(str(user_data), item["profile_id"])
        target = user_data / normalize_email(item["email"])
        if source.exists() or source.is_symlink():
            if target.exists() or target.is_symlink():
                raise FileExistsError(f"Legacy profile target already exists: {target}")
            planned.append((source, target))

    quarantine = manifest.get("quarantine_dir")
    if quarantine:
        quarantine_dir = Path(quarantine).resolve()
        for name in manifest.get("orphans", []):
            if not name or Path(name).name != name or name in {".", ".."}:
                raise ValueError("Invalid orphan profile name in manifest")
            source = quarantine_dir / name
            target = user_data / name
            if source.exists() or source.is_symlink():
                if target.exists() or target.is_symlink():
                    raise FileExistsError(f"Orphan restore target already exists: {target}")
                planned.append((source, target))

    try:
        for source, target in planned:
            try:
                os.replace(source, target)
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                shutil.move(str(source), str(target))
            moves.append((target, source))
    except Exception:
        restore_moves(moves)
        raise
    return moves


def restore_moves(moves: list[tuple[Path, Path]]) -> None:
    for current, original in reversed(moves):
        if (current.exists() or current.is_symlink()) and not original.exists():
            try:
                os.replace(current, original)
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                shutil.move(str(current), str(original))


def restore_database(db_path: Path, payload: bytes, owner_uid: int, owner_gid: int) -> None:
    temp_path = db_path.with_name(f".{db_path.name}.rollback-{os.getpid()}")
    if temp_path.exists():
        raise FileExistsError(f"Rollback temp file already exists: {temp_path}")
    try:
        with temp_path.open("xb") as handle:
            handle.write(payload)
        os.chown(temp_path, owner_uid, owner_gid)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, db_path)
    finally:
        temp_path.unlink(missing_ok=True)


def main() -> int:
    args = parse_args()
    db_path = Path(args.db).resolve()
    user_data = Path(args.user_data).resolve()
    backup_path = Path(args.backup).resolve()
    key = load_primary_key(Path(args.key_file).resolve())
    manifest = load_manifest(Path(args.manifest).resolve())
    restored_payload = Fernet(key).decrypt(backup_path.read_bytes())
    verify_sqlite_bytes(restored_payload)

    print(
        f"profiles={len(manifest['profiles'])} orphans={len(manifest.get('orphans', []))} "
        f"apply={args.apply}"
    )
    if not args.apply:
        return 0
    if not args.maintenance_confirmed:
        raise SystemExit("--maintenance-confirmed is required with --apply")
    if not args.current_backup:
        raise SystemExit("--current-backup is required with --apply")
    checkpoint_wal(db_path)

    encrypted_backup(db_path, Path(args.current_backup).resolve(), key)
    moves = move_profiles_back(user_data, manifest)
    try:
        restore_database(db_path, restored_payload, args.owner_uid, args.owner_gid)
    except Exception:
        restore_moves(moves)
        raise
    print("rollback_complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
