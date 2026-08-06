"""Minimal IMAP BODYSTRUCTURE parsing for attachment-safe text retrieval."""

from __future__ import annotations

import base64
import quopri
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Iterable, Mapping


@dataclass(frozen=True)
class MimePart:
    section: str
    content_type: str
    charset: str | None
    encoding: str
    filename: str
    disposition: str
    children: tuple["MimePart", ...] = ()
    parameters: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({})
    )
    content_id: str = ""
    description: str = ""
    octets: int | None = None
    lines: int | None = None
    md5: str = ""
    disposition_parameters: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({})
    )
    language: tuple[str, ...] = ()
    content_location: str = ""


@dataclass(frozen=True)
class BodyPlan:
    """Safe MIME sections selected for rendering without attachment payloads."""

    primary: tuple[MimePart, ...] = ()
    fallback: tuple[MimePart, ...] = ()
    attachments: tuple[str, ...] = ()
    inline_resources: tuple[MimePart, ...] = ()
    blocked_reason: str = ""


class BodyStructureError(ValueError):
    pass


class _Parser:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.position = 0

    def parse(self):
        self._space()
        value = self._value()
        self._space()
        if self.position != len(self.payload):
            raise BodyStructureError("unexpected trailing BODYSTRUCTURE data")
        return value

    def _value(self):
        self._space()
        if self.position >= len(self.payload):
            raise BodyStructureError("unexpected end of BODYSTRUCTURE")
        current = self.payload[self.position]
        if current == ord("("):
            return self._list()
        if current == ord('"'):
            return self._quoted()
        return self._atom()

    def _list(self) -> list[object]:
        self.position += 1
        values: list[object] = []
        while True:
            self._space()
            if self.position >= len(self.payload):
                raise BodyStructureError("unterminated BODYSTRUCTURE list")
            if self.payload[self.position] == ord(")"):
                self.position += 1
                return values
            values.append(self._value())

    def _quoted(self) -> str:
        self.position += 1
        value = bytearray()
        while self.position < len(self.payload):
            current = self.payload[self.position]
            self.position += 1
            if current == ord('"'):
                return value.decode("utf-8", errors="replace")
            if current == ord("\\") and self.position < len(self.payload):
                current = self.payload[self.position]
                self.position += 1
            value.append(current)
        raise BodyStructureError("unterminated quoted BODYSTRUCTURE value")

    def _atom(self):
        start = self.position
        while self.position < len(self.payload):
            current = self.payload[self.position]
            if current in b" ()\r\n\t":
                break
            self.position += 1
        if start == self.position:
            raise BodyStructureError("invalid BODYSTRUCTURE atom")
        value = self.payload[start:self.position].decode("ascii", errors="replace")
        if value.upper() == "NIL":
            return None
        if value.isdigit():
            return int(value)
        return value

    def _space(self) -> None:
        while self.position < len(self.payload) and self.payload[self.position] in b" \r\n\t":
            self.position += 1


def _response_bytes(rows: Iterable[object]) -> bytes:
    parts: list[bytes] = []
    for row in rows:
        if isinstance(row, bytes):
            parts.append(row)
        elif isinstance(row, tuple):
            for item in row:
                if isinstance(item, bytes):
                    parts.append(item)
    return b" ".join(parts)


def extract_bodystructure(rows: Iterable[object]) -> bytes:
    response = _response_bytes(rows)
    marker = b"BODYSTRUCTURE"
    position = response.upper().find(marker)
    if position < 0:
        raise BodyStructureError("BODYSTRUCTURE response is missing")
    start = response.find(b"(", position + len(marker))
    if start < 0:
        raise BodyStructureError("BODYSTRUCTURE value is missing")
    depth = 0
    quoted = False
    escaped = False
    for index in range(start, len(response)):
        current = response[index]
        if quoted:
            if escaped:
                escaped = False
            elif current == ord("\\"):
                escaped = True
            elif current == ord('"'):
                quoted = False
            continue
        if current == ord('"'):
            quoted = True
        elif current == ord("("):
            depth += 1
        elif current == ord(")"):
            depth -= 1
            if depth == 0:
                return response[start : index + 1]
    raise BodyStructureError("BODYSTRUCTURE value is incomplete")


def parse_bodystructure(rows: Iterable[object]) -> MimePart:
    parsed = _Parser(extract_bodystructure(rows)).parse()
    if not isinstance(parsed, list):
        raise BodyStructureError("BODYSTRUCTURE root is invalid")
    return _part(parsed, "")


