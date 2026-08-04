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
    AUTH_SERVICE,
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
