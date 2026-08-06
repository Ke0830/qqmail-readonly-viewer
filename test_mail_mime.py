import unittest

from mail_mime import (
    BodyStructureError,
    parse_bodystructure,
    safe_text_parts,
    select_body_plan,
)


PLAIN = (
    b'("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" '
    b"80 4 NIL NIL NIL NIL)"
)
HTML = (
    b'("TEXT" "HTML" ("CHARSET" "UTF-8") NIL NIL "QUOTED-PRINTABLE" '
    b"160 8 NIL NIL NIL NIL)"
)
PDF = (
    b'("APPLICATION" "PDF" ("NAME" "report.pdf") NIL NIL "BASE64" '
    b'900 NIL ("ATTACHMENT" ("FILENAME" "report.pdf")) NIL NIL)'
)


def response(structure: bytes) -> list[bytes]:
    return [b"1 (UID 7 BODYSTRUCTURE " + structure + b")"]


def multipart(*children: bytes, subtype: str, extensions: bytes) -> bytes:
    return (
        b"("
        + b"".join(children)
        + b' "'
        + subtype.encode("ascii")
        + b'" '
        + extensions
        + b")"
    )


class BodyStructureMetadataTests(unittest.TestCase):
    def test_parses_extended_single_part_metadata_and_size(self):
        structure = (
            b'("TEXT" "HTML" ("CHARSET" "UTF-8" "FORMAT" "FLOWED") '
            b'"<root@example>" "message body" "QUOTED-PRINTABLE" 321 12 '
            b'"body-md5" ("INLINE" ("FILENAME" "body.html")) '
            b'("EN" "ZH") "body/location")'
        )

        part = parse_bodystructure(response(structure))

        self.assertEqual(part.section, "TEXT")
        self.assertEqual(part.content_type, "text/html")
        self.assertEqual(dict(part.parameters), {"charset": "UTF-8", "format": "FLOWED"})
        self.assertEqual(part.charset, "UTF-8")
        self.assertEqual(part.content_id, "<root@example>")
        self.assertEqual(part.description, "message body")
        self.assertEqual(part.encoding, "quoted-printable")
        self.assertEqual(part.octets, 321)
        self.assertEqual(part.lines, 12)
        self.assertEqual(part.md5, "body-md5")
        self.assertEqual(part.disposition, "inline")
        self.assertEqual(dict(part.disposition_parameters), {"filename": "body.html"})
        self.assertEqual(part.filename, "body.html")
        self.assertEqual(part.language, ("EN", "ZH"))
        self.assertEqual(part.content_location, "body/location")

    def test_rejects_malformed_or_missing_size(self):
        malformed = b'("TEXT" "PLAIN" NIL NIL NIL "7BIT" NIL 1)'
        with self.assertRaises(BodyStructureError):
            parse_bodystructure(response(malformed))

        with self.assertRaises(BodyStructureError):
            parse_bodystructure([b"1 (UID 7 BODYSTRUCTURE broken)"])

    def test_rejects_multipart_without_a_subtype(self):
        malformed = b"(" + PLAIN + b" NIL NIL NIL NIL NIL)"

        with self.assertRaises(BodyStructureError):
            parse_bodystructure(response(malformed))


