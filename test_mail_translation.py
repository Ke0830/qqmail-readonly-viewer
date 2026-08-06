import json
import unittest

from mail_translation import (
    DEEPL_FREE_ENDPOINT,
    DEEPL_PRO_ENDPOINT,
    MAX_TRANSLATION_CHARS,
    TranslationConfig,
    TranslationError,
    TranslationSegment,
    translate_mail_content,
    translate_segments,
    translation_source_digest,
)


def _openai_echo_transport(calls, transform=lambda value: value):
    def transport(url, body, headers, deadline):
        request = json.loads(body)
        segments = json.loads(request["messages"][1]["content"])["segments"]
        calls.append((url, request, dict(headers), deadline))
        content = {
            "source_language": "EN",
            "translations": [
                {"id": item["id"], "text": transform(item["text"])}
                for item in segments
            ],
        }
        return {"choices": [{"message": {"content": json.dumps(content)}}]}

    return transport


class TranslationConfigTests(unittest.TestCase):
    def test_deepl_uses_fixed_free_and_pro_endpoints(self):
        calls = []

        def transport(url, body, headers, deadline):
            calls.append((url, json.loads(body), dict(headers)))
            return {
                "translations": [
                    {"text": "你好", "detected_source_language": "EN"}
                ]
            }

        for provider, endpoint in (
            ("deepl_free", DEEPL_FREE_ENDPOINT),
            ("deepl_pro", DEEPL_PRO_ENDPOINT),
        ):
            result = translate_segments(
                TranslationConfig(provider),
                "private-key",
                (TranslationSegment("subject", "Hello"),),
                transport=transport,
            )
            self.assertEqual(result.translations[0].text, "你好")
            self.assertEqual(calls[-1][0], endpoint)
            self.assertEqual(calls[-1][1]["target_lang"], "ZH-HANS")
            self.assertEqual(
                calls[-1][2]["Authorization"], "DeepL-Auth-Key private-key"
            )
            self.assertNotIn("private-key", json.dumps(calls[-1][1]))

    def test_openai_compatible_url_rules_cover_remote_and_ollama(self):
        remote = TranslationConfig(
            "openai_compatible", "https://API.Example.com/v1/", "mail-model"
        ).validated()
        local = TranslationConfig(
            "openai_compatible", "http://127.0.0.1:11434/v1", "qwen3"
        ).validated()
        self.assertEqual(remote.base_url, "https://api.example.com/v1")
        self.assertEqual(local.base_url, "http://127.0.0.1:11434/v1")
        with self.assertRaisesRegex(TranslationError, "必须使用 HTTPS"):
            TranslationConfig(
                "openai_compatible", "http://api.example.com/v1", "model"
            ).validated()
        for value in (
            "https://person:secret@example.com/v1",
            "https://example.com/v1?token=secret",
            "https://example.com/v1#fragment",
        ):
            with self.assertRaises(TranslationError):
                TranslationConfig("openai_compatible", value, "model").validated()

    def test_openai_requires_key_remotely_but_not_for_local_ollama(self):
        calls = []
        local = TranslationConfig(
            "openai_compatible", "http://localhost:11434/v1", "qwen3"
        )
        result = translate_segments(
            local,
            "",
            (TranslationSegment("subject", "Hello"),),
            transport=_openai_echo_transport(calls),
        )
        self.assertEqual(result.translations[0].text, "Hello")
        self.assertNotIn("Authorization", calls[0][2])
        with self.assertRaisesRegex(TranslationError, "必须填写 API Key"):
            translate_segments(
                TranslationConfig(
                    "openai_compatible", "https://api.example.com/v1", "model"
                ),
                "",
                (TranslationSegment("subject", "Hello"),),
                transport=_openai_echo_transport([]),
            )


