#!/usr/bin/env python3
"""Local, read-only IMAP mail viewer.

Credentials are stored in the macOS login keychain or Windows Credential
Locker. Mail is fetched over IMAPS and BODY.PEEK is used so viewing a message
does not mark it as read.
"""

from __future__ import annotations

import argparse
import email
import getpass
import html
import imaplib
import json
import re
import ssl
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from email.header import Header, decode_header
from email.message import Message
from datetime import timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterable
from urllib.parse import parse_qs, urlencode, urlparse


# Kept as public compatibility constants for integrations that imported them.
IMAP_HOST = "imap.qq.com"
IMAP_PORT = 993
KEYCHAIN_ACCOUNT = "qqmail-viewer"
EMAIL_SERVICE = "codex.qqmail-viewer.email"
AUTH_SERVICE = "codex.qqmail-viewer.authorization-code"
ACCOUNT_INDEX_SERVICE = "codex.qqmail-viewer.account-index.v2"
DEFAULT_LIMIT = 30
MAX_LIMIT = 100
CHINA_TIMEZONE = timezone(timedelta(hours=8))
NETEASE_IMAP_HOSTS = frozenset({"imap.163.com", "imap.126.com", "imap.yeah.net"})


class ViewerError(RuntimeError):
    """An expected, user-facing error."""


@dataclass(frozen=True)
class Provider:
    """A supported encrypted-IMAP provider profile."""

    id: str
    label: str
    host: str | None
    port: int | None
    domains: tuple[str, ...] = ()
    credential_label: str = "授权码或应用专用密码"


PROVIDERS: dict[str, Provider] = {
    "qq": Provider("qq", "QQ / Foxmail", "imap.qq.com", 993, ("qq.com", "foxmail.com"), "QQ 邮箱授权码"),
    "163": Provider("163", "网易 163", "imap.163.com", 993, ("163.com",), "网易邮箱客户端授权码"),
    "126": Provider("126", "网易 126", "imap.126.com", 993, ("126.com",), "网易邮箱客户端授权码"),
    "yeah": Provider("yeah", "网易 yeah", "imap.yeah.net", 993, ("yeah.net",), "网易邮箱客户端授权码"),
    "icloud": Provider("icloud", "iCloud Mail", "imap.mail.me.com", 993, ("icloud.com", "me.com", "mac.com"), "Apple 应用专用密码"),
    "gmail": Provider("gmail", "Gmail", "imap.gmail.com", 993, ("gmail.com", "googlemail.com"), "Google 应用专用密码"),
    "custom": Provider("custom", "自定义 IMAPS", None, None),
}


@dataclass(frozen=True)
class Account:
    """Non-secret account metadata. The credentials live in the OS store."""

    name: str
    provider: str
    email: str
    host: str
    port: int
    email_service: str
    auth_service: str
    is_default: bool = False

    @property
    def provider_label(self) -> str:
        return PROVIDERS.get(self.provider, PROVIDERS["custom"]).label

    def public_record(self) -> dict[str, object]:
        return {
            "name": self.name,
            "provider": self.provider,
            "provider_label": self.provider_label,
            "email": self.email,
            "default": self.is_default,
        }


@dataclass(frozen=True)
class AccountMailSummary:
    """A summary with explicit account ownership for aggregate views."""

    account: Account
    message: "MailSummary"


