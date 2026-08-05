import io
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import mail_cache
from mail_cache import CacheSettings, CacheStore, CachedMessage, default_cache_path
from mail_mime import parse_bodystructure, safe_text_parts
from mail_sync import SyncManager
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
                "a", "1", recipients="reader@example.com", text="私密正文", attachments=("a.pdf",)
            )
            raw = cache.connection.execute(
                "SELECT body_ciphertext FROM messages"
            ).fetchone()[0]
            self.assertNotIn("私密正文".encode(), bytes(raw))
            cache.close()

            reopened = CacheStore(path, CacheSettings("body", 3), key)
            detail = reopened.cached_detail("a", "1")
            self.assertIsNotNone(detail)
            self.assertEqual((detail.text, detail.attachments), ("私密正文", ("a.pdf",)))
            reopened.connection.execute(
                "UPDATE messages SET body_ciphertext=?", (sqlite3.Binary(b"damaged"),)
            )
            reopened.connection.commit()
            self.assertIsNone(reopened.cached_detail("a", "1"))
            self.assertIsNone(
                reopened.connection.execute("SELECT body_ciphertext FROM messages").fetchone()[0]
            )
            reopened.close()

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

    def get_message(self, uid):
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
