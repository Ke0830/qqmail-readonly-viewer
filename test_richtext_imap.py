import base64
import io
import unittest
from unittest.mock import patch

from PIL import Image

from mail_html import MAX_INPUT_BYTES, MAX_OUTPUT_BYTES
from qqmail_viewer import QQMailClient, ViewerError


HEADER_QUERY = "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO DATE)])"
BODYSTRUCTURE_QUERY = "(BODYSTRUCTURE)"
HEADER = (
    b"Subject: Section safety\r\n"
    b"From: sender@example.com\r\n"
    b"To: reader@example.com\r\n"
    b"Date: Wed, 05 Aug 2026 10:00:00 +0800\r\n"
    b"\r\n"
)


def _text_part(
    subtype: str,
    octets: int,
    *,
    content_id: str = "",
) -> bytes:
    encoded_content_id = (
        f'"{content_id}"'.encode("ascii") if content_id else b"NIL"
    )
    return (
        b'("TEXT" "'
        + subtype.encode("ascii")
        + b'" ("CHARSET" "UTF-8") '
        + encoded_content_id
        + b' NIL "8BIT" '
        + str(octets).encode("ascii")
        + b" 1 NIL NIL NIL NIL)"
    )


def _multipart(
    *children: bytes,
    subtype: str,
    parameters: bytes = b"NIL",
) -> bytes:
    return (
        b"("
        + b"".join(children)
        + b' "'
        + subtype.encode("ascii")
        + b'" '
        + parameters
        + b" NIL NIL NIL)"
    )


PLAIN = _text_part("PLAIN", 40)
HTML = _text_part("HTML", 120)
PDF = (
    b'("APPLICATION" "PDF" ("NAME" "report.pdf") NIL NIL "BASE64" '
    b'800 NIL ("ATTACHMENT" ("FILENAME" "report.pdf")) NIL NIL)'
)
CID_IMAGE = (
    b'("IMAGE" "PNG" ("NAME" "logo.png") "<logo@example>" NIL "BASE64" '
    b'500 NIL ("INLINE" ("FILENAME" "logo.png")) NIL "logo.png")'
)


class _RecordingIMAP:
    def __init__(self, structure: bytes, payloads: dict[str, bytes]) -> None:
        self.structure = structure
        self.payloads = payloads
        self.queries: list[str] = []

    def uid(self, command: str, uid: str, query: str):
        if command != "fetch" or uid != "7":
            raise AssertionError(f"unexpected IMAP request: {command} {uid} {query}")
        self.queries.append(query)
        if query == HEADER_QUERY:
            return "OK", [(b"1 (UID 7)", HEADER)]
        if query == BODYSTRUCTURE_QUERY:
            return "OK", [
                b"1 (UID 7 BODYSTRUCTURE " + self.structure + b")"
            ]
        if query in self.payloads:
            return "OK", [(b"1 (UID 7)", self.payloads[query])]
        raise AssertionError(
            f"unapproved body, attachment, image, or full-message fetch: {query}"
        )


class _SummaryIMAP:
    def __init__(self, structure: bytes) -> None:
        self.structure = structure
        self.queries: list[str] = []

    def uid(self, command: str, uid_set: str, query: str):
        if command != "fetch" or uid_set != "7":
            raise AssertionError(f"unexpected IMAP request: {command} {uid_set} {query}")
        self.queries.append(query)
        metadata = (
            b'1 (UID 7 FLAGS () INTERNALDATE "05-Aug-2026 10:00:00 +0800" '
            b"RFC822.SIZE 900 BODYSTRUCTURE "
            + self.structure
            + b")"
        )
        return "OK", [(metadata, HEADER)]