def _part(node: list[object], section: str) -> MimePart:
    if not node:
        raise BodyStructureError("empty BODYSTRUCTURE part")
    if isinstance(node[0], list):
        child_nodes: list[list[object]] = []
        index = 0
        while index < len(node) and isinstance(node[index], list):
            child_nodes.append(node[index])  # type: ignore[arg-type]
            index += 1
        if index >= len(node):
            raise BodyStructureError("multipart subtype is missing")
        subtype = _string(node[index]).lower()
        if not subtype:
            raise BodyStructureError("multipart subtype is missing")
        children = tuple(
            _part(child, f"{section}.{child_index}" if section else str(child_index))
            for child_index, child in enumerate(child_nodes, start=1)
        )
        parameters = _parameters(node[index + 1] if len(node) > index + 1 else None)
        disposition_node = node[index + 2] if len(node) > index + 2 else None
        disposition = _disposition(disposition_node)
        disposition_parameters = _disposition_parameters(disposition_node)
        return MimePart(
            section=section,
            content_type=f"multipart/{subtype}",
            charset=None,
            encoding="",
            filename=_filename(parameters, disposition_parameters),
            disposition=disposition,
            children=children,
            parameters=_immutable_parameters(parameters),
            disposition_parameters=_immutable_parameters(disposition_parameters),
            language=_language(node[index + 3] if len(node) > index + 3 else None),
            content_location=_string(
                node[index + 4] if len(node) > index + 4 else None
            ),
        )

    if len(node) < 7:
        raise BodyStructureError("single-part BODYSTRUCTURE is incomplete")
    media_type = _string(node[0]).lower()
    subtype = _string(node[1]).lower()
    if not media_type or not subtype:
        raise BodyStructureError("single-part media type is missing")
    parameters = _parameters(node[2])
    octets = _number(node[6], "single-part size")
    if media_type == "text":
        extension_start = 8
        lines = _optional_number(node[7] if len(node) > 7 else None)
    elif media_type == "message" and subtype == "rfc822":
        extension_start = 10
        lines = _optional_number(node[9] if len(node) > 9 else None)
    else:
        extension_start = 7
        lines = None
    disposition_node = node[extension_start + 1] if len(node) > extension_start + 1 else None
    disposition_parameters = _disposition_parameters(disposition_node)
    return MimePart(
        section=section or "TEXT",
        content_type=f"{media_type}/{subtype}",
        charset=parameters.get("charset"),
        encoding=_string(node[5]).lower(),
        filename=_filename(parameters, disposition_parameters),
        disposition=_disposition(disposition_node),
        parameters=_immutable_parameters(parameters),
        content_id=_string(node[3]).strip(),
        description=_string(node[4]),
        octets=octets,
        lines=lines,
        md5=_string(node[extension_start] if len(node) > extension_start else None),
        disposition_parameters=_immutable_parameters(disposition_parameters),
        language=_language(
            node[extension_start + 2] if len(node) > extension_start + 2 else None
        ),
        content_location=_string(
            node[extension_start + 3] if len(node) > extension_start + 3 else None
        ),
    )


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _parameters(value: object) -> dict[str, str]:
    if not isinstance(value, list):
        return {}
    result: dict[str, str] = {}
    for index in range(0, len(value) - 1, 2):
        key = _string(value[index]).lower()
        parameter_value = _string(value[index + 1])
        if key:
            result[key] = parameter_value
    return result


def _immutable_parameters(value: Mapping[str, str]) -> Mapping[str, str]:
    return MappingProxyType(dict(value))


def _number(value: object, label: str) -> int:
    if not isinstance(value, int) or value < 0:
        raise BodyStructureError(f"{label} is invalid")
    return value


def _optional_number(value: object) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


def _disposition(value: object) -> str:
    if isinstance(value, list) and value:
        return _string(value[0]).lower()
    return ""


def _disposition_parameters(value: object) -> dict[str, str]:
    if not isinstance(value, list) or len(value) < 2:
        return {}
    return _parameters(value[1])


def _filename(
    parameters: Mapping[str, str], disposition_parameters: Mapping[str, str]
) -> str:
    return (
        disposition_parameters.get("filename")
        or parameters.get("name")
        or ""
    )


def _language(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str) and item)
    return ()


