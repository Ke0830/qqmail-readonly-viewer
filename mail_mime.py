"""Minimal IMAP BODYSTRUCTURE parsing for attachment-safe text retrieval."""

from __future__ import annotations

import base64
import quopri
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class MimePart:
    section: str
    content_type: str
    charset: str | None
    encoding: str
    filename: str
    disposition: str
    children: tuple["MimePart", ...] = ()


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
        children = tuple(
            _part(child, f"{section}.{child_index}" if section else str(child_index))
            for child_index, child in enumerate(child_nodes, start=1)
        )
        disposition = _disposition(node[index + 2] if len(node) > index + 2 else None)
        return MimePart(
            section=section,
            content_type=f"multipart/{_string(node[index]).lower()}",
            charset=None,
            encoding="",
            filename=_filename(
                node[index + 1] if len(node) > index + 1 else None,
                node[index + 2] if len(node) > index + 2 else None,
            ),
            disposition=disposition,
            children=children,
        )

    if len(node) < 7:
        raise BodyStructureError("single-part BODYSTRUCTURE is incomplete")
    media_type = _string(node[0]).lower()
    subtype = _string(node[1]).lower()
    parameters = node[2]
    if media_type == "text":
        extension_start = 8
    elif media_type == "message" and subtype == "rfc822":
        extension_start = 10
    else:
        extension_start = 7
    disposition_node = node[extension_start + 1] if len(node) > extension_start + 1 else None
    return MimePart(
        section=section or "TEXT",
        content_type=f"{media_type}/{subtype}",
        charset=_parameter(parameters, "charset"),
        encoding=_string(node[5]).lower(),
        filename=_filename(parameters, disposition_node),
        disposition=_disposition(disposition_node),
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


def _parameter(value: object, name: str) -> str | None:
    return _parameters(value).get(name.lower())


def _disposition(value: object) -> str:
    if isinstance(value, list) and value:
        return _string(value[0]).lower()
    return ""


def _filename(parameters: object, disposition: object) -> str:
    disposition_parameters = (
        disposition[1] if isinstance(disposition, list) and len(disposition) > 1 else None
    )
    return (
        _parameter(disposition_parameters, "filename")
        or _parameter(parameters, "name")
        or ""
    )


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