class BodyPlanTests(unittest.TestCase):
    def test_alternative_prefers_html_with_plain_fallback(self):
        structure = multipart(
            PLAIN,
            HTML,
            subtype="ALTERNATIVE",
            extensions=b'("BOUNDARY" "alt") NIL NIL NIL',
        )
        root = parse_bodystructure(response(structure))

        rich = select_body_plan(root, prefer_html=True)
        plain = select_body_plan(root, prefer_html=False)

        self.assertEqual([part.section for part in rich.primary], ["2"])
        self.assertEqual([part.section for part in rich.fallback], ["1"])
        self.assertEqual([part.section for part in plain.primary], ["1"])
        self.assertEqual([part.section for part in plain.fallback], ["2"])
        self.assertFalse(rich.attachments)

        legacy_parts, legacy_attachments = safe_text_parts(root)
        self.assertEqual([part.section for part in legacy_parts], ["1"])
        self.assertFalse(legacy_attachments)

    def test_related_uses_start_root_and_separates_inline_resources(self):
        image = (
            b'("IMAGE" "PNG" ("NAME" "logo.png") "<logo@id>" NIL '
            b'"BASE64" 120 NIL ("INLINE" ("FILENAME" "logo.png")) '
            b'NIL "logo.png")'
        )
        body = (
            b'("TEXT" "HTML" ("CHARSET" "UTF-8") "<body@id>" NIL '
            b'"QUOTED-PRINTABLE" 240 8 NIL NIL NIL NIL)'
        )
        structure = multipart(
            image,
            body,
            PDF,
            subtype="RELATED",
            extensions=(
                b'("BOUNDARY" "rel" "START" "<body@id>" '
                b'"TYPE" "text/html") NIL NIL NIL'
            ),
        )
        root = parse_bodystructure(response(structure))

        plan = select_body_plan(root)

        self.assertEqual(root.parameters["start"], "<body@id>")
        self.assertEqual([part.section for part in plan.primary], ["2"])
        self.assertEqual(
            [(part.section, part.content_id) for part in plan.inline_resources],
            [("1", "<logo@id>")],
        )
        self.assertEqual(plan.attachments, ("report.pdf",))

    def test_related_without_start_uses_first_child_as_root(self):
        structure = multipart(
            HTML,
            b'("IMAGE" "GIF" NIL "<pixel@id>" NIL "BASE64" 20 NIL NIL NIL NIL)',
            subtype="RELATED",
            extensions=b'("BOUNDARY" "rel") NIL NIL NIL',
        )

        plan = select_body_plan(parse_bodystructure(response(structure)))

        self.assertEqual([part.section for part in plan.primary], ["1"])
        self.assertEqual([part.section for part in plan.inline_resources], ["2"])

    def test_mixed_selects_body_branch_and_aggregates_attachments(self):
        alternative = multipart(
            PLAIN,
            HTML,
            subtype="ALTERNATIVE",
            extensions=b'("BOUNDARY" "alt") NIL NIL NIL',
        )
        forwarded = (
            b'("MESSAGE" "RFC822" ("NAME" "forwarded.eml") NIL NIL '
            b'"7BIT" 400 NIL '
            b'("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" '
            b'40 3 NIL NIL NIL NIL) 10 NIL '
            b'("ATTACHMENT" ("FILENAME" "forwarded.eml")) NIL NIL)'
        )
        structure = multipart(
            alternative,
            PDF,
            forwarded,
            subtype="MIXED",
            extensions=b'("BOUNDARY" "mix") NIL NIL NIL',
        )
        root = parse_bodystructure(response(structure))

        plan = select_body_plan(root)

        self.assertEqual([part.section for part in plan.primary], ["1.2"])
        self.assertEqual([part.section for part in plan.fallback], ["1.1"])
        self.assertEqual(plan.attachments, ("report.pdf", "forwarded.eml"))
        forwarded_part = root.children[2]
        self.assertEqual(forwarded_part.content_type, "message/rfc822")
        self.assertEqual(forwarded_part.octets, 400)
        self.assertEqual(forwarded_part.lines, 10)
        self.assertFalse(forwarded_part.children)

    def test_encrypted_container_has_no_readable_sections(self):
        control = (
            b'("APPLICATION" "PGP-ENCRYPTED" NIL NIL NIL "7BIT" '
            b"11 NIL NIL NIL NIL)"
        )
        ciphertext = (
            b'("APPLICATION" "OCTET-STREAM" ("NAME" "encrypted.asc") '
            b'NIL NIL "7BIT" 500 NIL NIL NIL NIL)'
        )
        structure = multipart(
            control,
            ciphertext,
            subtype="ENCRYPTED",
            extensions=(
                b'("PROTOCOL" "application/pgp-encrypted" '
                b'"BOUNDARY" "encrypted") NIL NIL NIL'
            ),
        )

        plan = select_body_plan(parse_bodystructure(response(structure)))

        self.assertFalse(plan.primary)
        self.assertFalse(plan.fallback)
        self.assertFalse(plan.attachments)
        self.assertFalse(plan.inline_resources)
        self.assertEqual(plan.blocked_reason, "encrypted")

    def test_unknown_multipart_does_not_select_a_body(self):
        structure = multipart(
            PLAIN,
            PDF,
            subtype="X-CUSTOM",
            extensions=b'("BOUNDARY" "custom") NIL NIL NIL',
        )

        plan = select_body_plan(parse_bodystructure(response(structure)))

        self.assertFalse(plan.primary)
        self.assertFalse(plan.fallback)
        self.assertEqual(plan.attachments, ("report.pdf",))
        self.assertEqual(plan.blocked_reason, "unsupported")


if __name__ == "__main__":
    unittest.main()
