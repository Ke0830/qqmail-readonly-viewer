import base64
import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image

import mail_cache
from mail_cache import (
    BODY_CACHE_ENVELOPE_VERSION,
    CacheSettings,
    CacheStore,
    CachedMessage,
    default_cache_path,
)
from mail_html import HTML_POLICY_VERSION
from mail_mime import parse_bodystructure, safe_text_parts
from mail_sync import SyncManager
from mail_translation import TranslationConfig, translation_source_digest
from qqmail_viewer import (
    Account,
    PROCESS_CSRF_TOKEN,
    QQMailClient,
    ViewerError,
    ViewerHandler,
    ViewerRuntime,
    _cache_encryption_key,
    build_parser,
    cached_errors,
    list_cli,
)


def _summary(uid: str, *, unread: bool = True) -> dict[str, object]:
    return {
        "uid": uid,
        "subject": f"message {uid}",
        "sender": f"sender{uid}@example.com",
        "recipients": "reader@example.com",
        "date": f"2026-08-05 10:{int(uid) % 60:02d}",
        "received_at": float(uid),
        "size": int(uid) * 10,
        "unread": unread,
        "attachments": (),
    }


def _web_detail(
    uid: str, *, image_resources: tuple[dict[str, object], ...] = ()
) -> SimpleNamespace:
    return SimpleNamespace(
        uid=str(uid),
        subject=f"message {uid}",
        sender="sender@example.com",
        recipients="reader@example.com",
        date="2026-08-05 10:00",
        text=f"body {uid}",
        attachments=(),
        body_format="html",
        safe_html=f"<p>body {uid}</p>",
        blocked_images=0,
        html_policy=HTML_POLICY_VERSION,
        image_resources=image_resources,
    )


def _remote_image_resource(resource_id: str) -> dict[str, object]:
    return {
        "id": resource_id,
        "source_type": "remote",
        "source": f"https://images.example/{resource_id}.png",
        "descriptor": "",
        "section": "",
        "content_type": "image/png",
        "encoding": "",
        "octets": None,
    }


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _legacy_body_ciphertext(
    key: bytes, account_name: str, uid: str, text: str
) -> tuple[bytes, bytes]:
    if mail_cache.AESGCM is None:
        raise RuntimeError("cryptography is not installed")
    nonce = os.urandom(12)
    associated = f"{account_name}:{uid}".encode("utf-8")
    ciphertext = mail_cache.AESGCM(key).encrypt(
        nonce, text.encode("utf-8"), associated
    )
    return nonce, ciphertext


