"""Sanitize HTML email bodies for isolated, resource-free rendering."""

from __future__ import annotations

import html
import io
import ipaddress
import math
import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlsplit

import nh3
import tinycss2


MAX_INPUT_BYTES = 5 * 1024 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
HTML_POLICY_VERSION = "mail-html-v1"


class HtmlSanitizationError(ValueError):
    """Raised when an HTML body cannot be sanitized safely."""


class HtmlSizeLimitError(HtmlSanitizationError):
    """Raised when an HTML body exceeds the configured safety limits."""


@dataclass(frozen=True)
class SanitizedEmailHtml:
    safe_html: str
    plain_text: str
    blocked_images: int


_ALLOWED_TAGS = {
    "a",
    "abbr",
    "article",
    "b",
    "bdi",
    "blockquote",
    "br",
    "caption",
    "center",
    "code",
    "col",
    "colgroup",
    "dd",
    "del",
    "div",
    "dl",
    "dt",
    "em",
    "figcaption",
    "figure",
    "font",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "i",
    "img",
    "li",
    "main",
    "ol",
    "p",
    "pre",
    "q",
    "s",
    "section",
    "small",
    "span",
    "strike",
    "strong",
    "sub",
    "sup",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
    "wbr",
}

_CLEAN_CONTENT_TAGS = {
    "applet",
    "audio",
    "canvas",
    "embed",
    "head",
    "iframe",
    "math",
    "noembed",
    "noframes",
    "object",
    "script",
    "style",
    "svg",
    "template",
    "video",
}

_GLOBAL_ATTRIBUTES = {"dir", "lang", "style", "title"}
_TAG_ATTRIBUTES = {
    "a": {"href", "title"},
    "blockquote": {"title"},
    "col": {"align", "span", "valign", "width"},
    "colgroup": {"align", "span", "valign", "width"},
    "font": {"color", "face", "size"},
    "hr": {"align", "color", "size", "width"},
    "img": {"alt", "height", "style", "title", "width"},
    "li": {"value"},
    "ol": {"start", "type"},
    "table": {
        "align",
        "bgcolor",
        "border",
        "cellpadding",
        "cellspacing",
        "height",
        "summary",
        "valign",
        "width",
    },
    "tbody": {"align", "valign"},
    "td": {
        "align",
        "bgcolor",
        "colspan",
        "height",
        "rowspan",
        "valign",
        "width",
    },
    "tfoot": {"align", "valign"},
    "th": {
        "align",
        "bgcolor",
        "colspan",
        "height",
        "rowspan",
        "scope",
        "valign",
        "width",
    },
    "thead": {"align", "valign"},
    "tr": {"align", "bgcolor", "height", "valign"},
    "ul": {"type"},
}
_ALLOWED_ATTRIBUTES = {
    tag: _GLOBAL_ATTRIBUTES | _TAG_ATTRIBUTES.get(tag, set())
    for tag in _ALLOWED_TAGS
}

_ALLOWED_CSS_PROPERTIES = {
    "background",
    "background-color",
    "border",
    "border-bottom",
    "border-bottom-color",
    "border-bottom-left-radius",
    "border-bottom-right-radius",
    "border-bottom-style",
    "border-bottom-width",
    "border-collapse",
    "border-color",
    "border-left",
    "border-left-color",
    "border-left-style",
    "border-left-width",
    "border-radius",
    "border-right",
    "border-right-color",
    "border-right-style",
    "border-right-width",
    "border-spacing",
    "border-style",
    "border-top",
    "border-top-color",
    "border-top-left-radius",
    "border-top-right-radius",
    "border-top-style",
    "border-top-width",
    "border-width",
    "box-sizing",
    "clear",
    "color",
    "display",
    "float",
    "font-family",
    "font-size",
    "font-style",
    "font-variant",
    "font-weight",
    "height",
    "letter-spacing",
    "line-height",
    "margin",
    "margin-bottom",
    "margin-left",
    "margin-right",
    "margin-top",
    "max-height",
    "max-width",
    "min-height",
    "min-width",
    "opacity",
    "overflow-wrap",
    "padding",
    "padding-bottom",
    "padding-left",
    "padding-right",
    "padding-top",
    "table-layout",
    "text-align",
    "text-decoration",
    "text-transform",
    "vertical-align",
    "visibility",
    "white-space",
    "width",
    "word-break",
    "word-wrap",
    "word-spacing",
}

