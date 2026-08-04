import email
import unittest

from qqmail_viewer import decode_bytes, decode_mime, extract_message_text, normalize_date


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


if __name__ == "__main__":
    unittest.main()