class RichTextImapSectionTests(unittest.TestCase):
    def _client(self, connection: _RecordingIMAP) -> QQMailClient:
        client = QQMailClient("reader@example.com", "secret")
        client.connection = connection
        return client

    def _assert_queries(
        self,
        connection: _RecordingIMAP,
        expected_body_queries: list[str],
    ) -> None:
        self.assertEqual(
            connection.queries,
            [HEADER_QUERY, BODYSTRUCTURE_QUERY, *expected_body_queries],
        )
        self.assertNotIn("(BODY.PEEK[])", connection.queries)

    def test_web_alternative_fetches_only_html_section(self):
        structure = _multipart(
            PLAIN,
            HTML,
            subtype="ALTERNATIVE",
            parameters=b'("BOUNDARY" "alternative")',
        )
        connection = _RecordingIMAP(
            structure,
            {
                "(BODY.PEEK[2])": (
                    b'<table style="color: #123456"><tr><td>Rich body</td>'
                    b"</tr></table>"
                )
            },
        )

        detail = self._client(connection).get_message("7", prefer_html=True)

        self.assertEqual(detail.body_format, "html")
        self.assertIn("Rich body", detail.safe_html)
        self.assertIn("Rich body", detail.text)
        self._assert_queries(connection, ["(BODY.PEEK[2])"])

    def test_cli_default_alternative_fetches_only_plain_section(self):
        structure = _multipart(
            PLAIN,
            HTML,
            subtype="ALTERNATIVE",
            parameters=b'("BOUNDARY" "alternative")',
        )
        connection = _RecordingIMAP(
            structure,
            {"(BODY.PEEK[1])": b"Plain CLI body"},
        )

        detail = self._client(connection).get_message("7")

        self.assertEqual(detail.body_format, "plain")
        self.assertEqual(detail.text, "Plain CLI body")
        self.assertEqual(detail.safe_html, "")
        self._assert_queries(connection, ["(BODY.PEEK[1])"])

    def test_html_sanitization_failure_fetches_plain_fallback(self):
        oversized_html = (
            b"<p>" + b"x" * (MAX_OUTPUT_BYTES + 1) + b"</p>"
        )
        structure = _multipart(
            PLAIN,
            _text_part("HTML", len(oversized_html)),
            subtype="ALTERNATIVE",
            parameters=b'("BOUNDARY" "alternative")',
        )
        connection = _RecordingIMAP(
            structure,
            {
                "(BODY.PEEK[2])": oversized_html,
                "(BODY.PEEK[1])": b"Safe fallback",
            },
        )

        detail = self._client(connection).get_message("7", prefer_html=True)

        self.assertEqual(detail.body_format, "plain")
        self.assertEqual(detail.text, "Safe fallback")
        self.assertEqual(detail.safe_html, "")
        self._assert_queries(
            connection,
            ["(BODY.PEEK[2])", "(BODY.PEEK[1])"],
        )

    def test_empty_sanitized_html_fetches_plain_fallback(self):
        structure = _multipart(
            PLAIN,
            HTML,
            subtype="ALTERNATIVE",
            parameters=b'("BOUNDARY" "alternative")',
        )
        connection = _RecordingIMAP(
            structure,
            {
                "(BODY.PEEK[2])": b"<script>hidden()</script>",
                "(BODY.PEEK[1])": b"Visible plain fallback",
            },
        )

        detail = self._client(connection).get_message("7", prefer_html=True)

        self.assertEqual(detail.body_format, "plain")
        self.assertEqual(detail.text, "Visible plain fallback")
        self._assert_queries(
            connection,
            ["(BODY.PEEK[2])", "(BODY.PEEK[1])"],
        )

    def test_empty_plain_fetches_html_fallback_for_cli_text(self):
        structure = _multipart(
            PLAIN,
            HTML,
            subtype="ALTERNATIVE",
            parameters=b'("BOUNDARY" "alternative")',
        )
        connection = _RecordingIMAP(
            structure,
            {
                "(BODY.PEEK[1])": b" \r\n\t ",
                "(BODY.PEEK[2])": b"<p>Visible HTML fallback</p>",
            },
        )

        detail = self._client(connection).get_message("7")

        self.assertEqual(detail.body_format, "html")
        self.assertEqual(detail.text, "Visible HTML fallback")
        self._assert_queries(
            connection,
            ["(BODY.PEEK[1])", "(BODY.PEEK[2])"],
        )

    def test_related_html_never_fetches_cid_image_payload(self):
        root_html = _text_part("HTML", 160, content_id="<root@example>")
        structure = _multipart(
            root_html,
            CID_IMAGE,
            subtype="RELATED",
            parameters=(
                b'("BOUNDARY" "related" "START" "<root@example>" '
                b'"TYPE" "text/html")'
            ),
        )
        connection = _RecordingIMAP(
            structure,
            {
                "(BODY.PEEK[1])": (
                    b'<p>Related body</p><img src="cid:logo@example" '
                    b'alt="Logo">'
                )
            },
        )

        detail = self._client(connection).get_message("7", prefer_html=True)

        self.assertEqual(detail.body_format, "html")
        self.assertGreaterEqual(detail.blocked_images, 1)
        self.assertEqual(
            detail.image_resources,
            (
                {
                    "id": "r1",
                    "source_type": "cid",
                    "source": "logo@example",
                    "descriptor": "",
                    "section": "2",
                    "content_type": "image/png",
                    "encoding": "base64",
                    "octets": 500,
                },
            ),
        )
        self.assertNotIn("cid:logo@example", detail.safe_html)
        self.assertIn("Logo", detail.safe_html)
        self._assert_queries(connection, ["(BODY.PEEK[1])"])
        self.assertNotIn("(BODY.PEEK[2])", connection.queries)

    def test_explicit_inline_image_fetch_uses_only_its_selected_section(self):
        output = io.BytesIO()
        Image.new("RGB", (1, 1), (1, 2, 3)).save(output, format="PNG")
        png = output.getvalue()
        connection = _RecordingIMAP(
            PLAIN,
            {"(BODY.PEEK[2])": base64.b64encode(png)},
        )
        image = self._client(connection).fetch_inline_image(
            "7",
            {
                "section": "2",
                "content_type": "image/png",
                "encoding": "base64",
                "octets": len(base64.b64encode(png)),
            },
        )
        self.assertEqual(image.mime_type, "image/png")
        self.assertEqual(image.dimensions, (1, 1))
        self.assertEqual(connection.queries, ["(BODY.PEEK[2])"])
        self.assertNotIn("(BODY.PEEK[])", connection.queries)

    def test_mixed_body_never_fetches_attachment_payload(self):
        structure = _multipart(
            PLAIN,
            PDF,
            subtype="MIXED",
            parameters=b'("BOUNDARY" "mixed")',
        )
        connection = _RecordingIMAP(
            structure,
            {"(BODY.PEEK[1])": b"Message with attachment"},
        )

        detail = self._client(connection).get_message("7")

        self.assertEqual(detail.text, "Message with attachment")
        self.assertEqual(detail.attachments, ("report.pdf",))
        self._assert_queries(connection, ["(BODY.PEEK[1])"])
        self.assertNotIn("(BODY.PEEK[2])", connection.queries)

    def test_summary_excludes_cid_images_from_attachment_names(self):
        related = _multipart(
            _text_part("HTML", 160, content_id="<root@example>"),
            CID_IMAGE,
            subtype="RELATED",
            parameters=(
                b'("BOUNDARY" "related" "START" "<root@example>" '
                b'"TYPE" "text/html")'
            ),
        )
        connection = _SummaryIMAP(
            _multipart(
                related,
                PDF,
                subtype="MIXED",
                parameters=b'("BOUNDARY" "mixed")',
            )
        )

        summaries = self._client(connection).fetch_summaries(["7"])

        self.assertEqual(summaries[0]["attachments"], ("report.pdf",))
        self.assertEqual(len(connection.queries), 1)
        self.assertIn("BODYSTRUCTURE", connection.queries[0])
        self.assertNotIn("BODY.PEEK[]", connection.queries[0])
        self.assertNotIn("BODY.PEEK[1]", connection.queries[0])
        self.assertNotIn("BODY.PEEK[2]", connection.queries[0])

    def test_declared_body_over_five_mib_is_not_fetched(self):
        structure = _text_part("HTML", MAX_INPUT_BYTES + 1)
        connection = _RecordingIMAP(structure, {})

        detail = self._client(connection).get_message("7", prefer_html=True)

        self.assertFalse(detail.cacheable)
        self.assertEqual(detail.body_format, "unavailable")
        self.assertEqual(detail.safe_html, "")
        self._assert_queries(connection, [])
        self.assertNotIn("(BODY.PEEK[TEXT])", connection.queries)

    def test_plain_text_result_over_two_mib_is_not_cached(self):
        structure = _text_part("PLAIN", 80)
        connection = _RecordingIMAP(
            structure,
            {"(BODY.PEEK[TEXT])": b"x" * 80},
        )

        with patch("qqmail_viewer.MAX_OUTPUT_BYTES", 64):
            detail = self._client(connection).get_message("7")

        self.assertFalse(detail.cacheable)
        self.assertEqual(detail.body_format, "unavailable")
        self.assertEqual(detail.text, "为避免下载附件，正文无法安全读取")
        self._assert_queries(connection, ["(BODY.PEEK[TEXT])"])

    def test_unknown_multipart_does_not_fetch_any_body_section(self):
        structure = _multipart(
            PLAIN,
            PDF,
            subtype="X-CUSTOM",
            parameters=b'("BOUNDARY" "custom")',
        )
        connection = _RecordingIMAP(structure, {})

        detail = self._client(connection).get_message("7", prefer_html=True)

        self.assertFalse(detail.cacheable)
        self.assertEqual(detail.body_format, "unavailable")
        self.assertEqual(detail.attachments, ("report.pdf",))
        self._assert_queries(connection, [])

    def test_ok_without_a_literal_is_a_transport_failure_not_empty_body(self):
        structure = _text_part("HTML", 120)
        connection = _RecordingIMAP(
            structure,
            {"(BODY.PEEK[TEXT])": b""},
        )

        with self.assertRaises(ViewerError):
            self._client(connection).get_message("7", prefer_html=True)

        self._assert_queries(connection, ["(BODY.PEEK[TEXT])"])


if __name__ == "__main__":
    unittest.main()