def select_body_plan(root: MimePart, prefer_html: bool = True) -> BodyPlan:
    """Choose body sections according to MIME container semantics.

    The plan contains section descriptors only. It never fetches a payload and never
    treats an attachment, encapsulated message, or encrypted container as readable
    body content.
    """

    plan = _select_part(root, prefer_html)
    return BodyPlan(
        primary=_unique_parts(plan.primary),
        fallback=_unique_parts(plan.fallback),
        attachments=_unique_names(plan.attachments),
        inline_resources=_unique_parts(plan.inline_resources),
        blocked_reason=plan.blocked_reason,
    )


def _select_part(part: MimePart, prefer_html: bool) -> BodyPlan:
    if _is_attachment(part):
        return BodyPlan(attachments=(_attachment_name(part),))
    if _is_encrypted(part):
        return BodyPlan(blocked_reason="encrypted")
    if not part.children:
        if part.content_type == "message/rfc822":
            return BodyPlan(attachments=(_attachment_name(part),))
        if part.content_type in {"text/plain", "text/html"}:
            return BodyPlan(primary=(part,))
        if _is_inline_resource(part):
            return BodyPlan(inline_resources=(part,))
        return BodyPlan()

    if part.content_type == "multipart/alternative":
        return _select_alternative(part, prefer_html)
    if part.content_type == "multipart/related":
        return _select_related(part, prefer_html)
    if part.content_type == "multipart/mixed":
        return _select_mixed(part, prefer_html)
    metadata = _metadata_only(part)
    return BodyPlan(
        attachments=metadata.attachments,
        inline_resources=metadata.inline_resources,
        blocked_reason="unsupported",
    )


def _select_alternative(part: MimePart, prefer_html: bool) -> BodyPlan:
    child_plans = tuple(_select_part(child, prefer_html) for child in part.children)
    html: tuple[MimePart, ...] = ()
    plain: tuple[MimePart, ...] = ()
    for child_plan in child_plans:
        child_html = _parts_of_type(child_plan, "text/html")
        child_plain = _parts_of_type(child_plan, "text/plain")
        if child_html:
            html = child_html
        if child_plain:
            plain = child_plain

    primary = html if prefer_html and html else plain or html
    fallback = plain if primary == html else html
    return BodyPlan(
        primary=primary,
        fallback=fallback,
        attachments=_merge_names(plan.attachments for plan in child_plans),
        inline_resources=_merge_parts(
            plan.inline_resources for plan in child_plans
        ),
        blocked_reason=_blocked_without_body(child_plans, primary),
    )


def _select_related(part: MimePart, prefer_html: bool) -> BodyPlan:
    root = _related_root(part)
    if root is None:
        return BodyPlan()
    body = _select_part(root, prefer_html)
    attachments = list(body.attachments)
    inline_resources = list(body.inline_resources)
    for child in part.children:
        if child is root:
            continue
        resources = _related_resources(child)
        attachments.extend(resources.attachments)
        inline_resources.extend(resources.inline_resources)
    return BodyPlan(
        primary=body.primary,
        fallback=body.fallback,
        attachments=tuple(attachments),
        inline_resources=tuple(inline_resources),
        blocked_reason=body.blocked_reason,
    )


def _select_mixed(part: MimePart, prefer_html: bool) -> BodyPlan:
    primary: tuple[MimePart, ...] = ()
    fallback: tuple[MimePart, ...] = ()
    attachments: list[str] = []
    inline_resources: list[MimePart] = []
    blocked_reason = ""
    for child in part.children:
        child_plan = _select_part(child, prefer_html)
        attachments.extend(child_plan.attachments)
        inline_resources.extend(child_plan.inline_resources)
        if not primary and child_plan.primary:
            primary = child_plan.primary
            fallback = child_plan.fallback
        elif not primary and child_plan.blocked_reason and not blocked_reason:
            blocked_reason = child_plan.blocked_reason
    return BodyPlan(
        primary=primary,
        fallback=fallback,
        attachments=tuple(attachments),
        inline_resources=tuple(inline_resources),
        blocked_reason="" if primary else blocked_reason,
    )


def _related_root(part: MimePart) -> MimePart | None:
    if not part.children:
        return None
    start = _normalize_content_id(part.parameters.get("start", ""))
    if start:
        for child in part.children:
            if _normalize_content_id(child.content_id) == start:
                return child
    return part.children[0]


