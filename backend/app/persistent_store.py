from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import get_settings


_lock = threading.RLock()


def _sqlite_path() -> str:
    database_url = get_settings().database_url
    if database_url == "sqlite:///:memory:":
        return ":memory:"
    if not database_url.startswith("sqlite:///"):
        raise RuntimeError("当前持久化层只支持 SQLite DATABASE_URL")
    path = database_url[len("sqlite:///") :]
    if not path:
        raise RuntimeError("DATABASE_URL 缺少 SQLite 文件路径")
    if path != ":memory:":
        resolved = Path(path).expanduser().resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return str(resolved)
    return path


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_sqlite_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _utc_now() -> str:
    return datetime.utcnow().isoformat()


def init_db() -> None:
    with _lock, _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                credits INTEGER NOT NULL DEFAULT 0,
                is_admin INTEGER NOT NULL DEFAULT 0,
                last_login_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tokens (
                token TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(username) REFERENCES users(username) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS invite_codes (
                code TEXT PRIMARY KEY,
                created_by TEXT NOT NULL,
                is_used INTEGER NOT NULL DEFAULT 0,
                used_by TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS video_tasks (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_by TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_user_id ON tasks(user_id);
            CREATE INDEX IF NOT EXISTS idx_video_tasks_user_id ON video_tasks(user_id);
            """
        )


def _user_from_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "password_hash": row["password_hash"],
        "credits": row["credits"],
        "is_admin": bool(row["is_admin"]),
        "last_login_date": row["last_login_date"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def ensure_admin(username: str, password_hash: str, credits: int, last_login_date: str) -> None:
    with _lock, _connect() as conn:
        existing = conn.execute("SELECT username FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            return
        now = _utc_now()
        conn.execute(
            """
            INSERT INTO users (id, username, password_hash, credits, is_admin, last_login_date, created_at, updated_at)
            VALUES (?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (str(uuid.uuid4()), username, password_hash, credits, last_login_date, now, now),
        )


def get_user(username: str) -> dict[str, Any] | None:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return _user_from_row(row)


def create_user(username: str, password_hash: str, credits: int, is_admin: bool, last_login_date: str) -> dict[str, Any]:
    now = _utc_now()
    user_id = str(uuid.uuid4())
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO users (id, username, password_hash, credits, is_admin, last_login_date, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, username, password_hash, credits, int(is_admin), last_login_date, now, now),
        )
    user = get_user(username)
    if not user:
        raise RuntimeError("用户创建失败")
    return user


def update_user(username: str, **fields: Any) -> dict[str, Any]:
    allowed = {"password_hash", "credits", "is_admin", "last_login_date"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        user = get_user(username)
        if not user:
            raise KeyError(username)
        return user
    updates["updated_at"] = _utc_now()
    names = ", ".join(f"{k} = ?" for k in updates)
    values = [int(v) if k == "is_admin" else v for k, v in updates.items()]
    values.append(username)
    with _lock, _connect() as conn:
        cur = conn.execute(f"UPDATE users SET {names} WHERE username = ?", values)
        if cur.rowcount == 0:
            raise KeyError(username)
    user = get_user(username)
    if not user:
        raise KeyError(username)
    return user


def deduct_credits(username: str, cost: int) -> dict[str, Any] | None:
    with _lock, _connect() as conn:
        cur = conn.execute(
            """
            UPDATE users
            SET credits = credits - ?, updated_at = ?
            WHERE username = ? AND credits >= ?
            """,
            (cost, _utc_now(), username, cost),
        )
        if cur.rowcount == 0:
            return None
    return get_user(username)


def add_credits(username: str, amount: int) -> dict[str, Any] | None:
    with _lock, _connect() as conn:
        cur = conn.execute(
            """
            UPDATE users
            SET credits = credits + ?, updated_at = ?
            WHERE username = ?
            """,
            (amount, _utc_now(), username),
        )
        if cur.rowcount == 0:
            return None
    return get_user(username)


def list_users() -> list[dict[str, Any]]:
    with _lock, _connect() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()
        return [u for row in rows if (u := _user_from_row(row))]


def create_token(username: str) -> str:
    token = str(uuid.uuid4())
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO tokens (token, username, created_at) VALUES (?, ?, ?)",
            (token, username, _utc_now()),
        )
    return token


def get_token_username(token: str) -> str | None:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT username FROM tokens WHERE token = ?", (token,)).fetchone()
        return row["username"] if row else None


def delete_tokens_for_user(username: str) -> None:
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM tokens WHERE username = ?", (username,))


def create_invite_code(code: str, created_by: str) -> dict[str, Any]:
    now = _utc_now()
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO invite_codes (code, created_by, is_used, used_by, created_at, updated_at)
            VALUES (?, ?, 0, NULL, ?, ?)
            """,
            (code, created_by, now, now),
        )
    invite = get_invite_code(code)
    if not invite:
        raise RuntimeError("邀请码创建失败")
    return invite


def get_invite_code(code: str) -> dict[str, Any] | None:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM invite_codes WHERE code = ?", (code,)).fetchone()
        if not row:
            return None
        return {
            "code": row["code"],
            "created_by": row["created_by"],
            "is_used": bool(row["is_used"]),
            "used_by": row["used_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


def mark_invite_used(code: str, username: str) -> None:
    with _lock, _connect() as conn:
        cur = conn.execute(
            """
            UPDATE invite_codes
            SET is_used = 1, used_by = ?, updated_at = ?
            WHERE code = ? AND is_used = 0
            """,
            (username, _utc_now(), code),
        )
        if cur.rowcount == 0:
            raise KeyError(code)


def list_invite_codes() -> list[dict[str, Any]]:
    with _lock, _connect() as conn:
        rows = conn.execute("SELECT * FROM invite_codes ORDER BY created_at DESC").fetchall()
        return [
            {
                "code": row["code"],
                "created_by": row["created_by"],
                "is_used": bool(row["is_used"]),
                "used_by": row["used_by"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]


def create_task(task_id: str, payload: dict[str, Any]) -> None:
    _create_json_row("tasks", task_id, payload)


def update_task(task_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    return _update_json_row("tasks", task_id, fields)


def get_task(task_id: str) -> dict[str, Any] | None:
    return _get_json_row("tasks", task_id)


def list_tasks(user_id: str | None = None) -> list[dict[str, Any]]:
    return _list_json_rows("tasks", user_id)


def create_video_task(task_id: str, payload: dict[str, Any]) -> None:
    _create_json_row("video_tasks", task_id, payload)


def update_video_task(task_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    return _update_json_row("video_tasks", task_id, fields)


def get_video_task(task_id: str) -> dict[str, Any] | None:
    return _get_json_row("video_tasks", task_id)


def list_video_tasks(user_id: str | None = None, limit: int | None = 30) -> list[dict[str, Any]]:
    return _list_json_rows("video_tasks", user_id, limit)


def delete_video_task(task_id: str) -> None:
    _delete_json_row("video_tasks", task_id)


def prune_video_tasks(user_id: str, keep: int = 30) -> list[dict[str, Any]]:
    return _prune_json_rows("video_tasks", user_id, keep)


def get_app_setting(key: str) -> dict[str, Any] | None:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM app_settings WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        return {
            "key": row["key"],
            "value": row["value"],
            "updated_by": row["updated_by"] or "",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


def get_app_setting_value(key: str, default: str = "") -> str:
    setting = get_app_setting(key)
    if setting is None:
        return default
    return str(setting.get("value") or "")


def set_app_setting(key: str, value: str, updated_by: str = "") -> dict[str, Any]:
    now = _utc_now()
    with _lock, _connect() as conn:
        existing = conn.execute("SELECT key FROM app_settings WHERE key = ?", (key,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE app_settings SET value = ?, updated_by = ?, updated_at = ? WHERE key = ?",
                (value, updated_by, now, key),
            )
        else:
            conn.execute(
                "INSERT INTO app_settings (key, value, updated_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (key, value, updated_by, now, now),
            )
    setting = get_app_setting(key)
    if not setting:
        raise RuntimeError("系统配置保存失败")
    return setting


def _create_json_row(table: str, task_id: str, payload: dict[str, Any]) -> None:
    payload = dict(payload)
    payload.setdefault("id", task_id)
    now = payload.get("created_at") or _utc_now()
    payload["created_at"] = now
    with _lock, _connect() as conn:
        conn.execute(
            f"INSERT INTO {table} (id, user_id, payload, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (task_id, payload.get("user_id", ""), json.dumps(payload, ensure_ascii=False), now, now),
        )


def _get_json_row(table: str, task_id: str) -> dict[str, Any] | None:
    with _lock, _connect() as conn:
        row = conn.execute(f"SELECT payload FROM {table} WHERE id = ?", (task_id,)).fetchone()
        return json.loads(row["payload"]) if row else None


def _update_json_row(table: str, task_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    with _lock, _connect() as conn:
        row = conn.execute(f"SELECT payload FROM {table} WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload"])
        payload.update(fields)
        conn.execute(
            f"UPDATE {table} SET payload = ?, updated_at = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), _utc_now(), task_id),
        )
        return payload


def _delete_json_row(table: str, task_id: str) -> None:
    with _lock, _connect() as conn:
        conn.execute(f"DELETE FROM {table} WHERE id = ?", (task_id,))


def _list_json_rows(table: str, user_id: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    with _lock, _connect() as conn:
        limit_sql = ""
        params: list[Any] = []
        if user_id:
            query = f"SELECT payload FROM {table} WHERE user_id = ? ORDER BY created_at DESC"
            params.append(user_id)
        else:
            query = f"SELECT payload FROM {table} ORDER BY created_at DESC"
        if limit is not None:
            limit_sql = " LIMIT ?"
            params.append(limit)
        rows = conn.execute(query + limit_sql, params).fetchall()
        return [json.loads(row["payload"]) for row in rows]


def _prune_json_rows(table: str, user_id: str, keep: int) -> list[dict[str, Any]]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, payload FROM {table}
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT -1 OFFSET ?
            """,
            (user_id, keep),
        ).fetchall()
        ids = [row["id"] for row in rows]
        if ids:
            conn.executemany(f"DELETE FROM {table} WHERE id = ?", [(task_id,) for task_id in ids])
        return [json.loads(row["payload"]) for row in rows]
