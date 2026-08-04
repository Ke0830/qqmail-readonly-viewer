import email
import unittest
from datetime import datetime, timezone
from email.utils import format_datetime
from unittest.mock import patch

from qqmail_viewer import QQMailClient, build_parser, decode_bytes, decode_mime, extract_message_text, normalize_date


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


if __name__ == "__main__":
    unittest.main()
