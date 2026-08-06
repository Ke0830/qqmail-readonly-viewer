import email
import ssl
import sys
import unittest
import uuid
from types import SimpleNamespace
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import parse_qs, urlparse
from unittest.mock import MagicMock, patch

from qqmail_viewer import (
    ACCOUNT_INDEX_SERVICE,
    AUTH_SERVICE,
    Account,
    EMAIL_SERVICE,
    KEYCHAIN_ACCOUNT,
    ListingParams,
    QQMailClient,
    ViewerHandler,
    ViewerError,
    build_parser,
    decode_bytes,
    decode_mime,
    extract_message_text,
    keychain_get,
    keychain_set,
    listing_url,
    normalize_date,
    parse_listing_params,
    _configure_standard_streams,
    _windows_credential_get,
    _windows_credential_set,
    _windows_keyring,
    _configured_profile,
    aggregate_cli,
    aggregate_page,
    detect_provider,
    find_account,
    load_accounts,
)


class _FakeIMAP:
    def __init__(self, headers: dict[str, bytes]) -> None:
        self.headers = headers

    def uid(self, command: str, *args: object):
        if command == "search":
            return "OK", [" ".join(self.headers).encode("ascii")]
        if command == "fetch":
            uids = str(args[0]).split(",")
            rows = []
            for uid in uids:
                metadata = f"1 (UID {uid} RFC822.SIZE 42)".encode("ascii")
                rows.append((metadata, self.headers[uid]))
            return "OK", rows
        raise AssertionError(f"unexpected IMAP command: {command}")