_SAFE_CSS_FUNCTIONS = {"calc", "clamp", "hsl", "hsla", "max", "min", "rgb", "rgba"}
_UNSAFE_CSS_FUNCTIONS = {
    "attr",
    "cross-fade",
    "element",
    "expression",
    "image",
    "image-set",
    "paint",
    "url",
    "var",
    "-webkit-image-set",
}
_VOID_TAGS = {"br", "col", "hr", "img", "wbr"}
_BLOCK_TAGS = {
    "article",
    "blockquote",
    "caption",
    "center",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "figure",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "li",
    "main",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "tbody",
    "tfoot",
    "thead",
    "tr",
    "ul",
}
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)
_SPACE_LIKE = str.maketrans({"\u00a0": " ", "\u2007": " ", "\u202f": " "})
_LANG_RE = re.compile(r"^[A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8})*$")
_UNSAFE_HOSTNAME_CODEPOINT_RANGES = (
    # Unicode Default_Ignorable_Code_Point values not covered by categories.
    (0x034F, 0x034F),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFFA0, 0xFFA0),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0100, 0xE01EF),
    (0xE0000, 0xE0FFF),
)


def sanitize_email_html(raw_html: str) -> SanitizedEmailHtml:
    """Return inert HTML plus a normalized text fallback.

    The result contains no automatically fetched resource URLs. The only URL
    attribute retained is an absolute HTTP(S) link on ``a[href]``.
    """

    if not isinstance(raw_html, str):
        raise TypeError("raw_html must be str")
    normalized_input = raw_html.replace("\x00", "\ufffd")
    try:
        input_size = len(normalized_input.encode("utf-8"))
    except UnicodeEncodeError:
        normalized_input = normalized_input.encode(
            "utf-8", errors="replace"
        ).decode("utf-8")
        input_size = len(normalized_input.encode("utf-8"))
    if input_size > MAX_INPUT_BYTES:
        raise HtmlSizeLimitError(
            f"HTML input exceeds {MAX_INPUT_BYTES} bytes"
        )

    try:
        cleaned = nh3.clean(
            normalized_input,
            tags=_ALLOWED_TAGS,
            clean_content_tags=_CLEAN_CONTENT_TAGS,
            attributes=_ALLOWED_ATTRIBUTES,
            attribute_filter=_filter_attribute,
            url_schemes={"http", "https"},
            strip_comments=True,
        )
    except Exception as exc:
        raise HtmlSanitizationError(f"HTML sanitization failed: {exc}") from exc

    image_filter = _ImageFilter()
    image_filter.feed(cleaned)
    image_filter.close()
    safe_html = image_filter.html()
    if len(safe_html.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise HtmlSizeLimitError(
            f"sanitized HTML exceeds {MAX_OUTPUT_BYTES} bytes"
        )

    text_extractor = _PlainTextExtractor()
    text_extractor.feed(safe_html)
    text_extractor.close()
    plain_text = normalize_plain_text(text_extractor.text())
    if len(plain_text.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise HtmlSizeLimitError(
            f"plain-text output exceeds {MAX_OUTPUT_BYTES} bytes"
        )
    return SanitizedEmailHtml(safe_html, plain_text, image_filter.blocked_images)


def normalize_plain_text(value: str) -> str:
    """Normalize text whitespace without changing literal HTML entities."""

    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("\u2028", "\n").replace("\u2029", "\n")
    value = value.translate(_SPACE_LIKE).translate(_ZERO_WIDTH)
    lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in value.split("\n")]
    result: list[str] = []
    blank = False
    for line in lines:
        if line:
            result.append(line)
            blank = False
        elif result and not blank:
            result.append("")
            blank = True
    while result and not result[-1]:
        result.pop()
    return "\n".join(result)


def _filter_attribute(tag: str, attribute: str, value: str) -> str | None:
    tag = tag.lower()
    attribute = attribute.lower()
    if attribute == "style":
        return _sanitize_style(value)
    if attribute == "href":
        return _safe_http_url(value) if tag == "a" else None
    if attribute == "dir":
        normalized = value.strip().lower()
        return normalized if normalized in {"auto", "ltr", "rtl"} else None
    if attribute == "lang":
        normalized = value.strip()
        return normalized if _LANG_RE.fullmatch(normalized) else None
    if attribute in {"title", "summary", "alt", "face"}:
        normalized = " ".join(normalize_plain_text(value).splitlines())
        return normalized[:500] if normalized else None
    if attribute in {"align", "valign", "scope", "type"}:
        return _safe_keyword(attribute, value)
    if attribute in {
        "border",
        "cellpadding",
        "cellspacing",
        "colspan",
        "height",
        "rowspan",
        "size",
        "span",
        "start",
        "value",
        "width",
    }:
        return _safe_dimension_attribute(attribute, value)
    if attribute in {"bgcolor", "color"}:
        return _safe_color(value)
    return None


def _safe_http_url(value: str) -> str | None:
    normalized = "".join(
        character
        for character in value.strip()
        if ord(character) >= 0x20 and ord(character) != 0x7F
    )
    if len(normalized) > 4096 or "\\" in normalized:
        return None
    try:
        parsed = urlsplit(normalized)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    hostname = (parsed.hostname or "").rstrip(".").casefold()
    if not hostname or _hostname_has_unsafe_unicode(hostname):
        return None
    try:
        canonical_hostname = (
            hostname.encode("idna").decode("ascii").rstrip(".").casefold()
        )
    except UnicodeError:
        return None
    if (
        not canonical_hostname
        or "%" in canonical_hostname
        or canonical_hostname == "localhost"
        or canonical_hostname.endswith((".localhost", ".local"))
    ):
        return None
    try:
        address = ipaddress.ip_address(canonical_hostname)
    except ValueError:
        if (
            re.fullmatch(r"[0-9.]+", canonical_hostname)
            or any(
                re.fullmatch(r"0x[0-9a-f]+", label)
                for label in canonical_hostname.split(".")
            )
        ):
            return None
    else:
        if (
            not address.is_global
            or address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
            or bool(getattr(address, "is_site_local", False))
        ):
            return None
    safe_host = (
        f"[{canonical_hostname}]"
        if ":" in canonical_hostname
        else canonical_hostname
    )
    safe_netloc = f"{safe_host}:{port}" if port is not None else safe_host
    return parsed._replace(
        scheme=parsed.scheme.lower(), netloc=safe_netloc
    ).geturl()


def _hostname_has_unsafe_unicode(hostname: str) -> bool:
    for character in hostname:
        codepoint = ord(character)
        if codepoint < 0x80:
            continue
        if unicodedata.category(character) in {
            "Cc",
            "Cf",
            "Cn",
            "Co",
            "Cs",
            "Zl",
            "Zp",
            "Zs",
        }:
            return True
        if any(
            start <= codepoint <= end
            for start, end in _UNSAFE_HOSTNAME_CODEPOINT_RANGES
        ):
            return True
    return False


def _safe_keyword(attribute: str, value: str) -> str | None:
    normalized = value.strip().lower()
    allowed = {
        "align": {"center", "char", "justify", "left", "right"},
        "scope": {"col", "colgroup", "row", "rowgroup"},
        "type": {"1", "a", "i", "circle", "disc", "square"},
        "valign": {"baseline", "bottom", "middle", "top"},
    }
    return normalized if normalized in allowed.get(attribute, set()) else None


def _safe_dimension_attribute(attribute: str, value: str) -> str | None:
    normalized = value.strip().lower()
    if attribute == "size" and re.fullmatch(r"[+-]?[1-7]", normalized):
        return normalized
    match = re.fullmatch(r"(\d{1,5})(%)?", normalized)
    if not match:
        return None
    number = int(match.group(1))
    maximum = 100 if attribute in {"colspan", "rowspan", "span"} else 4096
    if number > maximum:
        return None
    if match.group(2) and number > 1000:
        return None
    return normalized


def _safe_color(value: str) -> str | None:
    normalized = value.strip()
    if re.fullmatch(r"#[0-9A-Fa-f]{3,8}", normalized):
        return normalized
    if re.fullmatch(r"[A-Za-z]{1,24}", normalized):
        return normalized.lower()
    return None


def _sanitize_style(value: str) -> str | None:
    declarations = tinycss2.parse_declaration_list(
        value, skip_comments=True, skip_whitespace=True
    )
    safe: list[str] = []
    for declaration in declarations:
        if declaration.type != "declaration":
            continue
        name = declaration.lower_name
        if name not in _ALLOWED_CSS_PROPERTIES:
            continue
        if not _css_tokens_are_safe(declaration.value):
            continue
        serialized = tinycss2.serialize(declaration.value).strip()
        if not serialized or not _css_value_is_reasonable(name, declaration.value):
            continue
        important = " !important" if declaration.important else ""
        safe.append(f"{name}: {serialized}{important}")
    return "; ".join(safe) if safe else None


def _css_tokens_are_safe(tokens: list[object]) -> bool:
    for token in tokens:
        token_type = getattr(token, "type", "")
        if token_type in {"at-keyword", "bad-string", "bad-url", "url"}:
            return False
        if token_type == "function":
            name = getattr(token, "lower_name", getattr(token, "name", "")).lower()
            if name in _UNSAFE_CSS_FUNCTIONS or name not in _SAFE_CSS_FUNCTIONS:
                return False
            if not _css_tokens_are_safe(getattr(token, "arguments", [])):
                return False
    return True


def _css_value_is_reasonable(name: str, tokens: list[object]) -> bool:
    numbers: list[tuple[float, str]] = []
    for token in tokens:
        token_type = getattr(token, "type", "")
        if token_type in {"dimension", "number", "percentage"}:
            number = float(getattr(token, "value", 0.0))
            if not math.isfinite(number):
                return False
            unit = "%" if token_type == "percentage" else getattr(token, "lower_unit", "")
            numbers.append((abs(number), unit))
        elif token_type == "function":
            if not _css_value_is_reasonable(name, getattr(token, "arguments", [])):
                return False

    if name == "opacity":
        return all(number <= 1 for number, _unit in numbers)
    if name == "font-size":
        return all(
            _within_size(number, unit, px=96, relative=8, percent=300)
            for number, unit in numbers
        )
    if name == "line-height":
        return all(
            _within_size(number, unit, px=192, relative=10, percent=1000)
            for number, unit in numbers
        )
    if name.startswith("margin") or name.startswith("padding") or name in {
        "border-spacing",
        "letter-spacing",
        "word-spacing",
    }:
        return all(
            _within_size(number, unit, px=512, relative=32, percent=500)
            for number, unit in numbers
        )
    return all(
        _within_size(number, unit, px=4096, relative=256, percent=1000)
        for number, unit in numbers
    )


def _within_size(
    number: float,
    unit: str,
    *,
    px: float,
    relative: float,
    percent: float,
) -> bool:
    if unit == "%":
        return number <= percent
    if unit in {"em", "rem", "ex", "ch", "lh", "rlh"}:
        return number <= relative
    if unit in {
        "vh",
        "vw",
        "vmin",
        "vmax",
        "svh",
        "svw",
        "lvh",
        "lvw",
        "dvh",
        "dvw",
    }:
        return number <= 200
    absolute_units = {
        "": 1.0,
        "px": 1.0,
        "pt": 96.0 / 72.0,
        "pc": 16.0,
        "in": 96.0,
        "cm": 96.0 / 2.54,
        "mm": 96.0 / 25.4,
        "q": 96.0 / 101.6,
    }
    factor = absolute_units.get(unit)
    return factor is not None and number * factor <= px


class _ImageFilter(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts = io.StringIO()
        self.output_bytes = 0
        self.blocked_images = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, self_closing=tag in _VOID_TAGS)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, self_closing=True)

    def handle_endtag(self, tag: str) -> None:
        if tag not in _VOID_TAGS:
            self._append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self._append(html.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        self._append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self._append(f"&#{name};")

    def html(self) -> str:
        return self.parts.getvalue()

    def _append(self, fragment: str) -> None:
        self.output_bytes += len(fragment.encode("utf-8"))
        if self.output_bytes > MAX_OUTPUT_BYTES:
            raise HtmlSizeLimitError(
                f"sanitized HTML exceeds {MAX_OUTPUT_BYTES} bytes"
            )
        self.parts.write(fragment)

    def _start(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
        *,
        self_closing: bool,
    ) -> None:
        attributes = {name: value or "" for name, value in attrs}
        if tag == "img":
            self.blocked_images += 1
            if _is_tracking_image(attributes):
                return
            alt = normalize_plain_text(attributes.get("alt", ""))[:300]
            label = "图片未加载" + (f"：{alt}" if alt else "")
            escaped = html.escape(label)
            aria = html.escape(label, quote=True)
            self._append(
                f'<span class="mail-image-placeholder" role="img" aria-label="{aria}">{escaped}</span>'
            )
            return
        if tag == "a":
            safe_attributes = [
                (name, value)
                for name, value in attrs
                if name not in {"referrerpolicy", "rel", "target"}
            ]
            if attributes.get("href"):
                safe_attributes.extend(
                    (
                        ("rel", "noopener noreferrer"),
                        ("referrerpolicy", "no-referrer"),
                        ("target", "_blank"),
                    )
                )
            attrs = safe_attributes
        serialized_attrs = "".join(
            f' {html.escape(name, quote=True)}="{html.escape(value, quote=True)}"'
            for name, value in attrs
            if value is not None
        )
        if self_closing and tag not in _VOID_TAGS:
            self._append(f"<{tag}{serialized_attrs}></{tag}>")
        else:
            self._append(f"<{tag}{serialized_attrs}>")


def _is_tracking_image(attributes: dict[str, str]) -> bool:
    width = _image_dimension(attributes.get("width", ""))
    height = _image_dimension(attributes.get("height", ""))
    style = attributes.get("style", "").lower()
    style_dimensions = _style_dimensions(style)
    width = style_dimensions.get("width", width)
    height = style_dimensions.get("height", height)
    hidden = (
        re.search(
            r"(?:^|;)\s*display\s*:\s*none(?:\s*!important)?\s*(?:;|$)",
            style,
        )
        or re.search(
            r"(?:^|;)\s*visibility\s*:\s*(?:hidden|collapse)"
            r"(?:\s*!important)?\s*(?:;|$)",
            style,
        )
        or re.search(
            r"(?:^|;)\s*opacity\s*:\s*(?:0(?:\.0+)?|\.0+)"
            r"(?:\s*!important)?\s*(?:;|$)",
            style,
        )
    )
    tiny = (
        width is not None
        and height is not None
        and width <= 2
        and height <= 2
    )
    return bool(hidden or tiny)


def _style_dimensions(style: str) -> dict[str, float]:
    result: dict[str, float] = {}
    for declaration in tinycss2.parse_declaration_list(
        style, skip_comments=True, skip_whitespace=True
    ):
        if (
            declaration.type != "declaration"
            or declaration.lower_name not in {"width", "height"}
        ):
            continue
        for token in declaration.value:
            if token.type in {"dimension", "number"}:
                result[declaration.lower_name] = abs(float(token.value))
                break
    return result


def _image_dimension(value: str) -> float | None:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)(?:px)?\s*", value, re.IGNORECASE)
    return float(match.group(1)) if match else None


class _PlainTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[tuple[str, int]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value or "" for name, value in attrs}
        if tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n• ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "a":
            self.links.append((attributes.get("href", ""), len(self.parts)))

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"}:
            self.parts.append("\t")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "a" and self.links:
            href, start = self.links.pop()
            visible = "".join(self.parts[start:]).strip()
            if href and href not in visible:
                self.parts.append(f" ({href})" if visible else href)

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


__all__ = [
    "HTML_POLICY_VERSION",
    "MAX_INPUT_BYTES",
    "MAX_OUTPUT_BYTES",
    "HtmlSanitizationError",
    "HtmlSizeLimitError",
    "SanitizedEmailHtml",
    "normalize_plain_text",
    "sanitize_email_html",
]
