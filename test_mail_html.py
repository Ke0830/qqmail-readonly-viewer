import unittest
from unittest.mock import patch

import mail_html
from mail_html import (
    HTML_POLICY_VERSION,
    HtmlSizeLimitError,
    SanitizedEmailHtml,
    normalize_plain_text,
    sanitize_email_html,
)


class MailHtmlTests(unittest.TestCase):
    def test_preserves_semantic_layout_tables_and_safe_inline_css(self):
        result = sanitize_email_html(
            """
            <main>
              <h1 style="color:#123456; font-size:28px; position:fixed">安全提醒</h1>
              <table width="640" cellpadding="12" style="border-collapse:collapse; background-color:#f4f7fb">
                <tr><th align="left">账号</th><th align="left">时间</th></tr>
                <tr><td>reader@example.com</td><td><strong>10:30</strong></td></tr>
              </table>
            </main>
            """
        )
        self.assertIsInstance(result, SanitizedEmailHtml)
        self.assertEqual(HTML_POLICY_VERSION, "mail-html-v1")
        self.assertIn("<main>", result.safe_html)
        self.assertIn("<table", result.safe_html)
        self.assertIn('width="640"', result.safe_html)
        self.assertRegex(result.safe_html, r"border-collapse:\s*collapse")
        self.assertRegex(result.safe_html, r"background-color:\s*#f4f7fb")
        self.assertRegex(result.safe_html, r"color:\s*#123456")
        self.assertNotIn("position", result.safe_html)
        self.assertIn("安全提醒", result.plain_text)
        self.assertIn("reader@example.com", result.plain_text)

    def test_removes_active_content_and_automatic_resource_urls(self):
        result = sanitize_email_html(
            """
            <base href="https://evil.example/">
            <script>fetch('http://127.0.0.1:9999/steal')</script>
            <style>@import 'https://evil.example/mail.css';</style>
            <link rel="stylesheet" href="https://evil.example/mail.css">
            <meta http-equiv="refresh" content="0;url=https://evil.example/">
            <iframe src="https://evil.example/frame"></iframe>
            <object data="https://evil.example/object"></object>
            <form action="https://evil.example/post"><input autofocus><button>确认</button></form>
            <div onclick="alert(1)" style="background-image:url(https://evil.example/pixel); color:red">
              可见正文
            </div>
            <picture><source srcset="https://evil.example/large.png 2x"><img src="https://evil.example/p.png" srcset="https://evil.example/p2.png 2x" alt="图"></picture>
            <video poster="https://evil.example/poster"><source src="https://evil.example/a.mp4"></video>
            """
        )
        for forbidden in (
            "127.0.0.1",
            "@import",
            "autofocus",
            "background-image",
            "evil.example",
            "<base",
            "<form",
            "<iframe",
            "<input",
            "<link",
            "<meta",
            "<object",
            "<script",
            "<style",
            "<video",
            "onclick",
            "poster=",
            "src=",
        ):
            self.assertNotIn(forbidden, result.safe_html)
        self.assertIn("可见正文", result.safe_html)
        self.assertRegex(result.safe_html, r"color:\s*red")
        self.assertIn("确认", result.safe_html)

    def test_keeps_only_absolute_http_and_https_links(self):
        result = sanitize_email_html(
            """
            <p><a href="https://example.com/path?q=1">安全链接</a></p>
            <p><a href="http://example.org/">HTTP</a></p>
            <p><a href="https://例子.测试/path">国际化域名</a></p>
            <p><a href="javascript:alert(1)">脚本</a></p>
            <p><a href="data:text/html,bad">数据</a></p>
            <p><a href="vbscript:msgbox(1)">旧脚本</a></p>
            <p><a href="mailto:user@example.com">邮件</a></p>
            <p><a href="http://127.0.0.1:8080/admin">本机地址</a></p>
            <p><a href="http://2130706433/admin">数字本机地址</a></p>
            <p><a href="http://0x7f000001/admin">十六进制本机地址</a></p>
            <p><a href="http://127.0.0.1\\@example.com/admin">反斜杠地址</a></p>
            <p><a href="http://localhost/private">本机名称</a></p>
            <p><a href="http://127。0。0。1/private">Unicode 句点本机地址</a></p>
            <p><a href="http://１２７.０.０.１/private">全角数字本机地址</a></p>
            <p><a href="http://local­host/private">软连字符本机名称</a></p>
            <p><a href="http://l⁤ocalhost/private">不可见字符本机名称</a></p>
            <p><a href="http://192.168.1.2/private">局域网地址</a></p>
            <p><a href="http://224.0.0.1/private">IPv4 组播</a></p>
            <p><a href="http://[ff02::1]/private">IPv6 组播</a></p>
            <p><a href="http://[fec0::1]/private">IPv6 site-local</a></p>
            <p><a href="http://[::7f00:1]/private">IPv6 保留地址</a></p>
            <p><a href="//example.net/path">协议相对</a></p>
            <p><a href="/local">相对路径</a></p>
            """
        )
        self.assertIn('href="https://example.com/path?q=1"', result.safe_html)
        self.assertIn('href="http://example.org/"', result.safe_html)
        self.assertIn('href="https://xn--fsqu00a.xn--0zwm56d/path"', result.safe_html)
        self.assertIn('target="_blank"', result.safe_html)
        self.assertIn('referrerpolicy="no-referrer"', result.safe_html)
        self.assertNotIn("javascript:", result.safe_html)
        self.assertNotIn("data:text", result.safe_html)
        self.assertNotIn("vbscript:", result.safe_html)
        self.assertNotIn("mailto:", result.safe_html)
        self.assertNotIn("127.0.0.1", result.safe_html)
        self.assertNotIn("2130706433", result.safe_html)
        self.assertNotIn("0x7f000001", result.safe_html)
        self.assertNotIn("127.0.0.1\\@example.com", result.safe_html)
        self.assertNotIn("localhost", result.safe_html)
        self.assertNotIn("127。0。0。1", result.safe_html)
        self.assertNotIn("１２７.０.０.１", result.safe_html)
        self.assertNotIn("local­host", result.safe_html)
        self.assertNotIn("l⁤ocalhost", result.safe_html)
        self.assertNotIn("192.168.1.2", result.safe_html)
        self.assertNotIn("224.0.0.1", result.safe_html)
        self.assertNotIn("ff02::1", result.safe_html)
        self.assertNotIn("fec0::1", result.safe_html)
        self.assertNotIn("::7f00:1", result.safe_html)
        self.assertNotIn('href="//', result.safe_html)
        self.assertNotIn('href="/local"', result.safe_html)
        self.assertIn("安全链接 (https://example.com/path?q=1)", result.plain_text)

    def test_rejects_css_network_functions_and_extreme_dimensions(self):
        result = sanitize_email_html(
            """
            <div style="
              color:rgb(10,20,30);
              width:640px;
              padding:24px;
              background:url(https://evil.example/a.png);
              background-color:var(--secret);
              cursor:url(https://evil.example/cursor),auto;
              width:999999px;
              animation:spin 1s;
              opacity:0.5
            ">内容</div>
            """
        )
        self.assertRegex(result.safe_html, r"color:\s*rgb\(10,20,30\)")
        self.assertRegex(result.safe_html, r"padding:\s*24px")
        self.assertRegex(result.safe_html, r"opacity:\s*0\.5")
        self.assertNotIn("evil.example", result.safe_html)
        self.assertNotIn("var(", result.safe_html)
        self.assertNotIn("cursor", result.safe_html)
        self.assertNotIn("animation", result.safe_html)
        self.assertNotIn("999999", result.safe_html)

    def test_replaces_normal_images_and_silently_removes_trackers(self):
        result = sanitize_email_html(
            """
            <p>品牌 <img src="https://images.example/logo.png" alt="公司标志" width="320" height="80"></p>
            <img src="https://track.example/open" width="1" height="1" alt="">
            <img src="cid:hero" alt="内嵌横幅" style="width:600px;height:200px">
            <img src="data:image/png;base64,AAAA" style="display:none" alt="hidden">
            <img alt="无源图片">
            """
        )
        self.assertEqual(result.blocked_images, 5)
        self.assertEqual(result.safe_html.count("mail-image-placeholder"), 3)
        self.assertIn("图片未加载：公司标志", result.safe_html)
        self.assertIn("图片未加载：内嵌横幅", result.safe_html)
        self.assertIn("图片未加载：无源图片", result.safe_html)
        self.assertNotIn("track.example", result.safe_html)
        self.assertNotIn("cid:", result.safe_html)
        self.assertNotIn("data:image", result.safe_html)
        self.assertNotIn("hidden", result.safe_html)

    def test_strips_svg_math_and_malformed_script_payloads(self):
        result = sanitize_email_html(
            """
            <svg><foreignObject><script>alert(1)</script></foreignObject></svg>
            <math><mtext><img src=x onerror=alert(1)></mtext></math>
            <p><scr<script>ipt>alert(2)</scr</script>ipt>正文</p>
            """
        )
        self.assertNotIn("<svg", result.safe_html)
        self.assertNotIn("<math", result.safe_html)
        self.assertNotIn("onerror", result.safe_html)
        self.assertNotIn("<script", result.safe_html)
        self.assertIn("正文", result.plain_text)

    def test_normalizes_plain_text_whitespace(self):
        raw = "  第一行\u00a0\u00a0内容\u200b  \r\n\r\n\r\n  第二行\t内容  "
        self.assertEqual(normalize_plain_text(raw), "第一行 内容\n\n第二行 内容")
        self.assertEqual(
            normalize_plain_text("literal &amp; &lt;tag&gt;"),
            "literal &amp; &lt;tag&gt;",
        )
        result = sanitize_email_html(
            "<h2>标题</h2><p>第一段&nbsp;&nbsp;内容</p><p>第二段<br>下一行</p>"
        )
        self.assertEqual(
            result.plain_text,
            "标题\n\n第一段 内容\n\n第二段\n下一行",
        )

    def test_enforces_input_and_output_byte_limits(self):
        with patch.object(mail_html, "MAX_INPUT_BYTES", 8):
            with self.assertRaises(HtmlSizeLimitError):
                sanitize_email_html("九个字节")
        with patch.object(mail_html, "MAX_OUTPUT_BYTES", 12):
            with self.assertRaises(HtmlSizeLimitError):
                sanitize_email_html("<p>这是一段超过限制的正文</p>")
        with patch.object(mail_html, "MAX_OUTPUT_BYTES", 256):
            with self.assertRaises(HtmlSizeLimitError):
                sanitize_email_html("<img alt='占位'>" * 1000)

    def test_rejects_non_string_input(self):
        with self.assertRaises(TypeError):
            sanitize_email_html(b"<p>bytes</p>")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