class CacheStoreTests(unittest.TestCase):
    def test_default_cache_paths(self):
        home = Path("/Users/person")
        self.assertEqual(
            default_cache_path("darwin", home),
            home / "Library/Caches/local-readonly-mail-viewer/mail-cache.sqlite3",
        )
        self.assertEqual(
            default_cache_path("win32", Path("C:/Users/person"), "D:/Local"),
            Path("D:/Local/local-readonly-mail-viewer/mail-cache.sqlite3"),
        )

    def test_sqlite_wal_indexes_pagination_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mail.sqlite3"
            cache = CacheStore(path, CacheSettings("metadata", 3), b"k" * 32)
            cache.upsert_messages("a", [_summary("1"), _summary("3", unread=False)])
            cache.upsert_messages("b", [_summary("2")])
            page = cache.query_page(("a", "b"), unread_only=True, limit=1)
            self.assertEqual((page.total, page.messages[0].uid), (2, "2"))
            self.assertEqual(
                cache.connection.execute("PRAGMA journal_mode").fetchone()[0], "wal"
            )
            indexes = {
                row[1]
                for row in cache.connection.execute("PRAGMA index_list(messages)")
            }
            self.assertIn("messages_account_unread_date", indexes)
            self.assertIn("messages_unread_date", indexes)
            cache.close()

            reopened = CacheStore(path, CacheSettings("metadata", 3), b"k" * 32)
            restored = reopened.query_messages(
                ("a", "b"), unread_only=False, limit=None
            )
            self.assertEqual([item.uid for item in restored], ["3", "2", "1"])
            reopened.close()

    def test_schema_migrates_missing_optional_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mail.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE messages (
                    account_name TEXT NOT NULL,
                    uid INTEGER NOT NULL,
                    subject TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    date_text TEXT NOT NULL,
                    received_at REAL NOT NULL,
                    size INTEGER NOT NULL DEFAULT 0,
                    unread INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(account_name, uid)
                );
                CREATE TABLE sync_state (account_name TEXT PRIMARY KEY);
                """
            )
            connection.close()
            cache = CacheStore(path, CacheSettings("metadata", 3), b"k" * 32)
            message_columns = {
                row[1] for row in cache.connection.execute("PRAGMA table_info(messages)")
            }
            state_columns = {
                row[1] for row in cache.connection.execute("PRAGMA table_info(sync_state)")
            }
            self.assertIn("body_ciphertext", message_columns)
            self.assertIn("attachments_json", message_columns)
            self.assertIn("uidvalidity", state_columns)
            self.assertIn("full_sync_complete", state_columns)
            cache.close()

    def test_memory_mode_does_not_create_database_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mail.sqlite3"
            cache = CacheStore(path, CacheSettings("memory", 3), b"k" * 32)
            cache.upsert_messages("a", [_summary("1")])
            self.assertFalse(path.exists())
            cache.close()

    def test_metadata_mode_never_returns_cached_body(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = CacheStore(
                Path(directory) / "mail.sqlite3",
                CacheSettings("metadata", 3),
                b"k" * 32,
            )
            cache.upsert_messages("a", [_summary("1")])
            cache.store_detail(
                "a", "1", recipients="reader@example.com", text="secret", attachments=("a.pdf",)
            )
            self.assertIsNone(cache.cached_detail("a", "1"))
            row = cache.connection.execute(
                "SELECT body_ciphertext, attachments_json FROM messages"
            ).fetchone()
            self.assertIsNone(row[0])
            self.assertEqual(row[1], '["a.pdf"]')
            cache.close()

    @unittest.skipIf(mail_cache.AESGCM is None, "cryptography is not installed")
    def test_aes_gcm_body_round_trip_restart_and_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mail.sqlite3"
            key = bytes(range(32))
            cache = CacheStore(path, CacheSettings("body", 3), key)
            cache.upsert_messages("a", [_summary("1")])
            cache.store_detail(
                "a",
                "1",
                recipients="reader@example.com",
                text="私密正文",
                attachments=("a.pdf",),
                body_format="html",
                safe_html="<p style=\"color:#123456\">私密正文</p>",
                blocked_images=2,
                html_policy=HTML_POLICY_VERSION,
            )
            row = cache.connection.execute(
                "SELECT body_nonce, body_ciphertext FROM messages"
            ).fetchone()
            raw = bytes(row[1])
            self.assertNotIn("私密正文".encode(), raw)
            self.assertNotIn(b"safe_html", raw)
            envelope = json.loads(
                mail_cache.AESGCM(key)
                .decrypt(bytes(row[0]), raw, b"a:1:body-v2")
                .decode("utf-8")
            )
            self.assertEqual(
                envelope,
                {
                    "v": BODY_CACHE_ENVELOPE_VERSION,
                    "policy": HTML_POLICY_VERSION,
                    "format": "html",
                    "text": "私密正文",
                    "safe_html": '<p style="color:#123456">私密正文</p>',
                    "blocked_images": 2,
                },
            )
            cache.close()

            reopened = CacheStore(path, CacheSettings("body", 3), key)
            detail = reopened.cached_detail("a", "1")
            self.assertIsNotNone(detail)
            self.assertEqual(
                (
                    detail.text,
                    detail.attachments,
                    detail.body_format,
                    detail.safe_html,
                    detail.blocked_images,
                    detail.html_policy,
                ),
                (
                    "私密正文",
                    ("a.pdf",),
                    "html",
                    '<p style="color:#123456">私密正文</p>',
                    2,
                    HTML_POLICY_VERSION,
                ),
            )
            reopened.connection.execute(
                "UPDATE messages SET body_ciphertext=?", (sqlite3.Binary(b"damaged"),)
            )
            reopened.connection.commit()
            self.assertIsNone(reopened.cached_detail("a", "1"))
            self.assertIsNone(
                reopened.connection.execute("SELECT body_ciphertext FROM messages").fetchone()[0]
            )
            reopened.close()

    @unittest.skipIf(mail_cache.AESGCM is None, "cryptography is not installed")
    def test_image_cache_is_encrypted_persistent_and_cleared_with_bodies(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mail.sqlite3"
            key = bytes(range(32))
            cache = CacheStore(path, CacheSettings("body", 3), key)
            cache.upsert_messages("a", [_summary("1")])
            cache.store_image(
                "a",
                "1",
                "r1",
                mime_type="image/png",
                source_type="cid",
                source_digest="a" * 64,
                data=b"private-image-bytes",
            )
            row = cache.connection.execute(
                "SELECT image_ciphertext FROM message_images"
            ).fetchone()
            self.assertNotIn(b"private-image-bytes", bytes(row[0]))
            cache.close()

            reopened = CacheStore(path, CacheSettings("body", 3), key)
            image = reopened.load_image("a", "1", "r1", source_digest="a" * 64)
            self.assertIsNotNone(image)
            self.assertEqual(image.data, b"private-image-bytes")
            reopened.clear_bodies()
            self.assertIsNone(reopened.load_image("a", "1", "r1"))
            self.assertEqual(reopened.image_bytes_for_message("a", "1"), 0)
            reopened.close()

    def test_runtime_materializes_only_local_image_tokens_and_caches_data_images(self):
        with tempfile.TemporaryDirectory() as directory:
            account = Account(
                "a",
                "qq",
                "reader@example.com",
                "imap.qq.com",
                993,
                "service.email",
                "service.secret",
                True,
            )
            runtime = ViewerRuntime(
                periodic=False,
                accounts=(account,),
                settings=CacheSettings("memory", 3),
                cache_file=Path(directory) / "mail.sqlite3",
                encryption_key=b"z" * 32,
            )
            try:
                runtime.cache.upsert_messages("a", [_summary("1")])
                output = io.BytesIO()
                Image.new("RGB", (2, 1), (1, 2, 3)).save(output, format="PNG")
                source = "data:image/png;base64," + base64.b64encode(
                    output.getvalue()
                ).decode("ascii")
                detail = mail_cache.CachedDetail(
                    runtime.cache.message("a", "1"),
                    "body",
                    (),
                    body_format="html",
                    safe_html='<span class="mail-image-placeholder" data-mail-image-ids="r1">图片未加载</span>',
                    image_resources=(
                        {
                            "id": "r1",
                            "source_type": "data",
                            "source": source,
                            "descriptor": "",
                            "section": "",
                            "content_type": "",
                            "encoding": "",
                            "octets": None,
                        },
                    ),
                )
                rendered = runtime.materialize_html(
                    "a",
                    "1",
                    SimpleNamespace(
                        safe_html=detail.safe_html,
                        image_resources=detail.image_resources,
                    ),
                )
                self.assertIn('/message-image/', rendered)
                self.assertNotIn(source, rendered)
                token = rendered.split('/message-image/', 1)[1].split('"', 1)[0]
                mime_type, image = runtime.image_for_token(token)
                self.assertEqual(mime_type, "image/png")
                self.assertEqual(image, output.getvalue())
                self.assertEqual(runtime.cache.image_bytes_for_message("a", "1"), len(image))
            finally:
                runtime.close()

    @unittest.skipIf(mail_cache.AESGCM is None, "cryptography is not installed")
    def test_opening_web_view_prefetches_body_and_data_image(self):
        account = Account(
            "a",
            "qq",
            "reader@example.com",
            "imap.qq.com",
            993,
            "service.email",
            "service.secret",
            True,
        )
        output = io.BytesIO()
        Image.new("RGB", (2, 1), (1, 2, 3)).save(output, format="PNG")
        source = "data:image/png;base64," + base64.b64encode(
            output.getvalue()
        ).decode("ascii")
        resource = {
            "id": "r1",
            "source_type": "data",
            "source": source,
            "descriptor": "",
            "section": "",
            "content_type": "image/png",
            "encoding": "base64",
            "octets": len(output.getvalue()),
        }
        detail_calls: list[tuple[str, bool]] = []

        class PrefetchClient:
            def connect(self):
                return self

            def noop(self):
                pass

            def close(self):
                pass

            def get_message(self, uid, *, prefer_html=False):
                detail_calls.append((str(uid), prefer_html))
                return _web_detail(str(uid), image_resources=(resource,))

        with tempfile.TemporaryDirectory() as directory, patch(
            "qqmail_viewer.configured_client", return_value=PrefetchClient()
        ):
            runtime = ViewerRuntime(
                periodic=False,
                accounts=(account,),
                settings=CacheSettings("memory", 3),
                cache_file=Path(directory) / "mail.sqlite3",
                encryption_key=b"z" * 32,
            )
            try:
                runtime.cache.upsert_messages("a", [_summary("1", unread=False)])
                runtime.cache.mark_seeded("a", True)
                runtime.cache.mark_seeded("a", False)
                runtime.cache.mark_sync_success(
                    "a", uidvalidity="1", highest_uid=1, full_sync_complete=True
                )
                handler = object.__new__(ViewerHandler)
                errors = handler._prepare_cache(
                    runtime,
                    ("a",),
                    unread_only=False,
                    limit=30,
                    refresh=False,
                )
                self.assertEqual(errors, ())
                self.assertTrue(
                    _wait_until(
                        lambda: runtime.cache.load_image(
                            "a", "1", "r1", touch=False
                        )
                        is not None
                    )
                )
                self.assertEqual(detail_calls, [("1", True)])
                self.assertEqual(
                    runtime.cache.load_image("a", "1", "r1", touch=False).data,
                    output.getvalue(),
                )
            finally:
                runtime.close()

    def test_runtime_prefetch_only_schedules_incomplete_account_indexes(self):
        runtime = object.__new__(ViewerRuntime)
        runtime.sync = MagicMock()

        runtime.start_prefetch()

        runtime.sync.kick_background.assert_called_once_with(
            30, incomplete_only=True
        )
        runtime.sync.start_prefetch.assert_called_once_with()

    @unittest.skipIf(mail_cache.AESGCM is None, "cryptography is not installed")
    def test_legacy_plaintext_ciphertext_is_read_as_v1_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = CacheStore(
                Path(directory) / "mail.sqlite3",
                CacheSettings("body", 3),
                bytes(range(32)),
            )
            cache.upsert_messages("a", [_summary("1")])
            nonce, ciphertext = _legacy_body_ciphertext(
                bytes(range(32)), "a", "1", "旧版纯文本正文"
            )
            cache.connection.execute(
                """
                UPDATE messages SET attachments_json=?, body_nonce=?, body_ciphertext=?
                WHERE account_name=? AND uid=?
                """,
                (
                    '["legacy.pdf"]',
                    sqlite3.Binary(nonce),
                    sqlite3.Binary(ciphertext),
                    "a",
                    1,
                ),
            )
            cache.connection.commit()

            detail = cache.cached_detail("a", "1")
            self.assertIsNotNone(detail)
            self.assertEqual(detail.text, "旧版纯文本正文")
            self.assertEqual(detail.attachments, ("legacy.pdf",))
            self.assertEqual(detail.body_format, "plain")
            self.assertEqual(detail.safe_html, "")
            self.assertEqual(detail.blocked_images, 0)
            self.assertEqual(detail.html_policy, "")
            cache.close()

    @unittest.skipIf(mail_cache.AESGCM is None, "cryptography is not installed")
    def test_legacy_ciphertext_that_looks_like_v2_remains_plain_text(self):
        with tempfile.TemporaryDirectory() as directory:
            key = bytes(range(32))
            cache = CacheStore(
                Path(directory) / "mail.sqlite3",
                CacheSettings("body", 3),
                key,
            )
            cache.upsert_messages("a", [_summary("1")])
            v2_shaped_text = json.dumps(
                {
                    "v": BODY_CACHE_ENVELOPE_VERSION,
                    "policy": HTML_POLICY_VERSION,
                    "format": "html",
                    "text": "伪装正文",
                    "safe_html": "<script>alert(1)</script>",
                    "blocked_images": 4,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            nonce, ciphertext = _legacy_body_ciphertext(
                key, "a", "1", v2_shaped_text
            )
            cache.connection.execute(
                """
                UPDATE messages SET body_nonce=?, body_ciphertext=?
                WHERE account_name=? AND uid=?
                """,
                (
                    sqlite3.Binary(nonce),
                    sqlite3.Binary(ciphertext),
                    "a",
                    1,
                ),
            )
            cache.connection.commit()

            detail = cache.cached_detail("a", "1")

            self.assertIsNotNone(detail)
            self.assertEqual(detail.text, v2_shaped_text)
            self.assertEqual(detail.body_format, "plain")
            self.assertEqual(detail.safe_html, "")
            self.assertEqual(detail.blocked_images, 0)
            self.assertEqual(detail.html_policy, "")
            cache.close()

    @unittest.skipIf(mail_cache.AESGCM is None, "cryptography is not installed")
    def test_authenticated_but_invalid_v2_envelope_is_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            key = bytes(range(32))
            cache = CacheStore(
                Path(directory) / "mail.sqlite3",
                CacheSettings("body", 3),
                key,
            )
            cache.upsert_messages("a", [_summary("1")])
            nonce = os.urandom(12)
            ciphertext = mail_cache.AESGCM(key).encrypt(
                nonce,
                b"not-a-versioned-json-envelope",
                b"a:1:body-v2",
            )
            cache.connection.execute(
                """
                UPDATE messages SET body_nonce=?, body_ciphertext=?
                WHERE account_name=? AND uid=?
                """,
                (
                    sqlite3.Binary(nonce),
                    sqlite3.Binary(ciphertext),
                    "a",
                    1,
                ),
            )
            cache.connection.commit()

            self.assertIsNone(cache.cached_detail("a", "1"))
            row = cache.connection.execute(
                "SELECT body_nonce, body_ciphertext FROM messages"
            ).fetchone()
            self.assertEqual(tuple(row), (None, None))
            cache.close()

    def test_encrypted_body_cache_wiring_without_external_dependency(self):
        class TestAEAD:
            def __init__(self, key):
                self.key = key

            def encrypt(self, nonce, payload, associated):
                return associated + b":" + payload[::-1]

            def decrypt(self, nonce, ciphertext, associated):
                prefix = associated + b":"
                if not ciphertext.startswith(prefix):
                    raise ValueError("authentication failed")
                return ciphertext[len(prefix) :][::-1]

        with tempfile.TemporaryDirectory() as directory, patch(
            "mail_cache.AESGCM", TestAEAD
        ):
            cache = CacheStore(
                Path(directory) / "mail.sqlite3",
                CacheSettings("body", 3),
                b"k" * 32,
            )
            cache.upsert_messages("a", [_summary("1")])
            cache.store_detail(
                "a", "1", recipients="reader@example.com", text="body", attachments=()
            )
            self.assertEqual(cache.cached_detail("a", "1").text, "body")
            cache.connection.execute(
                "UPDATE messages SET body_ciphertext=? WHERE account_name='a' AND uid=1",
                (sqlite3.Binary(b"damaged"),),
            )
            cache.connection.commit()
            self.assertIsNone(cache.cached_detail("a", "1"))
            cache.close()

    def test_missing_or_invalid_cache_key_is_replaced(self):
        values: dict[str, str] = {}
        with patch(
            "qqmail_viewer._keychain_get_optional",
            side_effect=lambda service: values.get(service),
        ), patch(
            "qqmail_viewer.keychain_set",
            side_effect=lambda service, value: values.__setitem__(service, value),
        ):
            first, created = _cache_encryption_key()
            second, created_again = _cache_encryption_key()
            self.assertTrue(created)
            self.assertFalse(created_again)
            self.assertEqual(first, second)
            values[next(iter(values))] = "not-base64"
            replacement, replaced = _cache_encryption_key()
            self.assertTrue(replaced)
            self.assertEqual(len(replacement), 32)


class RichBodyCacheRuntimeTests(unittest.TestCase):
    @unittest.skipIf(mail_cache.AESGCM is None, "cryptography is not installed")
    def test_current_policy_plain_cache_satisfies_web_detail_without_refetch(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = CacheStore(
                Path(directory) / "mail.sqlite3",
                CacheSettings("body", 3),
                bytes(range(32)),
            )
            cache.upsert_messages("a", [_summary("1")])
            cache.store_detail(
                "a",
                "1",
                recipients="reader@example.com",
                text="已确认没有安全 HTML 的纯文本",
                attachments=("note.txt",),
                body_format="plain",
                html_policy=HTML_POLICY_VERSION,
            )
            fetch_detail = MagicMock()
            runtime = object.__new__(ViewerRuntime)
            runtime.cache = cache
            runtime.sync = SimpleNamespace(fetch_detail=fetch_detail)

            detail = runtime.message_detail("a", "1", prefer_html=True)

            self.assertEqual(detail.text, "已确认没有安全 HTML 的纯文本")
            self.assertEqual(detail.attachments, ("note.txt",))
            self.assertEqual(detail.body_format, "plain")
            self.assertEqual(detail.html_policy, HTML_POLICY_VERSION)
            fetch_detail.assert_not_called()
            cache.close()

    @unittest.skipIf(mail_cache.AESGCM is None, "cryptography is not installed")
    def test_legacy_and_stale_policy_cache_refetch_html_for_web(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = CacheStore(
                Path(directory) / "mail.sqlite3",
                CacheSettings("body", 3),
                bytes(range(32)),
            )
            cache.upsert_messages("a", [_summary("1"), _summary("2")])
            legacy_nonce, legacy_ciphertext = _legacy_body_ciphertext(
                bytes(range(32)), "a", "1", "旧版纯文本"
            )
            cache.connection.execute(
                """
                UPDATE messages SET body_nonce=?, body_ciphertext=?
                WHERE account_name=? AND uid=?
                """,
                (
                    sqlite3.Binary(legacy_nonce),
                    sqlite3.Binary(legacy_ciphertext),
                    "a",
                    1,
                ),
            )
            cache.connection.commit()
            cache.store_detail(
                "a",
                "2",
                recipients="reader@example.com",
                text="旧策略正文",
                attachments=(),
                body_format="plain",
                html_policy="mail-html-v0",
            )

            refreshed = {
                uid: SimpleNamespace(uid=uid, text=f"新正文 {uid}")
                for uid in ("1", "2")
            }
            fetch_detail = MagicMock(
                side_effect=lambda account, uid, *, prefer_html: refreshed[uid]
            )
            runtime = object.__new__(ViewerRuntime)
            runtime.cache = cache
            runtime.sync = SimpleNamespace(fetch_detail=fetch_detail)

            self.assertIs(runtime.message_detail("a", "1", prefer_html=True), refreshed["1"])
            self.assertIs(runtime.message_detail("a", "2", prefer_html=True), refreshed["2"])
            self.assertEqual(
                [item.args for item in fetch_detail.call_args_list],
                [("a", "1"), ("a", "2")],
            )
            self.assertTrue(
                all(
                    item.kwargs == {"prefer_html": True}
                    for item in fetch_detail.call_args_list
                )
            )
            cache.close()

    @unittest.skipIf(mail_cache.AESGCM is None, "cryptography is not installed")
    def test_failed_rich_refresh_falls_back_to_stale_plain_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = CacheStore(
                Path(directory) / "mail.sqlite3",
                CacheSettings("body", 3),
                bytes(range(32)),
            )
            cache.upsert_messages("a", [_summary("1")])
            cache.store_detail(
                "a",
                "1",
                recipients="old-reader@example.com",
                text="仍可阅读的旧纯文本",
                attachments=("old.pdf",),
                body_format="plain",
                html_policy="mail-html-v0",
            )
            failed = SimpleNamespace(
                cacheable=False,
                body_format="unavailable",
                recipients="new-reader@example.com",
                attachments=("new.pdf",),
            )
            runtime = object.__new__(ViewerRuntime)
            runtime.cache = cache
            runtime.sync = SimpleNamespace(
                fetch_detail=MagicMock(return_value=failed)
            )

            detail = runtime.message_detail("a", "1", prefer_html=True)

            self.assertEqual(detail.text, "仍可阅读的旧纯文本")
            self.assertEqual(detail.recipients, "new-reader@example.com")
            self.assertEqual(detail.attachments, ("new.pdf",))
            self.assertEqual(detail.body_format, "plain")
            cache.close()


class MimeSectionTests(unittest.TestCase):
    STRUCTURE = (
        b'1 (UID 7 BODYSTRUCTURE (("TEXT" "PLAIN" ("CHARSET" "UTF-8") '
        b'NIL NIL "7BIT" 5 1 NIL NIL NIL NIL)("APPLICATION" "PDF" '
        b'("NAME" "report.pdf") NIL NIL "BASE64" 100 NIL '
        b'("ATTACHMENT" ("FILENAME" "report.pdf")) NIL NIL) '
        b'"MIXED" ("BOUNDARY" "x") NIL NIL NIL))'
    )

    def test_bodystructure_selects_text_and_lists_attachment(self):
        text_parts, attachments = safe_text_parts(parse_bodystructure([self.STRUCTURE]))
        self.assertEqual([part.section for part in text_parts], ["1"])
        self.assertEqual(attachments, ("report.pdf",))

    def test_priority_uids_prefers_sort_and_falls_back_to_highest_uid(self):
        class SortIMAP:
            def __init__(self, sort_status):
                self.sort_status = sort_status
                self.commands = []

            def uid(self, command, *args):
                self.commands.append((command, args))
                if command == "sort":
                    return self.sort_status, [b"9 7 8"]
                if command == "search":
                    return "OK", [b"1 20 3"]
                raise AssertionError(command)

        sorted_connection = SortIMAP("OK")
        client = QQMailClient("reader@example.com", "secret")
        client.connection = sorted_connection
        self.assertEqual(
            client.priority_uids(unread_only=True, limit=2), ["9", "7"]
        )
        self.assertEqual([item[0] for item in sorted_connection.commands], ["sort"])

        fallback_connection = SortIMAP("NO")
        client.connection = fallback_connection
        self.assertEqual(
            client.priority_uids(unread_only=False, limit=2), ["20", "3"]
        )
        self.assertEqual(
            [item[0] for item in fallback_connection.commands], ["sort", "search"]
        )

    def test_detail_fetches_only_selected_text_section(self):
        class DetailIMAP:
            def __init__(self):
                self.queries: list[str] = []

            def uid(self, command, uid, query):
                self.queries.append(query)
                if "HEADER.FIELDS" in query:
                    return "OK", [(
                        b"1 (UID 7)",
                        b"Subject: Safe\r\nFrom: sender@example.com\r\nTo: reader@example.com\r\nDate: Wed, 05 Aug 2026 10:00:00 +0800\r\n\r\n",
                    )]
                if query == "(BODYSTRUCTURE)":
                    return "OK", [MimeSectionTests.STRUCTURE]
                if query == "(BODY.PEEK[1])":
                    return "OK", [(b"1 (UID 7)", b"hello")]
                raise AssertionError(f"attachment or full-message payload requested: {query}")

        connection = DetailIMAP()
        client = QQMailClient("reader@example.com", "secret")
        client.connection = connection
        detail = client.get_message("7")
        self.assertEqual(detail.text, "hello")
        self.assertEqual(detail.attachments, ("report.pdf",))
        self.assertNotIn("BODY.PEEK[]", connection.queries)
        self.assertFalse(any("[2]" in query for query in connection.queries))

    def test_malformed_structure_never_falls_back_to_full_message(self):
        class BrokenIMAP:
            def __init__(self):
                self.queries: list[str] = []

            def uid(self, command, uid, query):
                self.queries.append(query)
                if "HEADER.FIELDS" in query:
                    return "OK", [(b"1 (UID 7)", b"Subject: Broken\r\n\r\n")]
                return "OK", [b"1 (UID 7 BODYSTRUCTURE broken)"]

        connection = BrokenIMAP()
        client = QQMailClient("reader@example.com", "secret")
        client.connection = connection
        detail = client.get_message("7")
        self.assertEqual(detail.text, "为避免下载附件，正文无法安全读取")
        self.assertNotIn("BODY.PEEK[]", connection.queries)


class _RemoteState:
    def __init__(self, uids, unseen, *, validity="1", delay=0.0):
        self.uids = list(uids)
        self.unseen = set(unseen)
        self.validity = validity
        self.delay = delay
        self.priority_calls: list[tuple[bool, int]] = []
        self.fetch_calls: list[list[str]] = []
        self.detail_calls: list[tuple[str, bool]] = []
        self.connects = 0
        self.noops = 0


class _WorkerClient:
    def __init__(self, state: _RemoteState):
        self.state = state

    def connect(self):
        self.state.connects += 1
        return self

    def noop(self):
        self.state.noops += 1

    def close(self):
        pass

    def uidvalidity(self):
        return self.state.validity

    def priority_uids(self, *, unread_only, limit):
        self.state.priority_calls.append((unread_only, limit))
        if self.state.delay:
            time.sleep(self.state.delay)
        source = self.state.unseen if unread_only else self.state.uids
        return sorted((str(uid) for uid in source), key=int, reverse=True)[:limit]

    def search_uids(self, unread_only):
        source = self.state.unseen if unread_only else self.state.uids
        return sorted((str(uid) for uid in source), key=int)

    def fetch_summaries(self, uids, unread_uids=None):
        values = [str(uid) for uid in uids]
        self.state.fetch_calls.append(values)
        unread = self.state.unseen if unread_uids is None else unread_uids
        return [_summary(uid, unread=uid in unread) for uid in values]

    def get_message(self, uid, *, prefer_html=False):
        self.state.detail_calls.append((str(uid), prefer_html))
        return SimpleNamespace(
            uid=uid,
            subject=f"message {uid}",
            sender="sender@example.com",
            recipients="reader@example.com",
            date="2026-08-05 10:00",
            text="body",
            attachments=(),
        )


class SyncManagerTests(unittest.TestCase):
    def _cache(self, directory):
        return CacheStore(
            Path(directory) / "mail.sqlite3",
            CacheSettings("metadata", 3),
            b"k" * 32,
        )

    def test_accounts_seed_in_parallel_and_only_fetch_page_candidates_first(self):
        accounts = (SimpleNamespace(name="a"), SimpleNamespace(name="b"))
        states = {
            "a": _RemoteState(range(1, 101), {98, 99, 100}, delay=0.15),
            "b": _RemoteState(range(1, 101), {97, 99, 100}, delay=0.15),
        }
        with tempfile.TemporaryDirectory() as directory:
            cache = self._cache(directory)
            manager = SyncManager(
                accounts,
                cache,
                CacheSettings("metadata", 3),
                lambda account: _WorkerClient(states[account.name]),
                periodic=False,
            )
            started = time.monotonic()
            errors = manager.ensure_seed(
                ("a", "b"), unread_only=True, limit=2, timeout=1
            )
            elapsed = time.monotonic() - started
            self.assertFalse(errors)
            self.assertLess(elapsed, 0.28)
            self.assertEqual(states["a"].fetch_calls[0], ["100", "99"])
            self.assertEqual(states["b"].fetch_calls[0], ["100", "99"])
            manager.stop()
            cache.close()

    def test_cold_seed_timeout_returns_while_slow_worker_continues(self):
        account = SimpleNamespace(name="slow")
        state = _RemoteState(range(1, 5), {4}, delay=0.2)
        with tempfile.TemporaryDirectory() as directory:
            cache = self._cache(directory)
            manager = SyncManager(
                (account,),
                cache,
                CacheSettings("metadata", 3),
                lambda account: _WorkerClient(state),
                periodic=False,
            )
            started = time.monotonic()
            manager.ensure_seed(("slow",), unread_only=True, limit=30, timeout=0.03)
            self.assertLess(time.monotonic() - started, 0.12)
            manager.stop()
            cache.close()

    def test_incremental_add_delete_flags_uidvalidity_and_connection_reuse(self):
        account = SimpleNamespace(name="a")
        state = _RemoteState(["1", "2"], {"2"})
        factory_calls = 0

        def factory(_account):
            nonlocal factory_calls
            factory_calls += 1
            return _WorkerClient(state)

        with tempfile.TemporaryDirectory() as directory:
            cache = self._cache(directory)
            manager = SyncManager(
                (account,), cache, CacheSettings("metadata", 3), factory, periodic=False
            )
            self.assertFalse(manager.sync_accounts(("a",), wait=True, force=True))
            self.assertTrue(cache.message("a", "2").unread)

            state.uids = ["2", "3"]
            state.unseen = {"3"}
            self.assertFalse(manager.sync_accounts(("a",), wait=True, force=True))
            self.assertIsNone(cache.message("a", "1"))
            self.assertFalse(cache.message("a", "2").unread)
            self.assertTrue(cache.message("a", "3").unread)
            self.assertEqual(factory_calls, 1)
            self.assertGreaterEqual(state.noops, 1)

            state.validity = "2"
            state.uids = ["9"]
            state.unseen = {"9"}
            self.assertFalse(manager.sync_accounts(("a",), wait=True, force=True))
            self.assertEqual(cache.account_uids("a"), {"9"})
            manager.stop()
            cache.close()

    def test_zero_refresh_interval_skips_automatic_but_allows_manual_sync(self):
        account = SimpleNamespace(name="a")
        state = _RemoteState(["1"], {"1"})
        with tempfile.TemporaryDirectory() as directory:
            cache = self._cache(directory)
            manager = SyncManager(
                (account,),
                cache,
                CacheSettings("metadata", 0),
                lambda account: _WorkerClient(state),
                periodic=False,
            )
            manager.sync_accounts(("a",), wait=False, force=False)
            time.sleep(0.02)
            self.assertEqual(state.connects, 0)
            self.assertFalse(manager.sync_accounts(("a",), wait=True, force=True))
            self.assertEqual(state.connects, 1)
            manager.stop()
            cache.close()

    def test_connection_failure_reconnects_with_bounded_backoff(self):
        account = SimpleNamespace(name="a")
        state = _RemoteState(["1"], {"1"})
        factory_calls = 0

        class FailingClient(_WorkerClient):
            def __init__(self, remote, should_fail):
                super().__init__(remote)
                self.should_fail = should_fail

            def connect(self):
                if self.should_fail:
                    raise OSError("temporary network failure")
                return super().connect()

        def factory(_account):
            nonlocal factory_calls
            factory_calls += 1
            return FailingClient(state, factory_calls < 3)

        with tempfile.TemporaryDirectory() as directory, patch(
            "mail_sync._AccountWorker._stop_event_wait", return_value=False
        ):
            cache = self._cache(directory)
            manager = SyncManager(
                (account,), cache, CacheSettings("metadata", 3), factory, periodic=False
            )
            self.assertFalse(manager.sync_accounts(("a",), wait=True, force=True))
            self.assertEqual(factory_calls, 3)
            self.assertEqual(cache.account_uids("a"), {"1"})
            manager.stop()
            cache.close()

    def test_detail_worker_forwards_prefer_html_to_account_client(self):
        account = SimpleNamespace(name="a")
        state = _RemoteState(["1"], {"1"})
        with tempfile.TemporaryDirectory() as directory:
            cache = self._cache(directory)
            manager = SyncManager(
                (account,),
                cache,
                CacheSettings("metadata", 3),
                lambda _account: _WorkerClient(state),
                periodic=False,
            )

            detail = manager.fetch_detail("a", "1", prefer_html=True)

            self.assertEqual(detail.text, "body")
            self.assertEqual(state.detail_calls, [("1", True)])
            manager.stop()
            cache.close()

    @unittest.skipIf(mail_cache.AESGCM is None, "cryptography is not installed")
    def test_prefetches_read_and_unread_newest_first_then_images(self):
        account = SimpleNamespace(name="a")
        state = _RemoteState(["1", "2", "3"], {"2"})
        image_calls: list[tuple[str, str]] = []
        completed = threading.Event()

        class PrefetchClient(_WorkerClient):
            def get_message(self, uid, *, prefer_html=False):
                self.state.detail_calls.append((str(uid), prefer_html))
                return _web_detail(
                    str(uid),
                    image_resources=(_remote_image_resource(f"r{uid}"),),
                )

        def prefetch_image(account_name, uid, resource, received_at):
            image_calls.append((str(uid), str(resource["id"])))
            if len(image_calls) == 3:
                completed.set()

        with tempfile.TemporaryDirectory() as directory:
            cache = CacheStore(
                Path(directory) / "mail.sqlite3",
                CacheSettings("body", 3),
                bytes(range(32)),
            )
            cache.upsert_messages(
                "a",
                [
                    _summary("1", unread=False),
                    _summary("2", unread=True),
                    _summary("3", unread=False),
                ],
            )
            manager = SyncManager(
                (account,),
                cache,
                CacheSettings("body", 3),
                lambda _account: PrefetchClient(state),
                periodic=False,
                prefetch_image=prefetch_image,
            )
            try:
                manager.start_prefetch()
                self.assertTrue(completed.wait(2))
                self.assertEqual(
                    state.detail_calls,
                    [("3", True), ("2", True), ("1", True)],
                )
                self.assertEqual(
                    image_calls,
                    [
                        ("3", "r3"),
                        ("2", "r2"),
                        ("1", "r1"),
                    ],
                )
            finally:
                manager.stop()
                cache.close()

    @unittest.skipIf(mail_cache.AESGCM is None, "cryptography is not installed")
    def test_prefetch_runs_accounts_in_parallel(self):
        accounts = (SimpleNamespace(name="a"), SimpleNamespace(name="b"))
        states = {
            "a": _RemoteState(["1"], set()),
            "b": _RemoteState(["1"], {"1"}),
        }
        started: set[str] = set()
        started_lock = threading.Lock()
        both_started = threading.Event()

        class ParallelClient(_WorkerClient):
            def __init__(self, name, remote):
                super().__init__(remote)
                self.name = name

            def get_message(self, uid, *, prefer_html=False):
                self.state.detail_calls.append((str(uid), prefer_html))
                with started_lock:
                    started.add(self.name)
                    if len(started) == 2:
                        both_started.set()
                both_started.wait(1)
                return _web_detail(str(uid))

        with tempfile.TemporaryDirectory() as directory:
            cache = CacheStore(
                Path(directory) / "mail.sqlite3",
                CacheSettings("body", 3),
                bytes(range(32)),
            )
            cache.upsert_messages("a", [_summary("1", unread=False)])
            cache.upsert_messages("b", [_summary("1", unread=True)])
            manager = SyncManager(
                accounts,
                cache,
                CacheSettings("body", 3),
                lambda account: ParallelClient(account.name, states[account.name]),
                periodic=False,
            )
            try:
                manager.start_prefetch()
                self.assertTrue(both_started.wait(1))
                self.assertTrue(
                    _wait_until(
                        lambda: cache.cached_detail("a", "1") is not None
                        and cache.cached_detail("b", "1") is not None
                    )
                )
            finally:
                manager.stop()
                self.assertTrue(
                    all(not thread.is_alive() for thread in manager._prefetch_threads.values())
                )
                cache.close()

    def test_metadata_mode_does_not_start_body_prefetch(self):
        account = SimpleNamespace(name="a")
        state = _RemoteState(["1"], {"1"})
        with tempfile.TemporaryDirectory() as directory:
            cache = self._cache(directory)
            cache.upsert_messages("a", [_summary("1")])
            manager = SyncManager(
                (account,),
                cache,
                CacheSettings("metadata", 3),
                lambda _account: _WorkerClient(state),
                periodic=False,
            )
            manager.start_prefetch()
            self.assertEqual(manager._prefetch_threads, {})
            self.assertEqual(state.detail_calls, [])
            manager.stop()
            cache.close()

    @unittest.skipIf(mail_cache.AESGCM is None, "cryptography is not installed")
    def test_interactive_detail_jumps_ahead_of_remaining_prefetch(self):
        account = SimpleNamespace(name="a")
        state = _RemoteState(["1", "2", "3"], set())
        newest_started = threading.Event()
        release_newest = threading.Event()
        interactive_done = threading.Event()
        interactive_errors: list[Exception] = []

        class PriorityClient(_WorkerClient):
            def get_message(self, uid, *, prefer_html=False):
                self.state.detail_calls.append((str(uid), prefer_html))
                if str(uid) == "3":
                    newest_started.set()
                    release_newest.wait(1)
                return _web_detail(str(uid))

        with tempfile.TemporaryDirectory() as directory:
            cache = CacheStore(
                Path(directory) / "mail.sqlite3",
                CacheSettings("body", 3),
                bytes(range(32)),
            )
            cache.upsert_messages("a", [_summary("1"), _summary("2"), _summary("3")])
            manager = SyncManager(
                (account,),
                cache,
                CacheSettings("body", 3),
                lambda _account: PriorityClient(state),
                periodic=False,
            )

            def fetch_interactive() -> None:
                try:
                    manager.fetch_detail("a", "1", prefer_html=True)
                except Exception as exc:
                    interactive_errors.append(exc)
                finally:
                    interactive_done.set()

            reader = threading.Thread(target=fetch_interactive)
            try:
                manager.start_prefetch()
                self.assertTrue(newest_started.wait(1))
                reader.start()
                self.assertTrue(
                    _wait_until(lambda: manager.workers["a"].jobs.qsize() >= 1)
                )
                release_newest.set()
                self.assertTrue(interactive_done.wait(2))
                self.assertTrue(_wait_until(lambda: len(state.detail_calls) == 3))
                self.assertEqual(interactive_errors, [])
                self.assertEqual(
                    state.detail_calls,
                    [("3", True), ("1", True), ("2", True)],
                )
            finally:
                release_newest.set()
                manager.stop()
                reader.join(timeout=1)
                cache.close()

    @unittest.skipIf(mail_cache.AESGCM is None, "cryptography is not installed")
    def test_failed_prefetch_is_retried_on_next_start(self):
        account = SimpleNamespace(name="a")
        state = _RemoteState(["1"], set())
        image_attempts = 0
        retried = threading.Event()

        class RetryClient(_WorkerClient):
            def get_message(self, uid, *, prefer_html=False):
                self.state.detail_calls.append((str(uid), prefer_html))
                return _web_detail(
                    str(uid), image_resources=(_remote_image_resource("r1"),)
                )

        def prefetch_image(account_name, uid, resource, received_at):
            nonlocal image_attempts
            image_attempts += 1
            if image_attempts == 1:
                raise OSError("temporary image failure")
            retried.set()

        with tempfile.TemporaryDirectory() as directory:
            cache = CacheStore(
                Path(directory) / "mail.sqlite3",
                CacheSettings("body", 3),
                bytes(range(32)),
            )
            cache.upsert_messages("a", [_summary("1")])
            manager = SyncManager(
                (account,),
                cache,
                CacheSettings("body", 3),
                lambda _account: RetryClient(state),
                periodic=False,
                prefetch_image=prefetch_image,
            )
            try:
                manager.start_prefetch()
                self.assertTrue(_wait_until(lambda: image_attempts == 1))
                manager.start_prefetch()
                self.assertTrue(retried.wait(2))
                self.assertEqual(image_attempts, 2)
                self.assertEqual(state.detail_calls, [("1", True)])
            finally:
                manager.stop()
                cache.close()

    @unittest.skipIf(mail_cache.AESGCM is None, "cryptography is not installed")
    def test_failed_detail_preserves_existing_body_and_attachments_without_caching_error(self):
        account = SimpleNamespace(name="a")
        state = _RemoteState(["1", "2"], {"1", "2"})

        class FailedDetailClient(_WorkerClient):
            def get_message(self, uid, *, prefer_html=False):
                self.state.detail_calls.append((str(uid), prefer_html))
                return SimpleNamespace(
                    uid=str(uid),
                    subject=f"message {uid}",
                    sender="sender@example.com",
                    recipients="reader@example.com",
                    date="2026-08-05 10:00",
                    text="为避免下载附件，正文无法安全读取",
                    attachments=(),
                    body_format="unavailable",
                    safe_html="",
                    blocked_images=0,
                    html_policy="",
                    cacheable=False,
                )

        with tempfile.TemporaryDirectory() as directory:
            cache = CacheStore(
                Path(directory) / "mail.sqlite3",
                CacheSettings("body", 3),
                bytes(range(32)),
            )
            first = _summary("1")
            first["attachments"] = ("existing.pdf",)
            second = _summary("2")
            second["attachments"] = ("uncached.zip",)
            cache.upsert_messages("a", [first, second])
            cache.store_detail(
                "a",
                "1",
                recipients="reader@example.com",
                text="原有正文",
                attachments=("existing.pdf",),
                body_format="plain",
                html_policy=HTML_POLICY_VERSION,
            )
            manager = SyncManager(
                (account,),
                cache,
                CacheSettings("body", 3),
                lambda _account: FailedDetailClient(state),
                periodic=False,
            )

            manager.fetch_detail("a", "1", prefer_html=True)
            manager.fetch_detail("a", "2", prefer_html=True)

            existing = cache.cached_detail("a", "1")
            self.assertIsNotNone(existing)
            self.assertEqual(existing.text, "原有正文")
            self.assertEqual(existing.attachments, ("existing.pdf",))
            self.assertIsNone(cache.cached_detail("a", "2"))
            rows = cache.connection.execute(
                """
                SELECT uid, attachments_json, body_ciphertext
                FROM messages WHERE account_name=? ORDER BY uid
                """,
                ("a",),
            ).fetchall()
            self.assertEqual(json.loads(rows[0][1]), ["existing.pdf"])
            self.assertIsNotNone(rows[0][2])
            self.assertEqual(json.loads(rows[1][1]), ["uncached.zip"])
            self.assertIsNone(rows[1][2])
            self.assertEqual(state.detail_calls, [("1", True), ("2", True)])
            manager.stop()
            cache.close()

    def test_cached_error_keeps_last_success_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = self._cache(directory)
            cache.upsert_messages("a", [_summary("1")])
            cache.mark_sync_success(
                "a", uidvalidity="1", highest_uid=1, full_sync_complete=True
            )
            cache.mark_sync_error("a", "network failed")
            runtime = SimpleNamespace(cache=cache)
            errors = cached_errors(runtime, ("a",))
            self.assertEqual(errors[0]["account"], "a")
            self.assertIn("network failed", errors[0]["error"])
            self.assertIn("上次成功同步", errors[0]["error"])
            cache.close()


class TranslationCacheTests(unittest.TestCase):
    @unittest.skipIf(mail_cache.AESGCM is None, "cryptography is not installed")
    def test_body_mode_persists_encrypted_translation_and_invalidates_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mail.sqlite3"
            key = b"t" * 32
            cache = CacheStore(path, CacheSettings("body", 3), key)
            cache.upsert_messages("a", [_summary("1")])
            cached = cache.store_translation(
                "a",
                "1",
                source_digest="a" * 64,
                subject="中文主题",
                text="中文正文",
                safe_html="<p>中文正文</p>",
                source_language="EN",
                provider_fingerprint="b" * 64,
            )
            row = cache.connection.execute(
                "SELECT translation_ciphertext FROM message_translations"
            ).fetchone()
            self.assertNotIn("中文正文".encode(), bytes(row[0]))
            self.assertEqual(
                cache.cached_translation("a", "1", "a" * 64), cached
            )
            cache.close()

            reopened = CacheStore(path, CacheSettings("body", 3), key)
            restored = reopened.cached_translation("a", "1", "a" * 64)
            self.assertIsNotNone(restored)
            self.assertEqual(restored.subject, "中文主题")
            self.assertIsNone(
                reopened.cached_translation("a", "1", "c" * 64)
            )
            self.assertEqual(
                reopened.connection.execute(
                    "SELECT COUNT(*) FROM message_translations"
                ).fetchone()[0],
                0,
            )
            reopened.close()

    @unittest.skipIf(mail_cache.AESGCM is None, "cryptography is not installed")
    def test_memory_and_metadata_modes_keep_translation_only_in_process(self):
        for mode in ("memory", "metadata"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "mail.sqlite3"
                cache = CacheStore(path, CacheSettings(mode, 3), b"m" * 32)
                cache.upsert_messages("a", [_summary("1")])
                cache.store_translation(
                    "a",
                    "1",
                    source_digest="a" * 64,
                    subject="主题",
                    text="正文",
                    safe_html="",
                    source_language="EN",
                    provider_fingerprint="b" * 64,
                )
                self.assertIsNotNone(
                    cache.cached_translation("a", "1", "a" * 64)
                )
                self.assertEqual(
                    cache.connection.execute(
                        "SELECT COUNT(*) FROM message_translations"
                    ).fetchone()[0],
                    0,
                )
                cache.close()

    @unittest.skipIf(mail_cache.AESGCM is None, "cryptography is not installed")
    def test_body_clear_and_translation_only_clear_remove_translations(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = CacheStore(
                Path(directory) / "mail.sqlite3",
                CacheSettings("body", 3),
                b"c" * 32,
            )
            cache.upsert_messages("a", [_summary("1")])
            values = {
                "source_digest": "a" * 64,
                "subject": "主题",
                "text": "正文",
                "safe_html": "",
                "source_language": "EN",
                "provider_fingerprint": "b" * 64,
            }
            cache.store_translation("a", "1", **values)
            cache.clear_translations()
            self.assertIsNone(cache.cached_translation("a", "1", "a" * 64))
            cache.store_translation("a", "1", **values)
            cache.clear_bodies()
            self.assertIsNone(cache.cached_translation("a", "1", "a" * 64))
            cache.close()

    @unittest.skipIf(mail_cache.AESGCM is None, "cryptography is not installed")
    def test_runtime_translates_on_demand_and_reuses_cached_result(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "qqmail_viewer.configured_client"
        ):
            runtime = ViewerRuntime(
                periodic=False,
                accounts=(WebAndSettingsTests.ACCOUNT,),
                settings=CacheSettings("body", 3),
                cache_file=Path(directory) / "mail.sqlite3",
                encryption_key=b"r" * 32,
            )
            runtime._translation_config = TranslationConfig(
                "openai_compatible", "https://api.example.com/v1", "model"
            ).validated()
            runtime._translation_config_loaded = True
            runtime.cache.upsert_messages("a", [_summary("1")])
            runtime.cache.store_detail(
                "a",
                "1",
                recipients="reader@example.com",
                text="Hello body",
                attachments=(),
                body_format="html",
                safe_html="<p>Hello body</p>",
                html_policy=HTML_POLICY_VERSION,
            )
            calls = []

            def transport(_url, body, _headers, _deadline):
                request = json.loads(body)
                segments = json.loads(request["messages"][1]["content"])["segments"]
                calls.append(segments)
                payload = {
                    "source_language": "EN",
                    "translations": [
                        {
                            "id": item["id"],
                            "text": item["text"].replace("message 1", "邮件一").replace(
                                "Hello body", "你好正文"
                            ),
                        }
                        for item in segments
                    ],
                }
                return {
                    "choices": [{"message": {"content": json.dumps(payload)}}]
                }

            with patch(
                "qqmail_viewer._keychain_get_optional", return_value="secret"
            ):
                translated = runtime.translate_message(
                    "a", "1", transport=transport
                )
                cached = runtime.translate_message(
                    "a",
                    "1",
                    transport=lambda *_args: self.fail("cache miss"),
                )
            self.assertEqual(translated.subject, "邮件一")
            self.assertEqual(translated.text, "你好正文")
            self.assertEqual(cached, translated)
            self.assertEqual(len(calls), 1)
            runtime.close()


class WebAndSettingsTests(unittest.TestCase):
    ACCOUNT = Account(
        "a", "custom", "a@example.com", "imap.example.com", 993, "email-a", "auth-a", True
    )

    def test_warm_home_reads_cache_without_creating_imap_client(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "qqmail_viewer.configured_client"
        ) as client_factory:
            runtime = ViewerRuntime(
                periodic=False,
                accounts=(self.ACCOUNT,),
                settings=CacheSettings("metadata", 3),
                cache_file=Path(directory) / "mail.sqlite3",
                encryption_key=b"k" * 32,
            )
            runtime.cache.upsert_messages("a", [_summary("1")])
            runtime.cache.mark_seeded("a", True)
            runtime.cache.mark_seeded("a", False)
            runtime.cache.mark_sync_success(
                "a", uidvalidity="1", highest_uid=1, full_sync_complete=True
            )
            handler = object.__new__(ViewerHandler)
            handler.server = SimpleNamespace(runtime=runtime)
            handler._send = MagicMock()
            handler._home({})
            body = handler._send.call_args.args[0].decode()
            self.assertIn("message 1", body)
            handler._api_messages({"account": ["a"]})
            api_payload = json.loads(handler._send.call_args.args[0])
            self.assertEqual(
                set(api_payload[0]), {"uid", "subject", "sender", "date", "size"}
            )
            client_factory.assert_not_called()
            runtime.close()

    def test_cli_single_and_aggregate_json_shapes_remain_compatible(self):
        cached = CachedMessage(
            "a",
            "1",
            "subject",
            "sender@example.com",
            "reader@example.com",
            "2026-08-05 10:00",
            1.0,
            10,
            True,
        )
        sync = SimpleNamespace(sync_accounts=lambda *args, **kwargs: ())
        state = SimpleNamespace(full_sync_complete=True)
        cache = SimpleNamespace(
            sync_state=lambda name: state,
            query_messages=lambda *args, **kwargs: (cached,),
            account_message_count=lambda name: 1,
        )
        runtime = SimpleNamespace(
            accounts=(self.ACCOUNT,),
            cache=cache,
            sync=sync,
            settings=CacheSettings("metadata", 3),
            close=MagicMock(),
        )
        with patch("qqmail_viewer.ViewerRuntime", return_value=runtime), patch(
            "qqmail_viewer.cached_errors", return_value=()
        ), patch("qqmail_viewer.sys.stdout", new_callable=io.StringIO) as output:
            list_cli(True, 20, False, None, 0, "a", False)
            single = json.loads(output.getvalue())
        self.assertEqual(
            set(single[0]), {"uid", "subject", "sender", "date", "size"}
        )

        with patch("qqmail_viewer.ViewerRuntime", return_value=runtime), patch(
            "qqmail_viewer.cached_errors", return_value=()
        ), patch("qqmail_viewer.sys.stdout", new_callable=io.StringIO) as output:
            list_cli(True, 20, False, None, 0, None, True)
            aggregate = json.loads(output.getvalue())
        self.assertEqual(set(aggregate), {"messages", "errors"})
        self.assertEqual(aggregate["messages"][0]["account"]["name"], "a")

    @unittest.skipIf(mail_cache.AESGCM is None, "cryptography is not installed")
    def test_message_translation_dialog_and_cached_chinese_default_view(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "qqmail_viewer.configured_client"
        ):
            runtime = ViewerRuntime(
                periodic=False,
                accounts=(self.ACCOUNT,),
                settings=CacheSettings("body", 3),
                cache_file=Path(directory) / "mail.sqlite3",
                encryption_key=b"w" * 32,
            )
            runtime.cache.upsert_messages("a", [_summary("1")])
            runtime.cache.store_detail(
                "a",
                "1",
                recipients="reader@example.com",
                text="Hello body",
                attachments=(),
                body_format="html",
                safe_html='<p>Hello <a href="https://example.com">open</a></p>',
                html_policy=HTML_POLICY_VERSION,
            )
            runtime._translation_config = None
            runtime._translation_config_loaded = True
            handler = object.__new__(ViewerHandler)
            handler.server = SimpleNamespace(runtime=runtime)
            handler._send = MagicMock()
            query = {
                "account": ["a"],
                "uid": ["1"],
                "return_account": ["a"],
            }
            handler._message(query)
            unconfigured = handler._send.call_args.args[0].decode()
            self.assertIn("data-translation-config-open", unconfigured)
            self.assertIn("绑定翻译 API", unconfigured)
            self.assertIn("邮件主题和正文会发送给该服务", unconfigured)
            self.assertNotIn('value="secret"', unconfigured)

            runtime._translation_config = TranslationConfig(
                "openai_compatible", "https://api.example.com/v1", "model"
            ).validated()
            item = runtime.message_detail("a", "1", prefer_html=True)
            digest = translation_source_digest(
                subject=item.subject,
                text=item.text,
                safe_html=item.safe_html,
                html_policy=item.html_policy,
            )
            runtime.cache.store_translation(
                "a",
                "1",
                source_digest=digest,
                subject="中文主题",
                text="你好正文",
                safe_html='<p>你好 <a href="https://example.com">打开</a></p>',
                source_language="EN",
                provider_fingerprint=runtime._translation_config.fingerprint(),
            )
            handler._message(query)
            translated = handler._send.call_args.args[0].decode()
            self.assertIn("<title>中文主题</title>", translated)
            self.assertIn(">中文主题</h1>", translated)
            self.assertIn("中文翻译", translated)
            self.assertIn("原文排版", translated)
            self.assertIn("原文纯文本", translated)
            self.assertIn('data-body-mode="translated" aria-pressed="true"', translated)
            self.assertIn("重新翻译", translated)
            self.assertIn("修改 API", translated)
            self.assertNotIn("data-translation-dialog", translated)

            handler._settings({})
            settings = handler._send.call_args.args[0].decode()
            self.assertIn("缓存、同步与翻译设置", settings)
            self.assertIn("OpenAI 兼容接口", settings)
            self.assertIn("https://api.example.com/v1", settings)
            self.assertIn("只清除译文缓存", settings)
            self.assertNotIn('value="secret"', settings)
            runtime.close()

    def test_translation_post_routes_share_csrf_protection(self):
        payload = b"csrf=wrong&scope=settings"
        for path in (
            "/translation/configure",
            "/translation/run",
            "/translation/disconnect",
            "/translation/cache/clear",
        ):
            with self.subTest(path=path):
                handler = object.__new__(ViewerHandler)
                handler.path = path
                handler.headers = {"Content-Length": str(len(payload))}
                handler.rfile = io.BytesIO(payload)
                handler._send = MagicMock()
                handler.do_POST()
                self.assertEqual(handler._send.call_args.args[1], 403)

    def test_translation_run_redirects_with_message_context(self):
        runtime = SimpleNamespace(
            accounts=(self.ACCOUNT,), translate_message=MagicMock()
        )
        handler = object.__new__(ViewerHandler)
        handler._runtime = MagicMock(return_value=runtime)
        handler._redirect = MagicMock()
        handler._translation_run_post(
            {
                "scope": ["message"],
                "account": ["a"],
                "uid": ["7"],
                "return_account": ["a"],
                "unread": ["0"],
                "limit": ["50"],
                "page": ["3"],
                "force": ["1"],
            }
        )
        runtime.translate_message.assert_called_once_with("a", "7", force=True)
        location = handler._redirect.call_args.args[0]
        self.assertIn("/message?", location)
        self.assertIn("translation=done", location)
        self.assertIn("page=3", location)

    def test_invalid_csrf_is_rejected(self):
        payload = b"csrf=wrong&action=clear_all"
        handler = object.__new__(ViewerHandler)
        handler.path = "/settings"
        handler.headers = {"Content-Length": str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        handler._send = MagicMock()
        handler.do_POST()
        self.assertEqual(handler._send.call_args.args[1], 403)

    def test_downgrade_renders_explicit_keep_or_purge_confirmation(self):
        handler = object.__new__(ViewerHandler)
        runtime = SimpleNamespace(settings=CacheSettings("body", 3))
        handler._runtime = MagicMock(return_value=runtime)
        handler._send = MagicMock()
        handler._settings_post(
            {
                "csrf": [PROCESS_CSRF_TOKEN],
                "action": ["save"],
                "cache_mode": ["metadata"],
                "refresh_minutes": ["3"],
            }
        )
        body = handler._send.call_args.args[0].decode()
        self.assertIn("保留旧缓存", body)
        self.assertIn("清除不再允许的数据", body)

    def test_settings_and_cache_cli_parsers(self):
        settings = build_parser().parse_args(
            ["settings", "--cache-mode", "metadata", "--refresh-minutes", "0", "--existing-cache", "purge"]
        )
        clear = build_parser().parse_args(["cache", "clear", "--bodies"])
        self.assertEqual(
            (settings.cache_mode, settings.refresh_minutes, settings.existing_cache),
            ("metadata", 0, "purge"),
        )
        self.assertTrue(clear.bodies)


if __name__ == "__main__":
    unittest.main()
