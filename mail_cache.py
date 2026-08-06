"""Thread-safe local cache for the read-only mail viewer."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Mapping

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # pragma: no cover - reported by the application boundary
    AESGCM = None  # type: ignore[assignment]


CACHE_MODES = ("memory", "metadata", "body")
DEFAULT_CACHE_MODE = "body"
DEFAULT_REFRESH_MINUTES = 3
MAX_REFRESH_MINUTES = 1440
# Body envelopes are versioned independently from the AES-GCM AAD.  Keeping
# the existing ``body-v2`` AAD lets installations decrypt cache rows created
# by the v1.3 rich-text implementation while the payload gains an image
# manifest in v3.
BODY_CACHE_ENVELOPE_VERSION = 3
BODY_CACHE_ENVELOPE_V2 = 2
DEFAULT_IMAGE_CACHE_LIMIT_BYTES = 256 * 1024 * 1024
DEFAULT_IMAGE_MEMORY_LIMIT_BYTES = 64 * 1024 * 1024
_CACHEABLE_BODY_FORMATS = frozenset({"plain", "html"})
_NON_CACHEABLE_BODY_TEXTS = frozenset(
    {
        "为避免下载附件，正文无法安全读取",
        "读取邮件正文超时。",
    }
)


@dataclass(frozen=True)
class CacheSettings:
    cache_mode: str = DEFAULT_CACHE_MODE
    refresh_minutes: int = DEFAULT_REFRESH_MINUTES

    def validated(self) -> "CacheSettings":
        if self.cache_mode not in CACHE_MODES:
            raise ValueError("cache_mode must be memory, metadata, or body")
        if not 0 <= self.refresh_minutes <= MAX_REFRESH_MINUTES:
            raise ValueError("refresh_minutes must be between 0 and 1440")
        return self

    def public_record(self) -> dict[str, object]:
        return {
            "cache_mode": self.cache_mode,
            "refresh_minutes": self.refresh_minutes,
        }


@dataclass(frozen=True)
class CachedMessage:
    account_name: str
    uid: str
    subject: str
    sender: str
    recipients: str
    date: str
    received_at: float
    size: int
    unread: bool


@dataclass(frozen=True)
class CachedPage:
    messages: tuple[CachedMessage, ...]
    total: int
    offset: int
    limit: int

    @property
    def page_count(self) -> int:
        return (self.total + self.limit - 1) // self.limit if self.total else 0

    @property
    def current_page(self) -> int:
        return self.offset // self.limit + 1 if self.total else 0


@dataclass(frozen=True)
class CachedDetail:
    message: CachedMessage
    text: str
    attachments: tuple[str, ...]
    body_format: str = "plain"
    safe_html: str = ""
    blocked_images: int = 0
    html_policy: str = ""
    image_resources: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True)
class CachedImage:
    """A decrypted image blob and its non-sensitive cache metadata."""

    account_name: str
    uid: str
    resource_id: str
    data: bytes
    mime_type: str
    source_type: str
    source_digest: str
    size: int
    created_at: float
    accessed_at: float


@dataclass(frozen=True)
class SyncState:
    account_name: str
    uidvalidity: str
    highest_uid: int
    unread_seeded: bool
    all_seeded: bool
    full_sync_complete: bool
    last_success: float | None
    last_attempt: float | None
    last_error: str


def default_cache_path(platform: str, home: Path, local_app_data: str | None = None) -> Path:
    if platform == "darwin":
        return home / "Library" / "Caches" / "local-readonly-mail-viewer" / "mail-cache.sqlite3"
    if platform == "win32":
        base = Path(local_app_data) if local_app_data else home / "AppData" / "Local"
        return base / "local-readonly-mail-viewer" / "mail-cache.sqlite3"
    return home / ".cache" / "local-readonly-mail-viewer" / "mail-cache.sqlite3"


class CacheStore:
    """One SQLite connection protected by a process-wide re-entrant lock."""

    def __init__(
        self,
        path: Path,
        settings: CacheSettings,
        encryption_key: bytes,
        *,
        image_cache_limit_bytes: int = DEFAULT_IMAGE_CACHE_LIMIT_BYTES,
        image_memory_limit_bytes: int = DEFAULT_IMAGE_MEMORY_LIMIT_BYTES,
    ) -> None:
        self.path = path
        self.settings = settings.validated()
        self.encryption_key = encryption_key
        if image_cache_limit_bytes < 0:
            raise ValueError("image_cache_limit_bytes must be non-negative")
        if image_memory_limit_bytes < 0:
            raise ValueError("image_memory_limit_bytes must be non-negative")
        self.image_cache_limit_bytes = int(image_cache_limit_bytes)
        self.image_memory_limit_bytes = int(image_memory_limit_bytes)
        self._image_memory: OrderedDict[tuple[str, int, str], CachedImage] = (
            OrderedDict()
        )
        self._image_memory_bytes = 0
        self._lock = threading.RLock()
        if self.settings.cache_mode == "memory":
            database = ":memory:"
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            database = str(path)
        self.connection = sqlite3.connect(database, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        with self._lock:
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA busy_timeout=5000")
            if database != ":memory:":
                self.connection.execute("PRAGMA journal_mode=WAL")
            self._initialize()
        if database != ":memory:" and os.name != "nt":
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass

    @property
    def body_cache_enabled(self) -> bool:
        return self.settings.cache_mode in {"memory", "body"}

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                account_name TEXT NOT NULL,
                uid INTEGER NOT NULL,
                subject TEXT NOT NULL,
                sender TEXT NOT NULL,
                recipients TEXT NOT NULL DEFAULT '',
                date_text TEXT NOT NULL,
                received_at REAL NOT NULL,
                size INTEGER NOT NULL DEFAULT 0,
                unread INTEGER NOT NULL DEFAULT 0,
                attachments_json TEXT NOT NULL DEFAULT '[]',
                body_nonce BLOB,
                body_ciphertext BLOB,
                body_cached_at REAL,
                PRIMARY KEY (account_name, uid)
            );
            CREATE INDEX IF NOT EXISTS messages_account_unread_date
                ON messages(account_name, unread, received_at DESC, uid DESC);
            CREATE INDEX IF NOT EXISTS messages_unread_date
                ON messages(unread, received_at DESC, uid DESC);
            CREATE TABLE IF NOT EXISTS message_images (
                account_name TEXT NOT NULL,
                uid INTEGER NOT NULL,
                resource_id TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT '',
                source_digest TEXT NOT NULL DEFAULT '',
                mime_type TEXT NOT NULL DEFAULT 'application/octet-stream',
                size INTEGER NOT NULL DEFAULT 0,
                image_nonce BLOB NOT NULL,
                image_ciphertext BLOB NOT NULL,
                created_at REAL NOT NULL,
                accessed_at REAL NOT NULL,
                PRIMARY KEY (account_name, uid, resource_id),
                FOREIGN KEY (account_name, uid)
                    REFERENCES messages(account_name, uid) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS message_images_lru
                ON message_images(accessed_at, created_at);
            CREATE TABLE IF NOT EXISTS sync_state (
                account_name TEXT PRIMARY KEY,
                uidvalidity TEXT NOT NULL DEFAULT '',
                highest_uid INTEGER NOT NULL DEFAULT 0,
                unread_seeded INTEGER NOT NULL DEFAULT 0,
                all_seeded INTEGER NOT NULL DEFAULT 0,
                full_sync_complete INTEGER NOT NULL DEFAULT 0,
                last_success REAL,
                last_attempt REAL,
                last_error TEXT NOT NULL DEFAULT ''
            );
            """
        )
        self._ensure_columns(
            "messages",
            {
                "recipients": "TEXT NOT NULL DEFAULT ''",
                "attachments_json": "TEXT NOT NULL DEFAULT '[]'",
                "body_nonce": "BLOB",
                "body_ciphertext": "BLOB",
                "body_cached_at": "REAL",
            },
        )
        self._ensure_columns(
            "sync_state",
            {
                "uidvalidity": "TEXT NOT NULL DEFAULT ''",
                "highest_uid": "INTEGER NOT NULL DEFAULT 0",
                "unread_seeded": "INTEGER NOT NULL DEFAULT 0",
                "all_seeded": "INTEGER NOT NULL DEFAULT 0",
                "full_sync_complete": "INTEGER NOT NULL DEFAULT 0",
                "last_success": "REAL",
                "last_attempt": "REAL",
                "last_error": "TEXT NOT NULL DEFAULT ''",
            },
        )
        self.connection.execute("PRAGMA user_version=3")
        self.connection.commit()

    def _ensure_columns(self, table: str, columns: Mapping[str, str]) -> None:
        existing = {
            str(row["name"])
            for row in self.connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, declaration in columns.items():
            if name not in existing:
                self.connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                )

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def upsert_messages(self, account_name: str, messages: Iterable[Mapping[str, object]]) -> None:
        rows = [
            (
                account_name,
                int(str(item["uid"])),
                str(item.get("subject", "")),
                str(item.get("sender", "")),
                str(item.get("recipients", "")),
                str(item.get("date", "")),
                float(item.get("received_at", 0.0)),
                int(item.get("size", 0)),
                1 if bool(item.get("unread", False)) else 0,
                json.dumps(list(item.get("attachments", ())), ensure_ascii=False),
            )
            for item in messages
        ]
        if not rows:
            return
        with self._lock, self.connection:
            self.connection.executemany(
                """
                INSERT INTO messages (
                    account_name, uid, subject, sender, recipients, date_text,
                    received_at, size, unread, attachments_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_name, uid) DO UPDATE SET
                    subject=excluded.subject,
                    sender=excluded.sender,
                    recipients=CASE
                        WHEN excluded.recipients != '' THEN excluded.recipients
                        ELSE messages.recipients
                    END,
                    date_text=excluded.date_text,
                    received_at=excluded.received_at,
                    size=excluded.size,
                    unread=excluded.unread,
                    attachments_json=CASE
                        WHEN excluded.attachments_json != '[]' THEN excluded.attachments_json
                        ELSE messages.attachments_json
                    END
                """,
                rows,
            )

    def account_uids(self, account_name: str) -> set[str]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT uid FROM messages WHERE account_name=?", (account_name,)
            ).fetchall()
        return {str(row["uid"]) for row in rows}

    def reconcile_account(
        self,
        account_name: str,
        all_uids: Iterable[str],
        unread_uids: Iterable[str],
    ) -> None:
        remote = {str(uid) for uid in all_uids}
        unread = {str(uid) for uid in unread_uids}
        cached = self.account_uids(account_name)
        removed = cached - remote
        with self._lock, self.connection:
            if removed:
                self.connection.executemany(
                    "DELETE FROM messages WHERE account_name=? AND uid=?",
                    ((account_name, int(uid)) for uid in removed),
                )
                for uid in removed:
                    self._drop_memory_images_locked(account_name=account_name, uid=uid)
            self.connection.execute(
                "UPDATE messages SET unread=0 WHERE account_name=?", (account_name,)
            )
            if unread:
                self.connection.executemany(
                    "UPDATE messages SET unread=1 WHERE account_name=? AND uid=?",
                    ((account_name, int(uid)) for uid in unread),
                )

    def clear_account(self, account_name: str) -> None:
        with self._lock, self.connection:
            self.connection.execute("DELETE FROM messages WHERE account_name=?", (account_name,))
            self.connection.execute("DELETE FROM sync_state WHERE account_name=?", (account_name,))
            self._drop_memory_images_locked(account_name=account_name)

    def clear_bodies(self) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                "UPDATE messages SET body_nonce=NULL, body_ciphertext=NULL, body_cached_at=NULL"
            )
            self.connection.execute("DELETE FROM message_images")
            self._drop_memory_images_locked()

    def clear_all(self) -> None:
        with self._lock, self.connection:
            self.connection.execute("DELETE FROM messages")
            self.connection.execute("DELETE FROM sync_state")
            # The foreign key normally removes these rows with messages.  The
            # explicit delete also covers databases created before the image
            # table was introduced and keeps the intent obvious.
            self.connection.execute("DELETE FROM message_images")
            self._drop_memory_images_locked()

    def mark_seeded(self, account_name: str, unread_only: bool) -> None:
        column = "unread_seeded" if unread_only else "all_seeded"
        with self._lock, self.connection:
            self.connection.execute(
                f"""
                INSERT INTO sync_state(account_name, {column}) VALUES (?, 1)
                ON CONFLICT(account_name) DO UPDATE SET {column}=1
                """,
                (account_name,),
            )

    def mark_sync_attempt(self, account_name: str) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO sync_state(account_name, last_attempt) VALUES (?, ?)
                ON CONFLICT(account_name) DO UPDATE SET last_attempt=excluded.last_attempt
                """,
                (account_name, time.time()),
            )

    def mark_sync_success(
        self,
        account_name: str,
        *,
        uidvalidity: str,
        highest_uid: int,
        full_sync_complete: bool,
    ) -> None:
        now = time.time()
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO sync_state(
                    account_name, uidvalidity, highest_uid, full_sync_complete,
                    last_success, last_attempt, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, '')
                ON CONFLICT(account_name) DO UPDATE SET
                    uidvalidity=excluded.uidvalidity,
                    highest_uid=excluded.highest_uid,
                    full_sync_complete=excluded.full_sync_complete,
                    last_success=excluded.last_success,
                    last_attempt=excluded.last_attempt,
                    last_error=''
                """,
                (
                    account_name,
                    uidvalidity,
                    highest_uid,
                    1 if full_sync_complete else 0,
                    now,
                    now,
                ),
            )

    def mark_sync_error(self, account_name: str, error: str) -> None:
        now = time.time()
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO sync_state(account_name, last_attempt, last_error)
                VALUES (?, ?, ?)
                ON CONFLICT(account_name) DO UPDATE SET
                    last_attempt=excluded.last_attempt,
                    last_error=excluded.last_error
                """,
                (account_name, now, error[:500]),
            )

    def sync_state(self, account_name: str) -> SyncState:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM sync_state WHERE account_name=?", (account_name,)
            ).fetchone()
        if row is None:
            return SyncState(account_name, "", 0, False, False, False, None, None, "")
        return SyncState(
            account_name=account_name,
            uidvalidity=str(row["uidvalidity"]),
            highest_uid=int(row["highest_uid"]),
            unread_seeded=bool(row["unread_seeded"]),
            all_seeded=bool(row["all_seeded"]),
            full_sync_complete=bool(row["full_sync_complete"]),
            last_success=float(row["last_success"]) if row["last_success"] is not None else None,
            last_attempt=float(row["last_attempt"]) if row["last_attempt"] is not None else None,
            last_error=str(row["last_error"]),
        )

    def account_is_fresh(self, account_name: str, refresh_minutes: int) -> bool:
        if refresh_minutes == 0:
            return False
        state = self.sync_state(account_name)
        return bool(
            state.last_success is not None
            and time.time() - state.last_success <= refresh_minutes * 60
        )

    def query_page(
        self,
        account_names: Iterable[str],
        *,
        unread_only: bool,
        limit: int,
        offset: int = 0,
    ) -> CachedPage:
        names = tuple(account_names)
        if not names:
            return CachedPage((), 0, 0, limit)
        placeholders = ",".join("?" for _ in names)
        where = f"account_name IN ({placeholders})"
        values: list[object] = list(names)
        if unread_only:
            where += " AND unread=1"
        with self._lock:
            total = int(
                self.connection.execute(
                    f"SELECT COUNT(*) FROM messages WHERE {where}", values
                ).fetchone()[0]
            )
            last_offset = ((total - 1) // limit) * limit if total else 0
            effective_offset = min(max(offset, 0), last_offset)
            rows = self.connection.execute(
                f"""
                SELECT account_name, uid, subject, sender, recipients, date_text,
                       received_at, size, unread
                FROM messages WHERE {where}
                ORDER BY received_at DESC, uid DESC
                LIMIT ? OFFSET ?
                """,
                [*values, limit, effective_offset],
            ).fetchall()
        return CachedPage(
            tuple(self._message_from_row(row) for row in rows),
            total,
            effective_offset,
            limit,
        )

    def query_messages(
        self,
        account_names: Iterable[str],
        *,
        unread_only: bool,
        limit: int | None,
        offset: int = 0,
        since_timestamp: float | None = None,
    ) -> tuple[CachedMessage, ...]:
        names = tuple(account_names)
        if not names:
            return ()
        placeholders = ",".join("?" for _ in names)
        where = f"account_name IN ({placeholders})"
        values: list[object] = list(names)
        if unread_only:
            where += " AND unread=1"
        if since_timestamp is not None:
            where += " AND received_at>=?"
            values.append(since_timestamp)
        sql = f"""
            SELECT account_name, uid, subject, sender, recipients, date_text,
                   received_at, size, unread
            FROM messages WHERE {where}
            ORDER BY received_at DESC, uid DESC
        """
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            values.extend((limit, max(offset, 0)))
        elif offset:
            sql += " LIMIT -1 OFFSET ?"
            values.append(max(offset, 0))
        with self._lock:
            rows = self.connection.execute(sql, values).fetchall()
        return tuple(self._message_from_row(row) for row in rows)

    def account_message_count(self, account_name: str) -> int:
        with self._lock:
            return int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM messages WHERE account_name=?",
                    (account_name,),
                ).fetchone()[0]
            )

    def message(self, account_name: str, uid: str) -> CachedMessage | None:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT account_name, uid, subject, sender, recipients, date_text,
                       received_at, size, unread
                FROM messages WHERE account_name=? AND uid=?
                """,
                (account_name, int(uid)),
            ).fetchone()
        return self._message_from_row(row) if row is not None else None

    def cached_detail(self, account_name: str, uid: str) -> CachedDetail | None:
        if not self.body_cache_enabled:
            return None
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM messages WHERE account_name=? AND uid=?",
                (account_name, int(uid)),
            ).fetchone()
        if row is None or row["body_nonce"] is None or row["body_ciphertext"] is None:
            return None
        if AESGCM is None:
            raise RuntimeError("缺少 cryptography，无法解密正文缓存；请重新安装项目依赖。")
        try:
            payload = self._decrypt_v2(
                bytes(row["body_nonce"]),
                bytes(row["body_ciphertext"]),
                account_name,
                uid,
            )
            (
                body_format,
                text,
                safe_html,
                blocked_images,
                html_policy,
                image_resources,
            ) = self._decode_body_payload_with_images(payload)
        except Exception:
            try:
                text = self._decrypt_legacy(
                    bytes(row["body_nonce"]),
                    bytes(row["body_ciphertext"]),
                    account_name,
                    uid,
                )
            except Exception:
                self.delete_cached_body(account_name, uid)
                return None
            body_format = "plain"
            safe_html = ""
            blocked_images = 0
            html_policy = ""
            image_resources = ()
        attachments = tuple(json.loads(str(row["attachments_json"])))
        return CachedDetail(
            self._message_from_row(row),
            text,
            attachments,
            body_format,
            safe_html,
            blocked_images,
            html_policy,
            image_resources,
        )

    def store_detail(
        self,
        account_name: str,
        uid: str,
        *,
        recipients: str,
        text: str,
        attachments: Iterable[str],
        body_format: str = "plain",
        safe_html: str = "",
        blocked_images: int = 0,
        html_policy: str = "",
        image_resources: Iterable[Mapping[str, object]] | None = None,
        cacheable: bool = True,
    ) -> None:
        nonce: bytes | None = None
        ciphertext: bytes | None = None
        cached_at: float | None = None
        normalized_format = str(body_format)
        blocked_count = max(0, int(blocked_images))
        normalized_images = (
            self._normalize_image_resources(image_resources)
            if image_resources is not None
            else ()
        )
        should_cache = (
            self.body_cache_enabled
            and cacheable
            and normalized_format in _CACHEABLE_BODY_FORMATS
            and text.strip() not in _NON_CACHEABLE_BODY_TEXTS
        )
        if should_cache:
            payload = self._encode_body_payload(
                body_format=normalized_format,
                text=text,
                safe_html=safe_html,
                blocked_images=blocked_count,
                html_policy=html_policy,
                image_resources=normalized_images,
            )
            nonce, ciphertext = self._encrypt(payload, account_name, uid)
            cached_at = time.time()
        with self._lock, self.connection:
            attachments_json = json.dumps(list(attachments), ensure_ascii=False)
            if should_cache:
                self.connection.execute(
                    """
                    UPDATE messages SET recipients=?, attachments_json=?,
                        body_nonce=?, body_ciphertext=?, body_cached_at=?
                    WHERE account_name=? AND uid=?
                    """,
                    (
                        recipients,
                        attachments_json,
                        nonce,
                        ciphertext,
                        cached_at,
                        account_name,
                        int(uid),
                    ),
                )
                if image_resources is not None:
                    self._remove_unlisted_images_locked(
                        account_name, uid, normalized_images
                    )
            elif cacheable:
                self.connection.execute(
                    """
                    UPDATE messages SET recipients=?, attachments_json=?
                    WHERE account_name=? AND uid=?
                    """,
                    (recipients, attachments_json, account_name, int(uid)),
                )
                if image_resources is not None:
                    self._remove_unlisted_images_locked(
                        account_name, uid, normalized_images
                    )
            else:
                self.connection.execute(
                    """
                    UPDATE messages SET recipients=?,
                        attachments_json=CASE
                            WHEN ? != '[]' THEN ?
                            ELSE attachments_json
                        END
                    WHERE account_name=? AND uid=?
                    """,
                    (
                        recipients,
                        attachments_json,
                        attachments_json,
                        account_name,
                        int(uid),
                    ),
                )

    def delete_cached_body(self, account_name: str, uid: str) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                """
                UPDATE messages SET body_nonce=NULL, body_ciphertext=NULL, body_cached_at=NULL
                WHERE account_name=? AND uid=?
                """,
                (account_name, int(uid)),
            )
            self.connection.execute(
                "DELETE FROM message_images WHERE account_name=? AND uid=?",
                (account_name, int(uid)),
            )
            self._drop_memory_images_locked(account_name=account_name, uid=uid)

    def load_image(
        self,
        account_name: str,
        uid: str,
        resource_id: str,
        *,
        source_digest: str | None = None,
    ) -> CachedImage | None:
        """Return one encrypted image, preferring the process-local LRU."""

        key = (account_name, int(uid), resource_id)
        with self._lock:
            memory = self._image_memory.get(key)
            if memory is not None:
                if source_digest is None or memory.source_digest == source_digest:
                    self._image_memory.move_to_end(key)
                    return replace(memory, accessed_at=time.time())
                self._drop_memory_images_locked(account_name=account_name, uid=uid, resource_id=resource_id)
            if not self.body_cache_enabled:
                return None
            row = self.connection.execute(
                """
                SELECT source_type, source_digest, mime_type, size, image_nonce,
                       image_ciphertext, created_at, accessed_at
                FROM message_images
                WHERE account_name=? AND uid=? AND resource_id=?
                """,
                (account_name, int(uid), resource_id),
            ).fetchone()
        if row is None:
            return None
        row_digest = str(row["source_digest"])
        if source_digest is not None and row_digest != source_digest:
            self.delete_cached_image(account_name, uid, resource_id)
            return None
        try:
            data = self._decrypt_image(
                bytes(row["image_nonce"]),
                bytes(row["image_ciphertext"]),
                account_name,
                uid,
                resource_id,
                row_digest,
            )
        except Exception:
            self.delete_cached_image(account_name, uid, resource_id)
            return None
        if len(data) != int(row["size"]):
            self.delete_cached_image(account_name, uid, resource_id)
            return None
        image = CachedImage(
            account_name=account_name,
            uid=str(uid),
            resource_id=resource_id,
            data=data,
            mime_type=str(row["mime_type"]),
            source_type=str(row["source_type"]),
            source_digest=row_digest,
            size=len(data),
            created_at=float(row["created_at"]),
            accessed_at=time.time(),
        )
        with self._lock, self.connection:
            self.connection.execute(
                """
                UPDATE message_images SET accessed_at=?
                WHERE account_name=? AND uid=? AND resource_id=?
                """,
                (image.accessed_at, account_name, int(uid), resource_id),
            )
            self._store_memory_image_locked(image)
        return image

    def store_image(
        self,
        account_name: str,
        uid: str,
        resource_id: str,
        *,
        mime_type: str,
        source_type: str,
        source_digest: str,
        data: bytes,
        now: float | None = None,
    ) -> None:
        if not resource_id or len(resource_id) > 128:
            raise ValueError("invalid image resource ID")
        if not source_digest or len(source_digest) != 64:
            raise ValueError("invalid image source digest")
        if not mime_type.startswith("image/") or len(mime_type) > 100:
            raise ValueError("invalid image MIME type")
        if not source_type or len(source_type) > 32:
            raise ValueError("invalid image source type")
        payload = bytes(data)
        if not payload:
            raise ValueError("image payload is empty")
        timestamp = time.time() if now is None else float(now)
        image = CachedImage(
            account_name=account_name,
            uid=str(uid),
            resource_id=resource_id,
            data=payload,
            mime_type=mime_type,
            source_type=source_type,
            source_digest=source_digest,
            size=len(payload),
            created_at=timestamp,
            accessed_at=timestamp,
        )
        nonce = ciphertext = None
        if self.body_cache_enabled:
            nonce, ciphertext = self._encrypt_image(
                payload, account_name, uid, resource_id, source_digest
            )
        with self._lock, self.connection:
            self._store_memory_image_locked(image)
            if self.body_cache_enabled:
                self.connection.execute(
                    """
                    INSERT INTO message_images(
                        account_name, uid, resource_id, source_type, source_digest,
                        mime_type, size, image_nonce, image_ciphertext, created_at,
                        accessed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_name, uid, resource_id) DO UPDATE SET
                        source_type=excluded.source_type,
                        source_digest=excluded.source_digest,
                        mime_type=excluded.mime_type,
                        size=excluded.size,
                        image_nonce=excluded.image_nonce,
                        image_ciphertext=excluded.image_ciphertext,
                        created_at=excluded.created_at,
                        accessed_at=excluded.accessed_at
                    """,
                    (
                        account_name,
                        int(uid),
                        resource_id,
                        source_type,
                        source_digest,
                        mime_type,
                        len(payload),
                        nonce,
                        ciphertext,
                        timestamp,
                        timestamp,
                    ),
                )
                self._prune_image_cache_locked()

    def delete_cached_image(self, account_name: str, uid: str, resource_id: str) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                "DELETE FROM message_images WHERE account_name=? AND uid=? AND resource_id=?",
                (account_name, int(uid), resource_id),
            )
            self._drop_memory_images_locked(account_name=account_name, uid=uid, resource_id=resource_id)

    def delete_message_images(self, account_name: str, uid: str) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                "DELETE FROM message_images WHERE account_name=? AND uid=?",
                (account_name, int(uid)),
            )
            self._drop_memory_images_locked(account_name=account_name, uid=uid)

    def prune_image_cache(self, max_bytes: int | None = None) -> int:
        with self._lock, self.connection:
            return self._prune_image_cache_locked(
                self.image_cache_limit_bytes if max_bytes is None else int(max_bytes)
            )

    def image_bytes_for_message(self, account_name: str, uid: str) -> int:
        with self._lock:
            if self.body_cache_enabled:
                row = self.connection.execute(
                    "SELECT COALESCE(SUM(size), 0) FROM message_images WHERE account_name=? AND uid=?",
                    (account_name, int(uid)),
                ).fetchone()
                return int(row[0] or 0)
            return sum(
                image.size
                for key, image in self._image_memory.items()
                if key[0] == account_name and key[1] == int(uid)
            )

    def _encrypt(self, text: str, account_name: str, uid: str) -> tuple[bytes, bytes]:
        if AESGCM is None:
            raise RuntimeError("缺少 cryptography，无法加密正文缓存；请重新安装项目依赖。")
        nonce = os.urandom(12)
        associated = self._body_associated_data(account_name, uid, version=2)
        ciphertext = AESGCM(self.encryption_key).encrypt(
            nonce, text.encode("utf-8"), associated
        )
        return nonce, ciphertext

    def _decrypt_v2(
        self, nonce: bytes, ciphertext: bytes, account_name: str, uid: str
    ) -> str:
        if AESGCM is None:
            raise RuntimeError("缺少 cryptography，无法解密正文缓存；请重新安装项目依赖。")
        associated = self._body_associated_data(account_name, uid, version=2)
        payload = AESGCM(self.encryption_key).decrypt(nonce, ciphertext, associated)
        return payload.decode("utf-8")

    def _decrypt_legacy(
        self, nonce: bytes, ciphertext: bytes, account_name: str, uid: str
    ) -> str:
        if AESGCM is None:
            raise RuntimeError("缺少 cryptography，无法解密正文缓存；请重新安装项目依赖。")
        associated = self._body_associated_data(account_name, uid, version=1)
        payload = AESGCM(self.encryption_key).decrypt(nonce, ciphertext, associated)
        return payload.decode("utf-8")

    def _encrypt_image(
        self,
        data: bytes,
        account_name: str,
        uid: str,
        resource_id: str,
        source_digest: str,
    ) -> tuple[bytes, bytes]:
        if AESGCM is None:
            raise RuntimeError("缺少 cryptography，无法加密图片缓存；请重新安装项目依赖。")
        nonce = os.urandom(12)
        associated = self._image_associated_data(
            account_name, uid, resource_id, source_digest
        )
        return nonce, AESGCM(self.encryption_key).encrypt(nonce, data, associated)

    def _decrypt_image(
        self,
        nonce: bytes,
        ciphertext: bytes,
        account_name: str,
        uid: str,
        resource_id: str,
        source_digest: str,
    ) -> bytes:
        if AESGCM is None:
            raise RuntimeError("缺少 cryptography，无法解密图片缓存；请重新安装项目依赖。")
        associated = self._image_associated_data(
            account_name, uid, resource_id, source_digest
        )
        return AESGCM(self.encryption_key).decrypt(nonce, ciphertext, associated)

    @staticmethod
    def _body_associated_data(
        account_name: str, uid: str, *, version: int
    ) -> bytes:
        if version == 1:
            return f"{account_name}:{uid}".encode("utf-8")
        if version == 2:
            return f"{account_name}:{uid}:body-v2".encode("utf-8")
        raise ValueError("unsupported body cache AAD version")

    @staticmethod
    def _image_associated_data(
        account_name: str, uid: str, resource_id: str, source_digest: str
    ) -> bytes:
        return (
            f"{account_name}:{uid}:image-v1:{resource_id}:{source_digest}"
        ).encode("utf-8")

    @staticmethod
    def _encode_body_payload(
        *,
        body_format: str,
        text: str,
        safe_html: str,
        blocked_images: int,
        html_policy: str,
        image_resources: tuple[dict[str, object], ...],
    ) -> str:
        envelope = {
            "v": BODY_CACHE_ENVELOPE_VERSION,
            "policy": html_policy,
            "format": body_format,
            "text": text,
            "safe_html": safe_html,
            "blocked_images": blocked_images,
        }
        if image_resources:
            envelope["images"] = image_resources
        return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _decode_body_payload_with_images(
        payload: str,
    ) -> tuple[str, str, str, int, str, tuple[dict[str, object], ...]]:
        try:
            envelope = json.loads(payload)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("invalid body cache envelope") from exc

        required = {"v", "policy", "format", "text", "safe_html", "blocked_images"}
        if not isinstance(envelope, dict) or not required.issubset(envelope):
            raise ValueError("invalid body cache envelope")
        if envelope["v"] not in {BODY_CACHE_ENVELOPE_V2, BODY_CACHE_ENVELOPE_VERSION}:
            raise ValueError("unsupported body cache envelope version")

        body_format = envelope["format"]
        text = envelope["text"]
        safe_html = envelope["safe_html"]
        blocked_images = envelope["blocked_images"]
        html_policy = envelope["policy"]
        if body_format not in _CACHEABLE_BODY_FORMATS:
            raise ValueError("invalid cached body format")
        if not all(isinstance(value, str) for value in (text, safe_html, html_policy)):
            raise ValueError("invalid cached body text fields")
        if type(blocked_images) is not int or blocked_images < 0:
            raise ValueError("invalid cached blocked image count")
        if envelope["v"] == BODY_CACHE_ENVELOPE_V2:
            images: tuple[dict[str, object], ...] = ()
        else:
            images = CacheStore._normalize_image_resources(envelope.get("images", ()))
        return body_format, text, safe_html, blocked_images, html_policy, images

    @staticmethod
    def _decode_body_payload(payload: str) -> tuple[str, str, str, int, str]:
        body_format, text, safe_html, blocked_images, html_policy, _images = (
            CacheStore._decode_body_payload_with_images(payload)
        )
        return body_format, text, safe_html, blocked_images, html_policy

    @staticmethod
    def _normalize_image_resources(
        values: Iterable[Mapping[str, object]] | object,
    ) -> tuple[dict[str, object], ...]:
        if not isinstance(values, Iterable) or isinstance(values, (str, bytes, dict)):
            raise ValueError("invalid image resource manifest")
        result: list[dict[str, object]] = []
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, Mapping):
                raise ValueError("invalid image resource")
            resource_id = value.get("id", value.get("resource_id", ""))
            source_type = value.get("source_type", "")
            source = value.get("source", "")
            descriptor = value.get("descriptor", "")
            section = value.get("section", "")
            content_type = value.get("content_type", "")
            encoding = value.get("encoding", "")
            octets = value.get("octets")
            if (
                not isinstance(resource_id, str)
                or not re.fullmatch(r"r[1-9][0-9]{0,5}", resource_id)
                or resource_id in seen
                or not isinstance(source_type, str)
                or source_type not in {"cid", "data", "location", "remote"}
                or not isinstance(source, str)
                or not 0 < len(source) <= 16_384
                or not isinstance(descriptor, str)
                or not isinstance(section, str)
                or not isinstance(content_type, str)
                or not isinstance(encoding, str)
                or (octets is not None and (type(octets) is not int or octets < 0))
            ):
                raise ValueError("invalid image resource")
            seen.add(resource_id)
            result.append(
                {
                    "id": resource_id,
                    "source_type": source_type,
                    "source": source,
                    "descriptor": descriptor,
                    "section": section,
                    "content_type": content_type,
                    "encoding": encoding,
                    "octets": octets,
                }
            )
        return tuple(result)

    def _remove_unlisted_images_locked(
        self,
        account_name: str,
        uid: str,
        resources: Iterable[Mapping[str, object]],
    ) -> None:
        keep = {str(resource["id"]) for resource in resources}
        rows = self.connection.execute(
            "SELECT resource_id FROM message_images WHERE account_name=? AND uid=?",
            (account_name, int(uid)),
        ).fetchall()
        removed = [str(row["resource_id"]) for row in rows if str(row["resource_id"]) not in keep]
        if removed:
            self.connection.executemany(
                "DELETE FROM message_images WHERE account_name=? AND uid=? AND resource_id=?",
                ((account_name, int(uid), resource_id) for resource_id in removed),
            )
        for resource_id in tuple(
            item[2]
            for item in self._image_memory
            if item[0] == account_name and item[1] == int(uid) and item[2] not in keep
        ):
            self._drop_memory_images_locked(
                account_name=account_name, uid=uid, resource_id=resource_id
            )

    def _store_memory_image_locked(self, image: CachedImage) -> None:
        key = (image.account_name, int(image.uid), image.resource_id)
        previous = self._image_memory.pop(key, None)
        if previous is not None:
            self._image_memory_bytes -= previous.size
        self._image_memory[key] = image
        self._image_memory_bytes += image.size
        while self._image_memory and self._image_memory_bytes > self.image_memory_limit_bytes:
            _key, removed = self._image_memory.popitem(last=False)
            self._image_memory_bytes -= removed.size

    def _drop_memory_images_locked(
        self,
        *,
        account_name: str | None = None,
        uid: str | None = None,
        resource_id: str | None = None,
    ) -> None:
        for key, image in tuple(self._image_memory.items()):
            if account_name is not None and key[0] != account_name:
                continue
            if uid is not None and key[1] != int(uid):
                continue
            if resource_id is not None and key[2] != resource_id:
                continue
            self._image_memory.pop(key, None)
            self._image_memory_bytes -= image.size

    def _prune_image_cache_locked(self, max_bytes: int | None = None) -> int:
        limit = self.image_cache_limit_bytes if max_bytes is None else max(0, max_bytes)
        total = int(
            self.connection.execute(
                "SELECT COALESCE(SUM(size), 0) FROM message_images"
            ).fetchone()[0]
        )
        removed = 0
        while total > limit:
            row = self.connection.execute(
                """
                SELECT account_name, uid, resource_id, size FROM message_images
                ORDER BY accessed_at ASC, created_at ASC LIMIT 1
                """
            ).fetchone()
            if row is None:
                break
            self.connection.execute(
                "DELETE FROM message_images WHERE account_name=? AND uid=? AND resource_id=?",
                (str(row["account_name"]), int(row["uid"]), str(row["resource_id"])),
            )
            self._drop_memory_images_locked(
                account_name=str(row["account_name"]),
                uid=str(row["uid"]),
                resource_id=str(row["resource_id"]),
            )
            total -= int(row["size"])
            removed += 1
        return removed

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> CachedMessage:
        return CachedMessage(
            account_name=str(row["account_name"]),
            uid=str(row["uid"]),
            subject=str(row["subject"]),
            sender=str(row["sender"]),
            recipients=str(row["recipients"]),
            date=str(row["date_text"]),
            received_at=float(row["received_at"]),
            size=int(row["size"]),
            unread=bool(row["unread"]),
        )