class MailParsingTests(unittest.TestCase):
    def test_decodes_chinese_subject(self):
        value = "=?utf-8?b?5rWL6K+V6YKu5Lu2?="
        self.assertEqual(decode_mime(value), "测试邮件")

    def test_decodes_nonstandard_raw_gbk_header(self):
        raw = b"Subject: \xce\xa2\xd0\xc5\xcd\xc5\xb6\xd3\n\n"
        message = email.message_from_bytes(raw)
        self.assertEqual(decode_mime(message.get("Subject")), "微信团队")

    def test_declared_charset_does_not_replace_before_fallback(self):
        payload = "腾讯企业微信".encode("gb18030")
        self.assertEqual(decode_bytes(payload, "utf-8"), "腾讯企业微信")

    def test_prefers_plain_text_and_lists_attachment(self):
        raw = b"""MIME-Version: 1.0
Content-Type: multipart/mixed; boundary=x

--x
Content-Type: text/plain; charset=utf-8

hello world
--x
Content-Type: application/pdf
Content-Disposition: attachment; filename=report.pdf
Content-Transfer-Encoding: base64

AA==
--x--
"""
        message = email.message_from_bytes(raw)
        text, attachments = extract_message_text(message)
        self.assertEqual(text, "hello world")
        self.assertEqual(attachments, ("report.pdf",))

    def test_converts_html_to_plain_text(self):
        raw = b"""Content-Type: text/html; charset=utf-8

<html><style>hidden</style><body><p>Hello <b>QQ</b></p><script>bad()</script></body></html>
"""
        message = email.message_from_bytes(raw)
        text, _ = extract_message_text(message)
        self.assertEqual(text, "Hello QQ")

    def test_decodes_gbk_body_with_wrong_utf8_declaration(self):
        body = "这是早期中文邮件".encode("gb18030")
        raw = b"Content-Type: text/plain; charset=utf-8\n\n" + body
        message = email.message_from_bytes(raw)
        text, _ = extract_message_text(message)
        self.assertEqual(text, "这是早期中文邮件")

    def test_normalizes_rfc_date(self):
        result = normalize_date("Mon, 04 Aug 2025 08:30:00 +0800")
        self.assertRegex(result, r"^2025-08-04 08:30$")

    def test_list_parser_supports_time_filter_pagination_and_all_pages(self):
        args = build_parser().parse_args(
            ["list", "--unread", "--since-hours", "24", "--all-pages", "--offset", "100"]
        )
        self.assertEqual(args.since_hours, 24)
        self.assertTrue(args.all_pages)
        self.assertEqual(args.offset, 100)

    def test_filters_by_recent_hours_before_paging(self):
        now = 1_754_000_000

        def headers(subject: str, timestamp: float) -> bytes:
            date = format_datetime(datetime.fromtimestamp(timestamp, timezone.utc))
            return f"Subject: {subject}\r\nFrom: sender@example.com\r\nDate: {date}\r\n\r\n".encode()

        client = QQMailClient("user@example.com", "authorization-code")
        client.connection = _FakeIMAP(
            {
                "1": headers("newest", now - 60),
                "2": headers("still recent", now - 23 * 60 * 60),
                "3": headers("too old", now - 25 * 60 * 60),
            }
        )

        with patch("qqmail_viewer.time.time", return_value=now):
            messages = client.list_messages(unread_only=True, limit=None, since_hours=24)
            second_page = client.list_messages(unread_only=True, limit=1, offset=1, since_hours=24)

        self.assertEqual([message.subject for message in messages], ["newest", "still recent"])
        self.assertEqual([message.subject for message in second_page], ["still recent"])

    def test_browser_page_returns_total_and_clamps_to_last_page(self):
        headers = {
            str(index): (
                f"Subject: message {index}\r\nFrom: sender{index}@example.com\r\n"
                f"Date: Mon, 04 Aug 2025 0{index}:00:00 +0000\r\n\r\n"
            ).encode()
            for index in range(1, 4)
        }
        client = QQMailClient("user@example.com", "authorization-code")
        client.connection = _FakeIMAP(headers)

        result = client.list_page(unread_only=False, limit=2, offset=50)

        self.assertEqual(result.total, 3)
        self.assertEqual(result.offset, 2)
        self.assertEqual(result.current_page, 2)
        self.assertEqual(result.page_count, 2)
        self.assertEqual([message.subject for message in result.messages], ["message 1"])

    def test_browser_page_handles_an_empty_mailbox(self):
        client = QQMailClient("user@example.com", "authorization-code")
        client.connection = _FakeIMAP({})

        result = client.list_page(unread_only=True, limit=30, offset=0)

        self.assertEqual(result.total, 0)
        self.assertEqual(result.current_page, 0)
        self.assertEqual(result.page_count, 0)
        self.assertEqual(result.messages, ())

    def test_page_query_has_priority_and_legacy_offset_still_works(self):
        page_params = parse_listing_params(
            {"unread": ["0"], "limit": ["50"], "page": ["3"], "offset": ["999"]}
        )
        offset_params = parse_listing_params({"unread": ["1"], "limit": ["50"], "offset": ["100"]})

        self.assertEqual((page_params.unread_only, page_params.limit, page_params.offset), (False, 50, 100))
        self.assertEqual((offset_params.requested_page, offset_params.offset), (3, 100))
        self.assertEqual(listing_url(False, 50, 100), "/?unread=0&limit=50&page=3")

    def test_invalid_browser_page_is_reported_as_first_page(self):
        result = parse_listing_params({"page": ["not-a-number"]})

        self.assertEqual((result.offset, result.requested_page), (0, 1))
        self.assertTrue(result.invalid_page)

    def test_detail_url_preserves_the_list_position(self):
        params = ListingParams(unread_only=False, limit=50, offset=100, requested_page=3)
        item = SimpleNamespace(uid="168")

        detail_url = ViewerHandler._message_url(item, params, 100)
        detail_params = parse_listing_params(parse_qs(urlparse(detail_url).query))

        self.assertEqual(listing_url(detail_params.unread_only, detail_params.limit, detail_params.offset), "/?unread=0&limit=50&page=3")

    def test_detail_url_keeps_account_and_aggregate_return_target(self):
        params = ListingParams(unread_only=True, limit=30, offset=60, requested_page=3)
        detail_url = ViewerHandler._message_url(SimpleNamespace(uid="42"), params, 60, "icloud", "all")
        query = parse_qs(urlparse(detail_url).query)

        self.assertEqual(query["account"], ["icloud"])
        self.assertEqual(query["return_account"], ["all"])
        self.assertEqual(listing_url(True, 30, 60, "all"), "/?unread=1&limit=30&page=3&account=all")

    def test_detects_supported_provider_domains(self):
        expected = {
            "person@qq.com": "qq",
            "person@foxmail.com": "qq",
            "person@163.com": "163",
            "person@126.com": "126",
            "person@yeah.net": "yeah",
            "person@icloud.com": "icloud",
            "person@gmail.com": "gmail",
        }
        self.assertEqual({address: detect_provider(address) for address in expected}, expected)
        self.assertIsNone(detect_provider("person@example.com"))

    def test_validates_custom_imaps_and_preconfigured_profiles(self):
        provider, host, port = _configured_profile("custom", "person@example.com", "imap.example.com", 1993)
        self.assertEqual((provider.id, host, port), ("custom", "imap.example.com", 1993))
        with self.assertRaisesRegex(ViewerError, "必须提供 --imap-host"):
            _configured_profile("custom", "person@example.com", None, None)
        with self.assertRaisesRegex(ViewerError, "不需要 --imap-host"):
            _configured_profile("gmail", "person@gmail.com", "imap.example.com", None)

    def test_legacy_qq_credentials_are_a_default_virtual_account(self):
        credentials = {EMAIL_SERVICE: "legacy@qq.com", AUTH_SERVICE: "secret"}
        def get_legacy(service):
            if service in credentials:
                return credentials[service]
            raise ViewerError("missing")

        with patch("qqmail_viewer.keychain_get", side_effect=get_legacy):
            accounts = load_accounts()

        self.assertEqual(len(accounts), 1)
        self.assertEqual((accounts[0].name, accounts[0].provider, accounts[0].email), ("qq", "qq", "legacy@qq.com"))
        self.assertTrue(accounts[0].is_default)
        self.assertEqual(accounts[0].auth_service, AUTH_SERVICE)

    def test_find_account_uses_default_and_rejects_unknown_name(self):
        accounts = (
            Account("qq", "qq", "a@qq.com", "imap.qq.com", 993, "email-a", "auth-a", True),
            Account("gmail", "gmail", "b@gmail.com", "imap.gmail.com", 993, "email-b", "auth-b"),
        )
        self.assertEqual(find_account(None, accounts).name, "qq")
        self.assertEqual(find_account("gmail", accounts).email, "b@gmail.com")
        with self.assertRaisesRegex(ViewerError, "可用账户"):
            find_account("missing", accounts)

    def test_aggregate_sorts_accounts_and_keeps_uid_ownership(self):
        accounts = (
            Account("qq", "qq", "a@qq.com", "imap.qq.com", 993, "email-a", "auth-a", True),
            Account("gmail", "gmail", "b@gmail.com", "imap.gmail.com", 993, "email-b", "auth-b"),
        )

        class FakeClient:
            def __init__(self, messages): self.messages = messages
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def list_messages(self, **kwargs): return self.messages

        from qqmail_viewer import MailSummary

        messages = {
            "qq": [MailSummary("7", "QQ", "a", "2026-08-04 10:00", 1)],
            "gmail": [MailSummary("7", "Gmail", "b", "2026-08-04 11:00", 1)],
        }
        with patch("qqmail_viewer.configured_client", side_effect=lambda account: FakeClient(messages[account.name])):
            page, rows, errors = aggregate_page(accounts, unread_only=False, limit=30)

        self.assertFalse(errors)
        self.assertEqual(page.total, 2)
        self.assertEqual([(row.account.name, row.message.uid) for row in rows], [("gmail", "7"), ("qq", "7")])

    def test_aggregate_reports_a_failed_account_but_keeps_successes(self):
        accounts = (
            Account("qq", "qq", "a@qq.com", "imap.qq.com", 993, "email-a", "auth-a", True),
            Account("bad", "custom", "b@example.com", "imap.example.com", 993, "email-b", "auth-b"),
        )

        class GoodClient:
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def list_messages(self, **kwargs):
                from qqmail_viewer import MailSummary
                return [MailSummary("1", "ok", "sender", "2026-08-04 12:00", 1)]

        def fake_client(account):
            if account.name == "bad":
                raise ViewerError("连接失败")
            return GoodClient()

        with patch("qqmail_viewer.configured_client", side_effect=fake_client):
            result = aggregate_cli(accounts, unread=True, limit=20, offset=0, since_hours=None, include_text=False)

        self.assertEqual(result["messages"][0]["account"]["name"], "qq")
        self.assertEqual(result["errors"], [{"account": "bad", "error": "连接失败"}])

    def test_aggregate_web_view_shows_account_selector_warning_and_owned_links(self):
        from qqmail_viewer import MailPage, MailSummary

        accounts = (
            Account("qq", "qq", "a@qq.com", "imap.qq.com", 993, "email-a", "auth-a", True),
            Account("gmail", "gmail", "b@gmail.com", "imap.gmail.com", 993, "email-b", "auth-b"),
        )
        summary = MailSummary("8", "A subject", "Person <person@example.com>", "2026-08-04 12:00", 1)
        handler = object.__new__(ViewerHandler)
        handler._send = MagicMock()
        handler._runtime = MagicMock(
            return_value=SimpleNamespace(
                accounts=accounts,
                cache=SimpleNamespace(
                    sync_state=lambda name: SimpleNamespace(full_sync_complete=True)
                ),
            )
        )
        handler._prepare_cache = MagicMock(
            return_value=({"account": "qq", "error": "连接失败"},)
        )
        page_data = MailPage((summary,), 1, 0, 30)

        with patch(
            "qqmail_viewer.cached_page",
            return_value=(
                page_data,
                (SimpleNamespace(message=summary, account=accounts[1], unread=True),),
            ),
        ):
            handler._home({"account": ["all"], "unread": ["0"], "limit": ["30"], "page": ["1"]})

        body = handler._send.call_args.args[0].decode("utf-8")
        self.assertIn("<title>本地邮箱查看器</title>", body)
        self.assertIn("<h1>本地邮箱查看器</h1>", body)
        self.assertNotIn("<h1>本地只读邮箱查看器</h1>", body)
        self.assertIn(
            '<p class="account-summary" aria-label="已添加邮箱，共 2 个">'
            '<strong class="account-summary-name">已添加邮箱</strong>'
            '<span class="account-summary-count">（2 个）</span></p>',
            body,
        )
        self.assertNotIn("当前范围", body)
        self.assertIn("全部（2）", body)
        self.assertIn(">QQ</a>", body)
        self.assertIn(">Gmail</a>", body)
        self.assertIn(
            '<span class="state-stat state-total">共 1 封</span>', body
        )
        self.assertIn(
            '<span class="state-stat state-range">显示第 1–1 封</span>', body
        )
        self.assertIn(
            '<span class="state-stat state-page">第 1 / 1 页</span>', body
        )
        self.assertIn(
            '<span class="state-stat state-sort">按日期倒序</span>', body
        )
        self.assertLess(body.index("state-page"), body.index("state-sort"))
        self.assertIn(
            ".state-text{display:grid;grid-template-columns:72px 132px 96px max-content",
            body,
        )
        self.assertIn("全部账户", body)
        self.assertIn("account=qq", body)
        self.assertIn("account=gmail", body)
        self.assertIn('aria-label="qq，a@qq.com"', body)
        self.assertIn('aria-label="gmail，b@gmail.com"', body)
        self.assertNotIn(">切换</button>", body)
        self.assertIn('aria-label="每页 30 封" aria-current="page"', body)
        self.assertLess(body.index(">30</a>"), body.index(">50</a>"))
        self.assertLess(body.index(">50</a>"), body.index(">100</a>"))
        self.assertIn("limit=50&amp;page=1&amp;account=all", body)
        self.assertIn("limit=100&amp;page=1&amp;account=all", body)
        self.assertNotIn(">应用</button>", body)
        self.assertIn('<script src="/assets/viewer.js" defer></script>', body)
        self.assertNotIn("window.fetch(url", body)
        self.assertNotIn("@view-transition", body)
        self.assertIn('<span class="sender-name">Person</span>', body)
        self.assertIn(
            '<span class="mail-address-label">发件邮箱</span>'
            '<span class="sender-address">person@example.com</span>',
            body,
        )
        self.assertIn(
            '<span class="mail-address-label">收件账户</span>'
            '<span class="account-tag">gmail · b@gmail.com</span>',
            body,
        )
        self.assertIn("gmail · b@gmail.com", body)
        self.assertIn('class="mail mail-unread"', body)
        self.assertIn('<span class="message-status">未读</span>', body)
        self.assertIn("账户 qq 暂时无法读取", body)
        self.assertIn("return_account=all", body)

        with patch(
            "qqmail_viewer.cached_page",
            return_value=(
                page_data,
                (SimpleNamespace(message=summary, account=accounts[1], unread=True),),
            ),
        ):
            for selected_limit in (50, 100):
                handler._home(
                    {
                        "account": ["all"],
                        "unread": ["0"],
                        "limit": [str(selected_limit)],
                        "page": ["1"],
                    }
                )
                selected_body = handler._send.call_args.args[0].decode("utf-8")
                self.assertIn(
                    f'aria-label="每页 {selected_limit} 封" aria-current="page"',
                    selected_body,
                )
                self.assertLess(
                    selected_body.index(">30</a>"), selected_body.index(">50</a>")
                )
                self.assertLess(
                    selected_body.index(">50</a>"), selected_body.index(">100</a>")
                )

    def test_all_mail_view_labels_read_and_unread_without_cluttering_unread_view(self):
        from qqmail_viewer import MailPage, MailSummary

        account = Account(
            "qq", "qq", "a@qq.com", "imap.qq.com", 993, "email-a", "auth-a", True
        )
        unread = MailSummary("9", "Unread subject", "Unread sender", "2026-08-05 12:00", 1)
        read = MailSummary("8", "Read subject", "Read sender", "2026-08-05 11:00", 1)
        handler = object.__new__(ViewerHandler)
        handler._send = MagicMock()
        handler._runtime = MagicMock(
            return_value=SimpleNamespace(
                accounts=(account,),
                cache=SimpleNamespace(
                    sync_state=lambda name: SimpleNamespace(full_sync_complete=True)
                ),
            )
        )
        handler._prepare_cache = MagicMock(return_value=())
        page_data = MailPage((unread, read), 2, 0, 30)
        owned = (
            SimpleNamespace(message=unread, account=account, unread=True),
            SimpleNamespace(message=read, account=account, unread=False),
        )

        with patch("qqmail_viewer.cached_page", return_value=(page_data, owned)):
            handler._home(
                {"account": ["qq"], "unread": ["0"], "limit": ["30"], "page": ["1"]}
            )

        body = handler._send.call_args.args[0].decode("utf-8")
        self.assertIn(
            '<p class="account-summary"><strong class="account-summary-name" '
            'title="QQ">QQ</strong><span class="account-summary-separator" '
            'aria-hidden="true">·</span><span class="account-summary-detail" '
            'title="a@qq.com">a@qq.com</span></p>',
            body,
        )
        self.assertIn('class="mail mail-unread"', body)
        self.assertIn('class="mail mail-read"', body)
        self.assertIn(
            '<section class="mailbox mailbox-with-status" aria-label="全部邮件列表">',
            body,
        )
        self.assertIn(
            '<div class="list-head"><span>发件人</span><span>主题</span>'
            '<span class="status-head">状态</span><span>日期</span></div>',
            body,
        )
        self.assertIn(
            '<span class="subject"><span class="subject-text">Unread subject</span>'
            '</span><span class="message-status">未读</span>',
            body,
        )
        self.assertIn('<span class="message-status">未读</span>', body)
        self.assertIn('<span class="message-status">已读</span>', body)

        unread_page = MailPage((unread,), 1, 0, 30)
        with patch(
            "qqmail_viewer.cached_page",
            return_value=(
                unread_page,
                (SimpleNamespace(message=unread, account=account, unread=True),),
            ),
        ):
            handler._home(
                {"account": ["qq"], "unread": ["1"], "limit": ["30"], "page": ["1"]}
            )

        unread_body = handler._send.call_args.args[0].decode("utf-8")
        self.assertIn(
            '<section class="mailbox" aria-label="未读邮件列表">', unread_body
        )
        self.assertNotIn(
            '<section class="mailbox mailbox-with-status"', unread_body
        )
        self.assertNotIn('class="status-head"', unread_body)
        self.assertNotIn('<span class="message-status">', unread_body)
        self.assertNotIn('class="mail mail-unread"', unread_body)
        self.assertNotIn('class="mail mail-read"', unread_body)
        self.assertNotIn("收件账户", unread_body)

    def test_sender_parts_keeps_a_bare_email_available_for_explicit_labeling(self):
        from qqmail_viewer import sender_parts

        self.assertEqual(
            sender_parts("Person <person@example.com>"),
            ("Person", "person@example.com"),
        )
        self.assertEqual(
            sender_parts("person@example.com"), ("", "person@example.com")
        )
        self.assertEqual(sender_parts("小可"), ("小可", ""))

    def test_serves_slider_script_and_allows_only_same_origin_script_and_fetch(self):
        handler = object.__new__(ViewerHandler)
        handler.path = "/assets/viewer.js"
        handler._send = MagicMock()

        handler.do_GET()

        script = handler._send.call_args.args[0].decode("utf-8")
        self.assertIn("segment-slider", script)
        self.assertIn("duration: 240", script)
        self.assertIn("window.fetch(url", script)
        self.assertIn("currentMain.replaceWith(nextPage.main)", script)
        self.assertIn("window.history.pushState", script)
        self.assertIn("Promise.allSettled", script)
        self.assertEqual(
            handler._send.call_args.kwargs["content_type"],
            "text/javascript; charset=utf-8",
        )

        handler = object.__new__(ViewerHandler)
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.wfile = MagicMock()

        handler._send(b"ok")

        headers = {
            call.args[0]: call.args[1]
            for call in handler.send_header.call_args_list
        }
        policy = headers["Content-Security-Policy"]
        self.assertIn("script-src 'self'", policy)
        self.assertIn("connect-src 'self'", policy)
        self.assertNotIn("'unsafe-inline'", policy.split("script-src", 1)[1].split(";", 1)[0])

    def test_uses_windows_credential_backend_only_on_windows(self):
        with patch("qqmail_viewer.sys.platform", "win32"), patch(
            "qqmail_viewer._windows_credential_get", return_value="user@example.com"
        ) as get_credential, patch("qqmail_viewer._windows_credential_set") as set_credential:
            self.assertEqual(keychain_get(EMAIL_SERVICE), "user@example.com")
            keychain_set(AUTH_SERVICE, "authorization-code")

        get_credential.assert_called_once_with(EMAIL_SERVICE)
        set_credential.assert_called_once_with(AUTH_SERVICE, "authorization-code")

    def test_imap_connection_verifies_certificate_and_hostname(self):
        connection = MagicMock()
        connection.select.return_value = ("OK", [b""])

        with patch("qqmail_viewer.imaplib.IMAP4_SSL", return_value=connection) as constructor:
            with QQMailClient("user@example.com", "authorization-code"):
                pass

        context = constructor.call_args.kwargs["ssl_context"]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_configures_cli_streams_for_utf8_output(self):
        stdout = MagicMock()
        stderr = MagicMock()
        with patch("qqmail_viewer.sys.stdout", stdout), patch("qqmail_viewer.sys.stderr", stderr):
            _configure_standard_streams()

        stdout.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")
        stderr.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")

    def test_rejects_non_windows_keyring_backend(self):
        class UnsafeBackend:
            pass

        fake_keyring = SimpleNamespace(get_keyring=lambda: UnsafeBackend())
        with patch.dict(sys.modules, {"keyring": fake_keyring}):
            with self.assertRaisesRegex(ViewerError, "只允许使用系统凭据管理器"):
                _windows_keyring()

    @unittest.skipUnless(sys.platform == "win32", "requires Windows Credential Manager")
    def test_windows_credential_manager_round_trip(self):
        service = f"qqmail-readonly-viewer.test.{uuid.uuid4().hex}"
        value = uuid.uuid4().hex
        stored = False
        try:
            _windows_credential_set(service, value)
            stored = True
            self.assertEqual(_windows_credential_get(service), value)
        finally:
            if stored:
                import keyring

                keyring.delete_password(service, KEYCHAIN_ACCOUNT)


if __name__ == "__main__":
    unittest.main()