def _related_resources(part: MimePart) -> BodyPlan:
    if _is_attachment(part) or part.content_type == "message/rfc822":
        return BodyPlan(attachments=(_attachment_name(part),))
    if part.children:
        attachments: list[str] = []
        resources: list[MimePart] = []
        for child in part.children:
            nested = _related_resources(child)
            attachments.extend(nested.attachments)
            resources.extend(nested.inline_resources)
        return BodyPlan(
            attachments=tuple(attachments), inline_resources=tuple(resources)
        )
    if _is_encrypted(part):
        return BodyPlan()
    return BodyPlan(inline_resources=(part,))


def _metadata_only(part: MimePart) -> BodyPlan:
    if _is_attachment(part) or part.content_type == "message/rfc822":
        return BodyPlan(attachments=(_attachment_name(part),))
    if part.children:
        attachments: list[str] = []
        resources: list[MimePart] = []
        for child in part.children:
            nested = _metadata_only(child)
            attachments.extend(nested.attachments)
            resources.extend(nested.inline_resources)
        return BodyPlan(
            attachments=tuple(attachments),
            inline_resources=tuple(resources),
        )
    if _is_inline_resource(part):
        return BodyPlan(inline_resources=(part,))
    return BodyPlan()


def _is_attachment(part: MimePart) -> bool:
    if part.disposition == "attachment":
        return True
    return bool(part.filename) and part.disposition != "inline"


def _is_inline_resource(part: MimePart) -> bool:
    return bool(
        part.disposition == "inline"
        or part.content_id
        or part.content_location
    )


def _is_encrypted(part: MimePart) -> bool:
    if part.content_type == "multipart/encrypted":
        return True
    if part.content_type in {
        "application/pgp-encrypted",
        "application/pkcs7-mime",
        "application/x-pkcs7-mime",
    }:
        smime_type = part.parameters.get("smime-type", "").lower()
        return part.content_type == "application/pgp-encrypted" or smime_type in {
            "",
            "enveloped-data",
        }
    return False


def _attachment_name(part: MimePart) -> str:
    return part.filename or "未命名附件"


def _parts_of_type(plan: BodyPlan, content_type: str) -> tuple[MimePart, ...]:
    primary = tuple(part for part in plan.primary if part.content_type == content_type)
    if primary:
        return primary
    return tuple(part for part in plan.fallback if part.content_type == content_type)


def _blocked_without_body(plans: Iterable[BodyPlan], body: tuple[MimePart, ...]) -> str:
    if body:
        return ""
    return next((plan.blocked_reason for plan in plans if plan.blocked_reason), "")


def _normalize_content_id(value: str) -> str:
    return value.strip().strip("<>").casefold()


def _merge_names(groups: Iterable[Iterable[str]]) -> tuple[str, ...]:
    return tuple(name for group in groups for name in group)


def _merge_parts(groups: Iterable[Iterable[MimePart]]) -> tuple[MimePart, ...]:
    return tuple(part for group in groups for part in group)


def _unique_names(names: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            result.append(name)
    return tuple(result)


def _unique_parts(parts: Iterable[MimePart]) -> tuple[MimePart, ...]:
    seen: set[tuple[str, str, str]] = set()
    result: list[MimePart] = []
    for part in parts:
        key = (part.section, part.content_type, part.content_id)
        if key not in seen:
            seen.add(key)
            result.append(part)
    return tuple(result)


def safe_text_parts(root: MimePart) -> tuple[tuple[MimePart, ...], tuple[str, ...]]:
    plain: list[MimePart] = []
    html: list[MimePart] = []
    attachments: list[str] = []

    def visit(part: MimePart) -> None:
        if part.children:
            for child in part.children:
                visit(child)
            return
        is_attachment = part.disposition == "attachment" or bool(part.filename)
        if is_attachment:
            attachments.append(part.filename or "未命名附件")
            return
        if part.content_type == "text/plain":
            plain.append(part)
        elif part.content_type == "text/html":
            html.append(part)

    visit(root)
    return tuple(plain or html), tuple(attachments)


def extract_fetch_payload(rows: Iterable[object]) -> bytes:
    payload = bytearray()
    for row in rows:
        if isinstance(row, tuple) and len(row) >= 2 and isinstance(row[1], bytes):
            payload.extend(row[1])
    return bytes(payload)


def decode_transfer(payload: bytes, encoding: str) -> bytes:
    normalized = encoding.lower()
    if normalized == "base64":
        try:
            return base64.b64decode(payload, validate=False)
        except (ValueError, TypeError):
            return payload
    if normalized in {"quoted-printable", "quopri"}:
        return quopri.decodestring(payload)
    return payload
