import html
import json
import unittest
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import MagicMock

from qqmail_viewer import (
    BASE_SCRIPT,
    HTML_POLICY_VERSION,
    MAIL_BODY_CSP,
    Account,
    MailDetail,
    ViewerHandler,
    _mail_body_srcdoc,
    _mail_detail_record,
)


class _MarkupCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.tags.append((tag, dict(attrs)))

    def first(self, tag: str) -> dict[str, str | None]:
        return next(attrs for current_tag, attrs in self.tags if current_tag == tag)


class RichTextRenderingTests(unittest.TestCase):
    ACCOUNT = Account(
        "qq",
        "qq",
        "reader@qq.com",
        "imap.qq.com",
        993,
        "email-service",
        "auth-service",
        True,
    )

    def _render_detail(self, detail: MailDetail) -> tuple[str, MagicMock]:
        message_detail = MagicMock(return_value=detail)
        runtime = SimpleNamespace(
            accounts=(self.ACCOUNT,),
            message_detail=message_detail,
        )
        handler = object.__new__(ViewerHandler)
        handler._runtime = MagicMock(return_value=runtime)
        handler._send = MagicMock()
        handler._message(
            {
                "uid": [detail.uid],
                "account": [self.ACCOUNT.name],
                "return_account": [self.ACCOUNT.name],
                "unread": ["0"],
                "limit": ["30"],
                "page": ["2"],
            }
        )
        rendered = handler._send.call_args.args[0].decode("utf-8")
        return rendered, message_detail

    def test_web_detail_requests_html_and_isolates_it_in_a_sandboxed_srcdoc(self):
        safe_html = (
            '<table style="color: #123456"><tr><td>排版正文</td></tr></table>'
            '<p><a href="https://example.com/full/path?q=1">查看详情</a></p>'
        )
        rendered, message_detail = self._render_detail(
            MailDetail(
                "42",
                "富文本邮件",
                "Sender <sender@example.com>",
                "reader@qq.com",
                "2026-08-05 18:00",
                "排版正文\n\n查看详情",
                ("report.pdf",),
                safe_html=safe_html,
                body_format="html",
                blocked_images=2,
                html_policy=HTML_POLICY_VERSION,
            )
        )

        message_detail.assert_called_once_with("qq", "42", prefer_html=True)
        collector = _MarkupCollector()
        collector.feed(rendered)
        iframe = collector.first("iframe")
        self.assertEqual(iframe.get("sandbox"), "allow-same-origin")
        self.assertNotIn("allow-scripts", iframe.get("sandbox") or "")
        self.assertNotIn("allow-forms", iframe.get("sandbox") or "")
        self.assertNotIn("allow-popups", iframe.get("sandbox") or "")
        self.assertNotIn("allow-top-navigation", iframe.get("sandbox") or "")

        srcdoc = iframe.get("srcdoc") or ""
        decoded_srcdoc = html.unescape(srcdoc)
        self.assertIn(safe_html, decoded_srcdoc)
        self.assertIn('http-equiv="Content-Security-Policy"', decoded_srcdoc)
        self.assertIn("default-src 'none'", decoded_srcdoc)
        self.assertIn("img-src 'none'", decoded_srcdoc)
        self.assertIn("font-src 'none'", decoded_srcdoc)
        self.assertIn("form-action 'none'", decoded_srcdoc)
        self.assertNotIn(safe_html, rendered)

        self.assertIn('data-message-body', rendered)
        self.assertIn('data-body-mode="html"', rendered)
        self.assertIn('data-body-mode="plain"', rendered)
        self.assertIn("排版版", rendered)
        self.assertIn("纯文本", rendered)
        mode_buttons = {
            attrs.get("data-body-mode"): attrs
            for tag, attrs in collector.tags
            if tag == "button" and attrs.get("data-body-mode")
        }
        self.assertEqual(mode_buttons["html"].get("aria-pressed"), "true")
        self.assertEqual(mode_buttons["plain"].get("aria-pressed"), "false")
        panels = {
            attrs.get("data-body-panel"): attrs
            for _tag, attrs in collector.tags
            if attrs.get("data-body-panel")
        }
        self.assertNotIn("hidden", panels["html"])
        self.assertIn("hidden", panels["plain"])
        self.assertIn("本邮件中的 2 张图片已隐藏", rendered)
        self.assertIn('data-external-link-dialog', rendered)
        self.assertIn('data-open-external-link', rendered)
        self.assertIn("排版正文", rendered)

    def test_plain_only_detail_skips_the_rich_text_frame(self):
        rendered, message_detail = self._render_detail(
            MailDetail(
                "43",
                "纯文本邮件",
                "sender@example.com",
                "reader@qq.com",
                "2026-08-05 18:10",
                "第一行\n\n第二行 <不会被当成标签>",
                (),
                body_format="plain",
            )
        )

        message_detail.assert_called_once_with("qq", "43", prefer_html=True)
        self.assertNotIn("<iframe", rendered)
        self.assertNotIn('data-body-mode="html"', rendered)
        self.assertIn("第一行", rendered)
        self.assertIn("第二行 &lt;不会被当成标签&gt;", rendered)

    def test_srcdoc_policy_blocks_every_network_capability(self):
        document = html.unescape(html.unescape(_mail_body_srcdoc("<p>safe</p>")))

        self.assertIn(MAIL_BODY_CSP, document)
        for directive in (
            "script-src 'none'",
            "connect-src 'none'",
            "img-src 'none'",
            "font-src 'none'",
            "media-src 'none'",
            "object-src 'none'",
            "frame-src 'none'",
            "form-action 'none'",
        ):
            self.assertIn(directive, document)
        self.assertIn("<body><p>safe</p></body>", document)

    def test_parent_page_csp_allows_only_local_frames(self):
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
        self.assertIn("frame-src 'self'", policy)
        self.assertNotIn("frame-src http:", policy)
        self.assertNotIn("frame-src https:", policy)

    def test_external_links_are_intercepted_and_opened_only_after_confirmation(self):
        self.assertIn("confirmExternalLink(shell, destination)", BASE_SCRIPT)
        self.assertIn("event.preventDefault()", BASE_SCRIPT)
        self.assertIn("dialog.showModal()", BASE_SCRIPT)
        self.assertIn("output.textContent = url.href", BASE_SCRIPT)
        self.assertIn("['http:', 'https:'].includes(destination.protocol)", BASE_SCRIPT)
        self.assertIn(
            'window.open(destination.href, "_blank", "noopener,noreferrer")',
            BASE_SCRIPT,
        )
        self.assertIn("opened.opener = null", BASE_SCRIPT)

    def test_cli_detail_json_keeps_the_legacy_plain_text_shape(self):
        detail = MailDetail(
            "44",
            "CLI 兼容",
            "sender@example.com",
            "reader@qq.com",
            "2026-08-05 18:20",
            "命令行纯文本",
            ("report.pdf",),
            safe_html="<p>SHOULD_NOT_LEAK_SAFE_HTML</p>",
            body_format="html",
            blocked_images=3,
            html_policy=HTML_POLICY_VERSION,
        )

        encoded = json.dumps(_mail_detail_record(detail), ensure_ascii=False)
        payload = json.loads(encoded)

        self.assertEqual(
            set(payload),
            {
                "uid",
                "subject",
                "sender",
                "recipients",
                "date",
                "text",
                "attachments",
            },
        )
        self.assertEqual(payload["text"], "命令行纯文本")
        self.assertEqual(payload["attachments"], ["report.pdf"])
        self.assertNotIn("SHOULD_NOT_LEAK_SAFE_HTML", encoded)
        self.assertNotIn("safe_html", payload)
        self.assertNotIn("body_format", payload)
        self.assertNotIn("blocked_images", payload)
        self.assertNotIn("html_policy", payload)


if __name__ == "__main__":
    unittest.main()