def _configure_standard_streams() -> None:
    """Use UTF-8 for CLI output, including redirected Windows terminals."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "head"}:
            self.ignored_depth += 1
        elif not self.ignored_depth and tag in {"br", "p", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "head"} and self.ignored_depth:
            self.ignored_depth -= 1
        elif not self.ignored_depth and tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)

    def text(self) -> str:
        value = html.unescape("".join(self.parts))
        value = re.sub(r"[ \t]+", " ", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()


@dataclass(frozen=True)
class MailSummary:
    uid: str
    subject: str
    sender: str
    date: str
    size: int


@dataclass(frozen=True)
class MailDetail:
    uid: str
    subject: str
    sender: str
    recipients: str
    date: str
    text: str
    attachments: tuple[str, ...]


@dataclass(frozen=True)
class MailPage:
    """A positioned slice of the currently selected mailbox view."""

    messages: tuple[MailSummary, ...]
    total: int
    offset: int
    limit: int

    @property
    def page_count(self) -> int:
        return (self.total + self.limit - 1) // self.limit if self.total else 0

    @property
    def current_page(self) -> int:
        return self.offset // self.limit + 1 if self.total else 0


@dataclass(frozen=True)
class ListingParams:
    """Normalized list-view parameters from a browser query string."""

    unread_only: bool
    limit: int
    offset: int
    requested_page: int
    invalid_page: bool = False


def parse_listing_params(query: dict[str, list[str]]) -> ListingParams:
    """Accept new page URLs while keeping existing offset URLs usable."""
    unread_only = query.get("unread", ["1"])[0] != "0"
    try:
        limit = min(max(int(query.get("limit", [str(DEFAULT_LIMIT)])[0]), 1), MAX_LIMIT)
    except ValueError:
        limit = DEFAULT_LIMIT

    page_value = query.get("page", [None])[0]
    if page_value is not None:
        try:
            requested_page = int(page_value)
        except ValueError:
            requested_page = 1
            return ListingParams(unread_only, limit, 0, requested_page, invalid_page=True)
        if requested_page < 1:
            return ListingParams(unread_only, limit, 0, 1, invalid_page=True)
        return ListingParams(unread_only, limit, (requested_page - 1) * limit, requested_page)

    try:
        offset = max(int(query.get("offset", ["0"])[0]), 0)
    except ValueError:
        offset = 0
    return ListingParams(unread_only, limit, offset, offset // limit + 1)


def listing_url(unread_only: bool, limit: int, offset: int, account: str | None = None) -> str:
    """Build the canonical, one-based page URL used by the web interface."""
    page_number = offset // limit + 1
    values: dict[str, object] = {"unread": "1" if unread_only else "0", "limit": limit, "page": page_number}
    if account:
        values["account"] = account
    query = urlencode(values)
    return f"/?{query}"


def sender_parts(sender: str) -> tuple[str, str]:
    """Split a decoded From header into a readable name and address."""
    name, address = parseaddr(sender)
    if address:
        return (name or address, address if name else "")
    return (sender or "（未知发件人）", "")


def decode_bytes(payload: bytes, declared_charset: str | None = None) -> str:
    """Decode mail bytes without destroying data before trying Chinese encodings."""
    candidates: list[str] = []
    if declared_charset and declared_charset.lower() not in {"unknown-8bit", "unknown", "x-unknown"}:
        candidates.append(declared_charset)
    candidates.extend(["utf-8", "gb18030", "gbk", "big5", "latin-1"])

    seen: set[str] = set()
    for encoding in candidates:
        normalized = encoding.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        try:
            return payload.decode(encoding, errors="strict")
        except (LookupError, UnicodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def decode_mime(value: str | Header | None) -> str:
    if not value:
        return ""
    parts: list[str] = []
    for chunk, charset in decode_header(value):
        if isinstance(chunk, bytes):
            parts.append(decode_bytes(chunk, charset))
        else:
            parts.append(chunk)
    return "".join(parts).strip()


def parse_date(value: str | None):
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None


def normalize_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = parse_date(value)
        if dt is None:
            return value
        return dt.astimezone(CHINA_TIMEZONE).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError):
        return value


def _decode_payload(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    return decode_bytes(payload, part.get_content_charset())


def extract_message_text(message: Message) -> tuple[str, tuple[str, ...]]:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[str] = []

    for part in message.walk():
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or "").lower()
        filename = decode_mime(part.get_filename())
        if disposition == "attachment" or filename:
            attachments.append(filename or "未命名附件")
            continue
        content_type = part.get_content_type().lower()
        if content_type == "text/plain":
            plain_parts.append(_decode_payload(part))
        elif content_type == "text/html":
            html_parts.append(_decode_payload(part))

    if plain_parts:
        text = "\n\n".join(plain_parts)
    else:
        extractor = _TextExtractor()
        extractor.feed("\n".join(html_parts))
        text = extractor.text()
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{4,}", "\n\n\n", text).strip()
    return text, tuple(attachments)


def _security(args: list[str], *, input_text: str | None = None) -> str:
    try:
        result = subprocess.run(
            ["security", *args],
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ViewerError("未找到 macOS 钥匙串工具 security，无法访问登录钥匙串。") from exc
    if result.returncode != 0:
        reason = result.stderr.strip() or "钥匙串操作失败"
        raise ViewerError(reason)
    return result.stdout.strip()


def _windows_credential_get(service: str) -> str:
    keyring = _windows_keyring()
    try:
        value = keyring.get_password(service, KEYCHAIN_ACCOUNT)
    except Exception as exc:
        raise ViewerError(f"无法访问 Windows 凭据管理器：{exc}") from exc
    if value is None:
        raise ViewerError("尚未配置 QQ 邮箱。请先运行 configure。")
    return value


def _windows_credential_set(service: str, value: str) -> None:
    keyring = _windows_keyring()
    try:
        keyring.set_password(service, KEYCHAIN_ACCOUNT, value)
    except Exception as exc:
        raise ViewerError(f"无法写入 Windows 凭据管理器：{exc}") from exc


def _windows_keyring():
    try:
        import keyring
    except ImportError as exc:
        raise ViewerError("缺少 Windows 凭据库依赖。请重新安装 qqmail-readonly-viewer。") from exc
    backend = keyring.get_keyring()
    if backend.__class__.__module__ != "keyring.backends.Windows":
        backend_name = f"{backend.__class__.__module__}.{backend.__class__.__name__}"
        raise ViewerError(
            "为避免授权码被保存到非系统凭据库，Windows 只允许使用系统凭据管理器。"
            f"当前 keyring 后端为 {backend_name}。"
        )
    return keyring


def _ensure_supported_platform() -> None:
    if sys.platform not in {"darwin", "win32"}:
        raise ViewerError("当前系统不受支持。此查看器目前仅支持 macOS 和 Windows。")


def keychain_get(service: str) -> str:
    _ensure_supported_platform()
    if sys.platform == "win32":
        return _windows_credential_get(service)
    try:
        return _security(["find-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", service, "-w"])
    except ViewerError as exc:
        raise ViewerError("尚未配置邮箱。请先运行 configure。") from exc


def keychain_set(service: str, value: str) -> None:
    _ensure_supported_platform()
    if sys.platform == "win32":
        _windows_credential_set(service, value)
        return
    _security(
        [
            "add-generic-password",
            "-U",
            "-a",
            KEYCHAIN_ACCOUNT,
            "-s",
            service,
            "-w",
            value,
        ]
    )


def _keychain_get_optional(service: str) -> str | None:
    """Return a credential when present, without exposing a missing-item error."""
    try:
        return keychain_get(service)
    except ViewerError:
        return None


def _credential_service(name: str, field: str) -> str:
    return f"codex.qqmail-viewer.account.{name}.{field}"


def _validate_account_name(value: str) -> str:
    name = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", name):
        raise ViewerError("账户名称只能包含小写字母、数字、连字符或下划线，长度为 1–32。")
    return name


def _suggest_account_name(address: str, provider: str) -> str:
    local = address.split("@", 1)[0].lower()
    safe = re.sub(r"[^a-z0-9_-]+", "-", local).strip("-")
    return _validate_account_name((safe or provider)[:32])


def detect_provider(address: str) -> str | None:
    """Infer a preconfigured provider from an email address."""
    address = address.strip().lower()
    if "@" not in address:
        return None
    domain = address.rsplit("@", 1)[1]
    for provider in PROVIDERS.values():
        if domain in provider.domains:
            return provider.id
    return None


def _account_from_record(record: object, default_name: str | None) -> Account:
    if not isinstance(record, dict):
        raise ViewerError("账户索引格式无效。请重新配置邮箱。")
    try:
        name = _validate_account_name(str(record["name"]))
        provider = str(record["provider"])
        email_address = str(record["email"]).strip().lower()
        host = str(record["host"]).strip()
        port = int(record["port"])
        email_service = str(record["email_service"])
        auth_service = str(record["auth_service"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ViewerError("账户索引格式无效。请重新配置邮箱。") from exc
    if provider not in PROVIDERS or not re.fullmatch(r"[^@\s]+@[^@\s]+", email_address):
        raise ViewerError("账户索引包含不支持的邮箱配置。")
    if not host or not 1 <= port <= 65535:
        raise ViewerError("账户索引包含无效的 IMAPS 主机或端口。")
    return Account(name, provider, email_address, host, port, email_service, auth_service, name == default_name)


def _legacy_account() -> Account | None:
    """Expose the old QQ credentials as a virtual default account, untouched."""
    address = _keychain_get_optional(EMAIL_SERVICE)
    secret = _keychain_get_optional(AUTH_SERVICE)
    if not address or not secret:
        return None
    return Account("qq", "qq", address, IMAP_HOST, IMAP_PORT, EMAIL_SERVICE, AUTH_SERVICE, True)


def load_accounts() -> tuple[Account, ...]:
    """Load non-secret account metadata, retaining the legacy QQ setup."""
    raw = _keychain_get_optional(ACCOUNT_INDEX_SERVICE)
    if raw is None:
        legacy = _legacy_account()
        return (legacy,) if legacy else ()
    try:
        index = json.loads(raw)
        records = index["accounts"]
        default_name = index["default"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ViewerError("账户索引无法读取。请重新配置邮箱。") from exc
    if not isinstance(records, list) or not isinstance(default_name, str):
        raise ViewerError("账户索引格式无效。请重新配置邮箱。")
    accounts = tuple(_account_from_record(record, default_name) for record in records)
    if not accounts or len({account.name for account in accounts}) != len(accounts):
        raise ViewerError("账户索引格式无效。请重新配置邮箱。")
    if not any(account.is_default for account in accounts):
        raise ViewerError("账户索引缺少默认账户。请重新配置邮箱。")
    return accounts


def save_accounts(accounts: Iterable[Account]) -> None:
    items = tuple(accounts)
    defaults = [account for account in items if account.is_default]
    if not items or len(defaults) != 1:
        raise ViewerError("账户配置必须且只能有一个默认账户。")
    index = {
        "version": 2,
        "default": defaults[0].name,
        "accounts": [
            {
                "name": account.name,
                "provider": account.provider,
                "email": account.email,
                "host": account.host,
                "port": account.port,
                "email_service": account.email_service,
                "auth_service": account.auth_service,
            }
            for account in items
        ],
    }
    keychain_set(ACCOUNT_INDEX_SERVICE, json.dumps(index, ensure_ascii=False, separators=(",", ":")))


def default_account(accounts: Iterable[Account] | None = None) -> Account:
    items = tuple(load_accounts() if accounts is None else accounts)
    for account in items:
        if account.is_default:
            return account
    raise ViewerError("尚未配置邮箱。请先运行 configure。")


def find_account(name: str | None, accounts: Iterable[Account] | None = None) -> Account:
    items = tuple(load_accounts() if accounts is None else accounts)
    if not items:
        raise ViewerError("尚未配置邮箱。请先运行 configure。")
    if not name:
        return default_account(items)
    for account in items:
        if account.name == name:
            return account
    raise ViewerError(f"未找到账户“{name}”。可用账户：" + "、".join(account.name for account in items))


class QQMailClient:
    """Generic, TLS-only IMAP client (historical class name kept for compatibility)."""

    def __init__(
        self,
        address: str,
        authorization_code: str,
        *,
        host: str = IMAP_HOST,
        port: int = IMAP_PORT,
        provider_label: str = "QQ 邮箱",
    ) -> None:
        self.address = address
        self.authorization_code = authorization_code
        self.host = host
        self.port = port
        self.provider_label = provider_label
        self.connection: imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> "QQMailClient":
        try:
            self.connection = imaplib.IMAP4_SSL(
                self.host,
                self.port,
                ssl_context=ssl.create_default_context(),
                timeout=30,
            )
            self.connection.login(self.address, self.authorization_code)
            self._identify_netease_client()
            status, response = self.connection.select("INBOX", readonly=True)
            if status != "OK":
                reason = _imap_response_text(response)
                self.close()
                suffix = f"服务器响应：{reason}" if reason else "请确认 IMAP 已开启并可访问收件箱。"
                raise ViewerError(f"无法以只读方式打开收件箱。{suffix}")
            return self
        except imaplib.IMAP4.error as exc:
            self.close()
            hint = "请确认已开启 IMAP，并使用授权码或应用专用密码。"
            if self.host == PROVIDERS["gmail"].host:
                hint = "请确认已开启 IMAP、已开启两步验证，并使用 Google 应用专用密码。组织账号、Advanced Protection 或没有应用专用密码的账号需要 OAuth（本版本暂不支持）。"
            elif self.host == PROVIDERS["icloud"].host:
                hint = "请确认使用 Apple 应用专用密码，而不是 Apple 账户密码。"
            raise ViewerError(f"{self.provider_label}登录失败。{hint}") from exc
        except OSError as exc:
            self.close()
            raise ViewerError(f"无法连接 {self.host}:{self.port}：{exc}") from exc

    def _identify_netease_client(self) -> None:
        """Send NetEase a harmless IMAP ID before its read-only mailbox check."""
        if self.host not in NETEASE_IMAP_HOSTS or self.connection is None:
            return
        try:
            self.connection.xatom(
                "ID", '("name" "qqmail-readonly-viewer" "version" "1.2.0")'
            )
        except (imaplib.IMAP4.error, OSError):
            # Some older IMAP endpoints simply do not implement ID. EXAMINE
            # remains the authoritative read-only compatibility check.
            pass

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        if self.connection is None:
            return
        try:
            self.connection.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
        self.connection = None

    def _imap(self) -> imaplib.IMAP4_SSL:
        if self.connection is None:
            raise ViewerError("邮箱连接尚未建立。")
        return self.connection

    def _matching_messages(
        self,
        *,
        unread_only: bool,
        since_hours: float | None = None,
    ) -> list[MailSummary]:
        criteria = "UNSEEN" if unread_only else "ALL"
        status, data = self._imap().uid("search", None, criteria)
        if status != "OK" or not data:
            raise ViewerError("无法搜索收件箱。")
        uids = data[0].split()
        dated_messages: list[tuple[float, int, MailSummary]] = []
        # UID order is not date order after mailbox migration or re-indexing.
        # Fetch headers in bounded batches, then sort by each message's Date.
        for start in range(0, len(uids), 200):
            uid_set = b",".join(uids[start : start + 200]).decode("ascii", errors="ignore")
            status, rows = self._imap().uid(
                "fetch",
                uid_set,
                "(UID BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)] RFC822.SIZE)",
            )
            if status != "OK":
                continue
            for row in rows:
                if not isinstance(row, tuple) or len(row) < 2:
                    continue
                metadata = row[0] if isinstance(row[0], bytes) else b""
                header_bytes = row[1] if isinstance(row[1], bytes) else b""
                uid_match = re.search(rb"UID\s+(\d+)", metadata)
                if not uid_match:
                    continue
                uid = uid_match.group(1).decode("ascii")
                parsed = email.message_from_bytes(header_bytes)
                raw_date = parsed.get("Date")
                date_value = parse_date(raw_date)
                timestamp = date_value.timestamp() if date_value is not None else 0.0
                size_match = re.search(rb"RFC822\.SIZE\s+(\d+)", metadata)
                summary = MailSummary(
                    uid=uid,
                    subject=decode_mime(parsed.get("Subject")) or "（无主题）",
                    sender=decode_mime(parsed.get("From")),
                    date=normalize_date(raw_date),
                    size=int(size_match.group(1)) if size_match else 0,
                )
                dated_messages.append((timestamp, int(uid), summary))
        dated_messages.sort(key=lambda item: (item[0], item[1]), reverse=True)
        if since_hours is not None:
            cutoff = time.time() - since_hours * 60 * 60
            dated_messages = [item for item in dated_messages if item[0] >= cutoff]
        return [item[2] for item in dated_messages]

    def list_messages(
        self,
        *,
        unread_only: bool,
        limit: int | None,
        offset: int = 0,
        since_hours: float | None = None,
    ) -> list[MailSummary]:
        messages = self._matching_messages(unread_only=unread_only, since_hours=since_hours)
        end = offset + limit if limit is not None else None
        return messages[offset:end]

    def list_page(self, *, unread_only: bool, limit: int, offset: int = 0) -> MailPage:
        """Return a browser page and its total, without fetching message bodies."""
        messages = self._matching_messages(unread_only=unread_only)
        total = len(messages)
        last_offset = ((total - 1) // limit) * limit if total else 0
        effective_offset = min(max(offset, 0), last_offset)
        return MailPage(tuple(messages[effective_offset : effective_offset + limit]), total, effective_offset, limit)

    def get_message(self, uid: str) -> MailDetail:
        if not uid.isdigit():
            raise ViewerError("邮件 UID 无效。")
        status, rows = self._imap().uid("fetch", uid, "(BODY.PEEK[])")
        if status != "OK":
            raise ViewerError("无法读取这封邮件。")
        raw_message, _ = _extract_fetch_bytes(rows)
        if not raw_message:
            raise ViewerError("邮件不存在或已被移动。")
        parsed = email.message_from_bytes(raw_message)
        text, attachments = extract_message_text(parsed)
        return MailDetail(
            uid=uid,
            subject=decode_mime(parsed.get("Subject")) or "（无主题）",
            sender=decode_mime(parsed.get("From")),
            recipients=decode_mime(parsed.get("To")),
            date=normalize_date(parsed.get("Date")),
            text=text,
            attachments=attachments,
        )


def _extract_fetch_bytes(rows: Iterable[object]) -> tuple[bytes, bytes]:
    payload = b""
    metadata = b""
    for row in rows:
        if isinstance(row, tuple) and len(row) >= 2:
            metadata += row[0] if isinstance(row[0], bytes) else b""
            payload += row[1] if isinstance(row[1], bytes) else b""
    return payload, metadata


def _imap_response_text(rows: object) -> str:
    """Turn a server response into a compact, safe-to-display diagnostic."""
    if not isinstance(rows, (list, tuple)):
        return ""
    parts: list[str] = []
    for row in rows:
        if isinstance(row, bytes):
            parts.append(decode_bytes(row))
        elif isinstance(row, str):
            parts.append(row)
    return " ".join(" ".join(parts).split())[:300]


def configured_client(account: Account | None = None) -> QQMailClient:
    selected = account or default_account()
    return QQMailClient(
        keychain_get(selected.email_service),
        keychain_get(selected.auth_service),
        host=selected.host,
        port=selected.port,
        provider_label=selected.provider_label,
    )


def _configured_profile(
    provider_id: str | None, address: str, host: str | None, port: int | None
) -> tuple[Provider, str, int]:
    address = address.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+", address):
        raise ViewerError("请输入完整的邮箱地址。")
    resolved = detect_provider(address) if provider_id in {None, "auto"} else provider_id
    if not resolved:
        raise ViewerError("无法自动识别服务商。请使用 --provider custom 并填写 --imap-host。")
    provider = PROVIDERS.get(resolved)
    if provider is None:
        raise ViewerError("不支持的服务商。可选：qq、163、126、yeah、icloud、gmail、custom。")
    if provider.id == "custom":
        resolved_host = (host or "").strip().lower()
        if not resolved_host:
            raise ViewerError("自定义邮箱必须提供 --imap-host，并且只支持加密 IMAPS。")
        resolved_port = 993 if port is None else port
    else:
        if host is not None or port is not None:
            raise ViewerError("预置服务商不需要 --imap-host 或 --port；请使用其受信任的 IMAPS 设置。")
        resolved_host = provider.host or ""
        resolved_port = provider.port or 0
    if not re.fullmatch(r"[a-z0-9.-]+", resolved_host) or not 1 <= resolved_port <= 65535:
        raise ViewerError("IMAPS 主机或端口无效。")
    return provider, resolved_host, resolved_port


def configure(
    address: str,
    *,
    provider_id: str | None = None,
    name: str | None = None,
    host: str | None = None,
    port: int | None = None,
    make_default: bool = False,
) -> Account:
    """Test a read-only connection, then persist only the non-secret profile."""
    _ensure_supported_platform()
    provider, resolved_host, resolved_port = _configured_profile(provider_id, address, host, port)
    address = address.strip().lower()
    account_name = _validate_account_name(name) if name else _suggest_account_name(address, provider.id)
    existing = tuple(load_accounts())
    matching = next((account for account in existing if account.name == account_name), None)
    if matching and matching.provider != provider.id:
        raise ViewerError(f"账户名称“{account_name}”已被 {matching.provider_label} 使用，请换一个 --name。")
    code = getpass.getpass(f"{provider.credential_label}（输入不会显示）：").strip().replace(" ", "")
    if not code:
        raise ViewerError("授权码或应用专用密码不能为空。")
    print("正在测试只读 IMAPS 连接……")
    candidate = Account(
        account_name,
        provider.id,
        address,
        resolved_host,
        resolved_port,
        matching.email_service if matching else _credential_service(account_name, "email"),
        matching.auth_service if matching else _credential_service(account_name, "secret"),
        make_default or not existing or (matching.is_default if matching else False),
    )
    with configured_client_for(candidate, code):
        pass
    keychain_set(candidate.email_service, address)
    keychain_set(candidate.auth_service, code)
    updated: list[Account] = []
    for account in existing:
        if account.name == account_name:
            continue
        updated.append(Account(**{**account.__dict__, "is_default": False if candidate.is_default else account.is_default}))
    updated.append(candidate)
    if not any(account.is_default for account in updated):
        updated[-1] = Account(**{**candidate.__dict__, "is_default": True})
    save_accounts(updated)
    storage = "Windows 凭据管理器" if sys.platform == "win32" else "macOS 钥匙串"
    print(f"配置成功：{account_name}（{provider.label}）。授权信息已保存在 {storage} 中。")
    return candidate


def configured_client_for(account: Account, secret: str) -> QQMailClient:
    return QQMailClient(account.email, secret, host=account.host, port=account.port, provider_label=account.provider_label)


def aggregate_page(
    accounts: Iterable[Account], *, unread_only: bool, limit: int, offset: int = 0
) -> tuple[MailPage, tuple[AccountMailSummary, ...], tuple[dict[str, str], ...]]:
    """Fetch header-only lists independently so one failed account does not hide others."""
    combined: list[AccountMailSummary] = []
    errors: list[dict[str, str]] = []
    for account in accounts:
        try:
            with configured_client(account) as client:
                combined.extend(
                    AccountMailSummary(account, message)
                    for message in client.list_messages(unread_only=unread_only, limit=None)
                )
        except ViewerError as exc:
            errors.append({"account": account.name, "error": str(exc)})
    combined.sort(key=lambda item: (item.message.date, item.message.uid), reverse=True)
    total = len(combined)
    last_offset = ((total - 1) // limit) * limit if total else 0
    effective_offset = min(max(offset, 0), last_offset)
    page_items = tuple(combined[effective_offset : effective_offset + limit])
    page_data = MailPage(
        tuple(item.message for item in page_items), total, effective_offset, limit
    )
    return page_data, page_items, tuple(errors)


def aggregate_cli(
    accounts: Iterable[Account],
    *,
    unread: bool,
    limit: int | None,
    offset: int,
    since_hours: float | None,
    include_text: bool,
) -> dict[str, object]:
    combined: list[AccountMailSummary] = []
    errors: list[dict[str, str]] = []
    for account in accounts:
        try:
            with configured_client(account) as client:
                messages = client.list_messages(
                    unread_only=unread, limit=None, offset=0, since_hours=since_hours
                )
                for message in messages:
                    combined.append(AccountMailSummary(account, message))
        except ViewerError as exc:
            errors.append({"account": account.name, "error": str(exc)})
    combined.sort(key=lambda item: (item.message.date, item.message.uid), reverse=True)
    selected = combined[offset : offset + limit if limit is not None else None]
    records: list[dict[str, object]] = []
    for item in selected:
        record: dict[str, object] = asdict(item.message)
        record["account"] = item.account.public_record()
        if include_text:
            try:
                with configured_client(item.account) as client:
                    detail = client.get_message(item.message.uid)
                record["preview"] = detail.text[:1200]
                record["attachments"] = list(detail.attachments)
            except ViewerError as exc:
                record["error"] = str(exc)
        records.append(record)
    return {"messages": records, "errors": errors}


BASE_STYLE = """
:root{color-scheme:light dark;--canvas:#fff;--surface:#fff;--surface-raised:#f5f5f5;--ink:#242424;--muted:#6b6b6b;--quiet:#969696;--line:#e6e6e6;--line-strong:#d4d4d4;--accent:#282828;--accent-ink:#fff;--accent-wash:#ededed;--danger:#9b2d30;--danger-wash:#fff2f2;--shadow:0 18px 42px rgba(0,0,0,.06)}
@media(prefers-color-scheme:dark){:root{--canvas:#191919;--surface:#202020;--surface-raised:#292929;--ink:#f2f2f2;--muted:#b5b5b5;--quiet:#858585;--line:#343434;--line-strong:#4a4a4a;--accent:#d9d9d9;--accent-ink:#1b1b1b;--accent-wash:#303030;--danger:#ffb6b6;--danger-wash:#392126;--shadow:0 18px 42px rgba(0,0,0,.24)}}
*{box-sizing:border-box}body{margin:0;background:var(--canvas);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}button,input,select{font:inherit}a{color:inherit}main{max-width:1160px;margin:0 auto;padding:48px 26px 76px}h1,h2,p{margin:0}.app-header{display:flex;align-items:flex-end;justify-content:space-between;gap:28px;padding-bottom:26px;border-bottom:1px solid var(--line)}h1{font-size:clamp(26px,4vw,34px);line-height:1.12;letter-spacing:-.025em}.account{margin-top:8px;color:var(--muted);overflow-wrap:anywhere}.mailbox-state{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}.state-chip{padding:4px 9px;border-radius:999px;background:var(--accent-wash);color:var(--accent);font-size:13px;font-weight:700}.state-text{padding:4px 0;color:var(--muted);font-size:13px}.control-bar{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:18px 0}.filter-group,.page-size-form,.pagination,.jump-form,.account-form{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.segmented{display:flex;padding:3px;border:1px solid var(--line);border-radius:11px;background:var(--surface-raised)}.segment{padding:7px 11px;border-radius:8px;text-decoration:none;color:var(--muted);font-size:14px;font-weight:650}.segment[aria-current="page"]{background:var(--surface);box-shadow:0 2px 7px rgba(30,44,67,.12);color:var(--ink)}.field-label{color:var(--muted);font-size:13px;font-weight:650}.select,.page-input{height:35px;border:1px solid var(--line-strong);border-radius:8px;background:var(--surface);color:var(--ink);padding:0 9px}.page-input{width:52px;text-align:center}.button,.page-link{min-height:35px;display:inline-flex;align-items:center;justify-content:center;padding:0 11px;border:1px solid var(--line-strong);border-radius:8px;background:var(--surface);color:var(--ink);text-decoration:none;cursor:pointer;font-weight:650;font-size:14px}.button.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}.button:hover,.page-link:hover{border-color:var(--accent);color:var(--accent)}.button.primary:hover{filter:brightness(1.06);color:var(--accent-ink)}.page-link.disabled{border-color:var(--line);background:var(--surface-raised);color:var(--quiet);cursor:default}.pagination{justify-content:space-between;padding:15px 0}.page-controls{display:flex;align-items:center;gap:7px;flex-wrap:wrap}.page-status{color:var(--muted);font-size:14px;font-weight:650}.mailbox{border-top:1px solid var(--line);border-bottom:1px solid var(--line);background:var(--surface);box-shadow:var(--shadow)}.list-head,.mail{display:grid;grid-template-columns:minmax(220px,1.12fr) minmax(280px,1.85fr) 148px;gap:24px;align-items:center}.list-head{padding:10px 20px;background:var(--surface-raised);border-bottom:1px solid var(--line);color:var(--muted);font-size:12px;font-weight:750;letter-spacing:.04em}.mail{min-height:72px;padding:13px 20px;border-bottom:1px solid var(--line);text-decoration:none;position:relative;transition:background .16s ease,box-shadow .16s ease}.mail:last-child{border-bottom:0}.mail:hover{background:color-mix(in srgb,var(--accent) 6%,var(--surface))}.mail:focus-visible{outline:3px solid color-mix(in srgb,var(--accent) 55%,transparent);outline-offset:-3px;z-index:1}.sender-name{display:block;color:var(--ink);font-weight:650;overflow-wrap:anywhere}.sender-address,.account-tag{display:block;margin-top:2px;color:var(--muted);font-size:13px;overflow-wrap:anywhere}.account-tag{font-size:12px}.subject{font-weight:700;overflow-wrap:anywhere;line-height:1.4}.date{color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap;text-align:right}.empty-state,.error-state{padding:64px 24px;text-align:center;background:var(--surface)}.empty-state h2,.error-state h2{font-size:20px}.empty-state p,.error-state p{max-width:48ch;margin:8px auto 0;color:var(--muted)}.notice{margin:0 0 14px;padding:10px 13px;border:1px solid var(--line-strong);background:var(--surface-raised);color:var(--muted);font-size:14px}.notice.warning{border-color:var(--line-strong)}.detail-header{display:flex;align-items:flex-start;justify-content:space-between;gap:24px;padding-bottom:22px;border-bottom:1px solid var(--line)}.detail-subject{max-width:800px;font-size:clamp(24px,3.6vw,32px);overflow-wrap:anywhere}.read-only-note{margin-top:8px;color:var(--muted)}.message-shell{max-width:930px;margin-top:28px;background:var(--surface);box-shadow:var(--shadow)}.message-meta{display:grid;grid-template-columns:90px minmax(0,1fr);gap:10px 22px;padding:24px;border-bottom:1px solid var(--line)}.message-meta dt{color:var(--muted);font-weight:650}.message-meta dd{margin:0;overflow-wrap:anywhere}.attachments{margin:20px 24px 0;padding:13px 15px;border:1px solid var(--line);background:var(--surface-raised)}.attachment-label{font-weight:750}.body{padding:28px 24px 34px;white-space:pre-wrap;overflow-wrap:anywhere;font:15px/1.78 ui-monospace,SFMono-Regular,Menlo,monospace}.error-state{max-width:700px;margin:72px auto;box-shadow:var(--shadow)}.error-state h2{color:var(--danger)}.error-state .button{margin-top:20px}
@media(max-width:780px){main{padding:28px 16px 52px}.app-header,.detail-header{display:block}.control-bar{align-items:flex-start;flex-direction:column}.pagination{align-items:flex-start;flex-direction:column}.list-head{display:none}.mail{grid-template-columns:minmax(0,1fr);gap:5px;padding:15px 16px}.subject{grid-column:1;grid-row:1}.mail .sender{grid-column:1;grid-row:2}.date{grid-column:1;grid-row:3;margin-top:2px;text-align:left;white-space:normal;font-size:13px}.sender-name{font-size:14px}.sender-address{max-width:100%;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.message-shell{margin-top:22px}.message-meta{grid-template-columns:1fr;gap:2px;padding:20px}.message-meta dt:not(:first-child){margin-top:12px}.body{padding:24px 20px}.jump-form{width:100%}.page-input{width:64px}}
"""


def page(title: str, content: str) -> bytes:
    document = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>{html.escape(title)}</title><style>{BASE_STYLE}</style></head><body><main>{content}</main></body></html>"""
    return document.encode("utf-8")


class ViewerHandler(BaseHTTPRequestHandler):
    server_version = "QQMailViewer/1.2.0"

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path)
        try:
            if route.path == "/":
                self._home(parse_qs(route.query))
            elif route.path == "/message":
                self._message(parse_qs(route.query))
            elif route.path == "/api/messages":
                self._api_messages(parse_qs(route.query))
            else:
                self._send(
                    page(
                        "未找到",
                        '<section class="error-state"><h2>页面不存在</h2><p>请从邮箱列表重新开始。</p><a class="button primary" href="/">返回邮箱列表</a></section>',
                    ),
                    HTTPStatus.NOT_FOUND,
                )
        except ViewerError as exc:
            retry_path = route.path or "/"
            if route.query:
                retry_path = f"{retry_path}?{route.query}"
            content = f'''<section class="error-state"><h2>暂时无法读取邮箱</h2><p>{html.escape(str(exc))}</p><a class="button primary" href="{html.escape(retry_path, quote=True)}">重新尝试</a></section>'''
            self._send(page("发生错误", content), HTTPStatus.BAD_GATEWAY)

    @staticmethod
    def _page_link(label: str, *, href: str | None) -> str:
        if href is None:
            return f'<span class="page-link disabled" aria-disabled="true">{label}</span>'
        return f'<a class="page-link" href="{html.escape(href, quote=True)}">{label}</a>'

    def _pagination(self, page_data: MailPage, params: ListingParams, selection: str) -> str:
        if not page_data.total:
            return '<nav class="pagination" aria-label="邮件分页"><span class="page-status">没有可分页的邮件</span></nav>'

        current = page_data.current_page
        total_pages = page_data.page_count
        first_url = listing_url(params.unread_only, params.limit, 0, selection)
        previous_url = listing_url(params.unread_only, params.limit, page_data.offset - params.limit, selection) if current > 1 else None
        next_url = listing_url(params.unread_only, params.limit, page_data.offset + params.limit, selection) if current < total_pages else None
        last_url = listing_url(params.unread_only, params.limit, (total_pages - 1) * params.limit, selection)
        return f'''<nav class="pagination" aria-label="邮件分页">
  <span class="page-status">第 {current} / {total_pages} 页</span>
  <div class="page-controls">
    {self._page_link("首页", href=first_url if current > 1 else None)}
    {self._page_link("上一页", href=previous_url)}
    <form class="jump-form" method="get" action="/">
      <input type="hidden" name="unread" value="{"1" if params.unread_only else "0"}">
      <input type="hidden" name="limit" value="{params.limit}">
      <input type="hidden" name="account" value="{html.escape(selection, quote=True)}">
      <label class="field-label" for="page-input-{current}">跳至</label>
      <input class="page-input" id="page-input-{current}" name="page" type="number" min="1" max="{total_pages}" value="{current}" inputmode="numeric">
      <button class="button" type="submit">前往</button>
    </form>
    {self._page_link("下一页", href=next_url)}
    {self._page_link("末页", href=last_url if current < total_pages else None)}
  </div>
</nav>'''

    @staticmethod
    def _message_url(item: MailSummary, params: ListingParams, offset: int, account: str = "", return_account: str = "") -> str:
        values: dict[str, object] = {
            "uid": item.uid,
            "unread": "1" if params.unread_only else "0",
            "limit": params.limit,
            "page": offset // params.limit + 1,
        }
        if account:
            values["account"] = account
        if return_account:
            values["return_account"] = return_account
        query = urlencode(values)
        return f"/message?{query}"

    def _home(self, query: dict[str, list[str]]) -> None:
        params = parse_listing_params(query)
        accounts = load_accounts()
        if not accounts:
            raise ViewerError("尚未配置邮箱。请先运行 configure。")
        requested = query.get("account", [None])[0]
        selection = requested or ("all" if len(accounts) >= 2 else default_account(accounts).name)
        if selection == "all":
            page_data, aggregate_items, errors = aggregate_page(
                accounts, unread_only=params.unread_only, limit=params.limit, offset=params.offset
            )
            items = tuple((entry.message, entry.account) for entry in aggregate_items)
            address = "全部账户 · " + "、".join(account.email for account in accounts)
        else:
            selected_account = find_account(selection, accounts)
            with configured_client(selected_account) as client:
                page_data = client.list_page(
                    unread_only=params.unread_only, limit=params.limit, offset=params.offset
                )
            items = tuple((item, selected_account) for item in page_data.messages)
            errors = ()
            address = f"{selected_account.email} · {selected_account.provider_label}"
        rows = []
        for item, item_account in items:
            sender_name, sender_address = sender_parts(item.sender)
            account_tag = (
                f'<span class="account-tag">{html.escape(item_account.name)} · {html.escape(item_account.email)}</span>'
                if selection == "all"
                else ""
            )
            rows.append(
                f'''<a class="mail" href="{html.escape(self._message_url(item, params, page_data.offset, item_account.name, selection), quote=True)}">
  <span class="sender"><span class="sender-name">{html.escape(sender_name)}</span>{f'<span class="sender-address">{html.escape(sender_address)}</span>' if sender_address else ''}{account_tag}</span>
  <span class="subject">{html.escape(item.subject)}</span><time class="date">{html.escape(item.date)}</time>
</a>'''
            )
        if page_data.total:
            range_text = f"显示第 {page_data.offset + 1}–{page_data.offset + len(page_data.messages)} 封"
            page_text = f"第 {page_data.current_page} / {page_data.page_count} 页"
        else:
            range_text = "没有符合当前筛选的邮件"
            page_text = "没有可分页的邮件"
        notice = ""
        if params.invalid_page:
            notice = '<p class="notice" role="status">页码必须是大于 0 的整数，已显示可用页面。</p>'
        elif page_data.total and page_data.offset != params.offset:
            notice = f'<p class="notice" role="status">第 {params.requested_page} 页不存在，已显示最后一页。</p>'
        warnings = "".join(
            f'<p class="notice warning" role="status">账户 {html.escape(error["account"])} 暂时无法读取：{html.escape(error["error"])}</p>'
            for error in errors
        )
        unread_url = listing_url(True, params.limit, 0, selection)
        all_url = listing_url(False, params.limit, 0, selection)
        selected_mode = "未读邮件" if params.unread_only else "全部邮件"
        listing = "".join(rows) if rows else '<div class="empty-state"><h2>这里没有符合条件的邮件</h2><p>你可以切换到全部邮件，或稍后刷新再试。</p></div>'
        account_options = "".join(
            f'<option value="{html.escape(account.name, quote=True)}"{" selected" if selection == account.name else ""}>{html.escape(account.name)} · {html.escape(account.email)}</option>'
            for account in accounts
        )
        if len(accounts) >= 2:
            account_options = f'<option value="all"{" selected" if selection == "all" else ""}>全部账户</option>' + account_options
        content = f'''<header class="app-header">
  <div>
    <h1>本地只读邮箱查看器</h1>
    <p class="account">{html.escape(address)} · 按日期倒序</p>
    <div class="mailbox-state"><span class="state-chip">{selected_mode}</span><span class="state-text">共 {page_data.total} 封 · {range_text} · {page_text}</span></div>
  </div>
  <a class="button primary" href="{html.escape(listing_url(params.unread_only, params.limit, page_data.offset, selection), quote=True)}">刷新列表</a>
</header>
<div class="control-bar">
  <div class="filter-group">
    <nav class="segmented" aria-label="邮件筛选">
      <a class="segment" href="{html.escape(unread_url, quote=True)}"{ ' aria-current="page"' if params.unread_only else ''}>仅看未读</a>
      <a class="segment" href="{html.escape(all_url, quote=True)}"{ ' aria-current="page"' if not params.unread_only else ''}>查看全部</a>
    </nav>
    <form class="account-form" method="get" action="/">
      <input type="hidden" name="unread" value="{"1" if params.unread_only else "0"}">
      <input type="hidden" name="limit" value="{params.limit}">
      <input type="hidden" name="page" value="1">
      <label class="field-label" for="account-select">账户</label>
      <select class="select" id="account-select" name="account">{account_options}</select>
      <button class="button" type="submit">切换</button>
    </form>
    <form class="page-size-form" method="get" action="/">
      <input type="hidden" name="unread" value="{"1" if params.unread_only else "0"}">
      <input type="hidden" name="page" value="1">
      <input type="hidden" name="account" value="{html.escape(selection, quote=True)}">
      <label class="field-label" for="page-size">每页</label>
      <select class="select" id="page-size" name="limit">{''.join(f'<option value="{value}"{" selected" if params.limit == value else ""}>{value} 封</option>' for value in (30, 50, 100))}</select>
      <button class="button" type="submit">应用</button>
    </form>
  </div>
</div>
{notice}
{warnings}
{self._pagination(page_data, params, selection)}
<section class="mailbox" aria-label="{selected_mode}列表">
  <div class="list-head"><span>发件人</span><span>主题</span><span>日期</span></div>
  {listing}
</section>
{self._pagination(page_data, params, selection)}'''
        self._send(page("本地只读邮箱查看器", content))

    def _message(self, query: dict[str, list[str]]) -> None:
        uid = query.get("uid", [""])[0]
        params = parse_listing_params(query)
        account_name = query.get("account", [""])[0]
        account = find_account(account_name)
        return_account = query.get("return_account", [account.name])[0]
        if return_account != "all":
            find_account(return_account)
        with configured_client(account) as client:
            item = client.get_message(uid)
        attachment_box = ""
        if item.attachments:
            names = "、".join(html.escape(name) for name in item.attachments)
            attachment_box = f'<aside class="attachments"><span class="attachment-label">附件</span>（仅列出，不下载）：{names}</aside>'
        back_url = listing_url(params.unread_only, params.limit, params.offset, return_account)
        content = f'''<header class="detail-header">
  <div><h1 class="detail-subject">{html.escape(item.subject)}</h1><p class="read-only-note">{html.escape(account.name)} · {html.escape(account.email)} · 只读查看，不会标为已读</p></div>
  <a class="button" href="{html.escape(back_url, quote=True)}">返回邮件列表</a>
</header>
<article class="message-shell">
  <dl class="message-meta"><dt>发件人</dt><dd>{html.escape(item.sender)}</dd><dt>收件人</dt><dd>{html.escape(item.recipients)}</dd><dt>时间</dt><dd>{html.escape(item.date)}</dd></dl>
  {attachment_box}
  <div class="body">{html.escape(item.text) or '（邮件没有可显示的文本正文）'}</div>
</article>'''
        self._send(page(item.subject, content))

    def _api_messages(self, query: dict[str, list[str]]) -> None:
        params = parse_listing_params(query)
        selection = query.get("account", [None])[0]
        if selection == "all":
            payload_value: object = aggregate_cli(
                load_accounts(),
                unread=params.unread_only,
                limit=params.limit,
                offset=params.offset,
                since_hours=None,
                include_text=False,
            )
        else:
            with configured_client(find_account(selection)) as client:
                messages = client.list_messages(
                    unread_only=params.unread_only, limit=params.limit, offset=params.offset
                )
            # Existing single-account API remains an array with the same fields.
            payload_value = [asdict(item) for item in messages]
        payload = json.dumps(payload_value, ensure_ascii=False, indent=2).encode("utf-8")
        self._send(payload, content_type="application/json; charset=utf-8")

    def _send(self, body: bytes, status: HTTPStatus = HTTPStatus.OK, *, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        sys.stderr.write("[viewer] " + (format % args) + "\n")


def list_cli(
    unread: bool,
    limit: int | None,
    include_text: bool,
    since_hours: float | None,
    offset: int,
    account_name: str | None = None,
    all_accounts: bool = False,
) -> None:
    if all_accounts:
        print(
            json.dumps(
                aggregate_cli(
                    load_accounts(),
                    unread=unread,
                    limit=limit,
                    offset=offset,
                    since_hours=since_hours,
                    include_text=include_text,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    with configured_client(find_account(account_name)) as client:
        summaries = client.list_messages(
            unread_only=unread, limit=limit, offset=offset, since_hours=since_hours
        )
        if include_text:
            result = []
            for summary in summaries:
                detail = client.get_message(summary.uid)
                record = asdict(summary)
                record["preview"] = detail.text[:1200]
                record["attachments"] = list(detail.attachments)
                result.append(record)
        else:
            result = [asdict(item) for item in summaries]
    print(json.dumps(result, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="本地只读 IMAP 邮箱查看器")
    sub = parser.add_subparsers(dest="command", required=True)

    configure_parser = sub.add_parser("configure", help="测试连接并把授权信息保存到系统凭据库")
    configure_parser.add_argument("--email", required=True, help="完整邮箱地址")
    configure_parser.add_argument("--provider", default="auto", choices=("auto", *PROVIDERS), help="服务商；默认按邮箱自动识别")
    configure_parser.add_argument("--name", help="账户名称，用于 list、show 和网页切换")
    configure_parser.add_argument("--imap-host", help="仅 custom：加密 IMAPS 主机名")
    configure_parser.add_argument("--port", type=int, help="仅 custom：IMAPS 端口，默认 993")
    configure_parser.add_argument("--default", action="store_true", help="将此账户设为默认账户")

    sub.add_parser("accounts", help="列出已配置账户（不显示密码或授权码）")

    list_parser = sub.add_parser("list", help="以 JSON 列出邮件，供人工或定时任务读取")
    mode = list_parser.add_mutually_exclusive_group()
    mode.add_argument("--unread", action="store_true", default=True, help="仅列出未读邮件（默认）")
    mode.add_argument("--all", action="store_true", help="列出最近的全部邮件")
    list_parser.add_argument("--limit", type=int, default=20, help="单页最多返回多少封，不设上限")
    list_parser.add_argument("--offset", type=int, default=0, help="跳过前 N 封，配合 --limit 分页")
    list_parser.add_argument("--all-pages", action="store_true", help="返回所有符合条件的邮件，不分页、不设上限")
    list_parser.add_argument("--since-hours", type=float, help="仅返回最近 N 小时内收到的邮件")
    list_parser.add_argument("--include-text", action="store_true", help="附带每封邮件前 1200 字正文预览")
    account_mode = list_parser.add_mutually_exclusive_group()
    account_mode.add_argument("--account", help="读取指定账户名称")
    account_mode.add_argument("--all-accounts", action="store_true", help="汇总读取全部账户，失败账户会单独报告")

    show_parser = sub.add_parser("show", help="以 JSON 读取一封邮件的完整文本正文")
    show_parser.add_argument("uid", help="邮件 UID，可从 list 输出获得")
    show_parser.add_argument("--account", help="邮件所属账户名称；默认账户可省略")

    serve_parser = sub.add_parser("serve", help="启动仅限本机访问的网页查看器")
    serve_parser.add_argument("--port", type=int, default=8765, help="本机端口（默认 8765）")
    return parser


def main() -> int:
    _configure_standard_streams()
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "configure":
            configure(
                args.email,
                provider_id=args.provider,
                name=args.name,
                host=args.imap_host,
                port=args.port,
                make_default=args.default,
            )
        elif args.command == "accounts":
            accounts = load_accounts()
            if not accounts:
                raise ViewerError("尚未配置邮箱。请先运行 configure。")
            print(json.dumps([account.public_record() for account in accounts], ensure_ascii=False, indent=2))
        elif args.command == "list":
            if args.limit <= 0:
                raise ViewerError("--limit 必须大于 0。")
            if args.offset < 0:
                raise ViewerError("--offset 不能小于 0。")
            if args.since_hours is not None and args.since_hours <= 0:
                raise ViewerError("--since-hours 必须大于 0。")
            limit = None if args.all_pages else args.limit
            list_cli(
                not args.all,
                limit,
                args.include_text,
                args.since_hours,
                args.offset,
                args.account,
                args.all_accounts,
            )
        elif args.command == "show":
            with configured_client(find_account(args.account)) as client:
                print(json.dumps(asdict(client.get_message(args.uid)), ensure_ascii=False, indent=2))
        elif args.command == "serve":
            if not 1024 <= args.port <= 65535:
                raise ViewerError("端口必须在 1024–65535 之间。")
            if not load_accounts():
                raise ViewerError("尚未配置邮箱。请先运行 configure。")
            server = ThreadingHTTPServer(("127.0.0.1", args.port), ViewerHandler)
            print(f"本地只读邮箱查看器已启动：http://127.0.0.1:{args.port}")
            print("按 Control-C 停止。")
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print("\n已停止。")
            finally:
                server.server_close()
        return 0
    except ViewerError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