class TranslationBehaviorTests(unittest.TestCase):
    CONFIG = TranslationConfig(
        "openai_compatible", "https://api.example.com/v1", "mail-model"
    )

    def test_preserves_html_structure_links_images_and_plain_paragraphs(self):
        calls = []
        source = (
            '<table style="color: red"><tr><td>Hello</td></tr></table>'
            '<p><a href="https://example.com/path">Open</a></p>'
            '<img data-mail-image-ids="image-1" alt="Logo">'
            '<p>Write to person@example.com</p>'
        )

        def translated(value):
            return value.replace("Hello", "你好").replace("Open", "打开").replace(
                "Write to", "写信给"
            )

        result = translate_mail_content(
            self.CONFIG,
            "secret",
            subject="A subject",
            text="Hello\n\nOpen",
            safe_html=source,
            transport=_openai_echo_transport(calls, translated),
        )

        self.assertIn("你好", result.safe_html)
        self.assertIn("打开", result.safe_html)
        self.assertIn('href="https://example.com/path"', result.safe_html)
        self.assertIn('data-mail-image-ids="image-1"', result.safe_html)
        self.assertIn("person@example.com", result.safe_html)
        self.assertIn("你好\n\n打开", result.text)
        self.assertTrue(all(call[0].endswith("/chat/completions") for call in calls))

    def test_model_html_is_escaped_and_cannot_add_active_content(self):
        result = translate_mail_content(
            self.CONFIG,
            "secret",
            subject="Subject",
            text="Body",
            safe_html="<p>Body</p>",
            transport=_openai_echo_transport(
                [], lambda _value: '<script src="https://bad.example/x.js">x</script>'
            ),
        )
        self.assertNotIn("<script", result.safe_html)
        self.assertIn("&lt;script", result.safe_html)

    def test_missing_or_duplicate_ids_are_rejected(self):
        def malformed(_url, _body, _headers, _deadline):
            content = {
                "source_language": "EN",
                "translations": [{"id": "wrong", "text": "坏结果"}],
            }
            return {"choices": [{"message": {"content": json.dumps(content)}}]}

        with self.assertRaisesRegex(TranslationError, "无效片段"):
            translate_segments(
                self.CONFIG,
                "secret",
                (TranslationSegment("subject", "Hello"),),
                transport=malformed,
            )

        def reordered(_url, body, _headers, _deadline):
            request = json.loads(body)
            segments = json.loads(request["messages"][1]["content"])["segments"]
            content = {
                "source_language": "EN",
                "translations": [
                    {"id": item["id"], "text": item["text"]}
                    for item in reversed(segments)
                ],
            }
            return {"choices": [{"message": {"content": json.dumps(content)}}]}

        with self.assertRaisesRegex(TranslationError, "无效片段"):
            translate_segments(
                self.CONFIG,
                "secret",
                (
                    TranslationSegment("subject", "Hello"),
                    TranslationSegment("body", "World"),
                ),
                transport=reordered,
            )

    def test_urls_and_email_addresses_must_survive_exactly(self):
        with self.assertRaisesRegex(TranslationError, "链接或邮箱地址"):
            translate_segments(
                self.CONFIG,
                "secret",
                (
                    TranslationSegment(
                        "body", "Open https://example.com and mail person@example.com"
                    ),
                ),
                transport=_openai_echo_transport(
                    [], lambda value: value.replace("__MAIL_TRANSLATION_TOKEN_0__", "")
                ),
            )

    def test_large_segments_are_batched_and_reassembled(self):
        calls = []
        source = "Long sentence. " * 4000
        result = translate_segments(
            self.CONFIG,
            "secret",
            (TranslationSegment("body", source),),
            transport=_openai_echo_transport(calls),
        )
        self.assertEqual(result.translations, (TranslationSegment("body", source),))
        self.assertGreater(len(calls), 1)
        with self.assertRaisesRegex(TranslationError, "100,000"):
            translate_segments(
                self.CONFIG,
                "secret",
                (TranslationSegment("body", "x" * (MAX_TRANSLATION_CHARS + 1)),),
                transport=_openai_echo_transport([]),
            )

    def test_source_digest_changes_with_body_or_policy(self):
        first = translation_source_digest(
            subject="s", text="t", safe_html="<p>t</p>", html_policy="v1"
        )
        second = translation_source_digest(
            subject="s", text="t2", safe_html="<p>t2</p>", html_policy="v1"
        )
        third = translation_source_digest(
            subject="s", text="t", safe_html="<p>t</p>", html_policy="v2"
        )
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, third)


if __name__ == "__main__":
    unittest.main()
