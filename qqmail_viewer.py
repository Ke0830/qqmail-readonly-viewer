#!/usr/bin/env python3
"""Local, read-only IMAP mail viewer.

Credentials and the body-cache key are stored in the macOS login keychain or
Windows Credential Manager. Lists are served from a local cache; IMAPS and
selective BODY.PEEK sections keep synchronization and viewing read-only.
"""

from __future__ import annotations

import argparse
import base64
import email
import getpass
import html
import imaplib
import json
import os
import re
import secrets
import ssl
import subprocess
import sys
import threading
import time
import hashlib
from dataclasses import asdict, dataclass
from email.header import Header, decode_header
from email.message import Message
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from mail_cache import (
    CACHE_MODES,
    CacheSettings,
    CacheStore,
    CachedMessage,
    CachedTranslation,
    default_cache_path,
)
from mail_html import (
    HTML_POLICY_VERSION,
    HtmlImageReference,
    MAX_INPUT_BYTES,
    MAX_OUTPUT_BYTES,
    HtmlSanitizationError,
    normalize_plain_text,
    materialize_image_placeholders,
    sanitize_email_html,
)
from mail_images import (
    MAX_IMAGE_BYTES,
    ImageValidationError,
    RemoteImageFetcher,
    validate_image,
)
from mail_mime import (
    BodyStructureError,
    decode_transfer,
    extract_fetch_payload,
    parse_bodystructure,
    select_body_plan,
)
from mail_sync import SyncManager, cached_web_body_is_current
from mail_translation import (
    TRANSLATION_TARGET,
    TranslationConfig,
    TranslationError,
    translate_mail_content,
    translation_source_digest,
    validate_api_key,
)


# Kept as public compatibility constants for integrations that imported them.
IMAP_HOST = "imap.qq.com"
IMAP_PORT = 993
KEYCHAIN_ACCOUNT = "qqmail-viewer"
EMAIL_SERVICE = "codex.qqmail-viewer.email"
AUTH_SERVICE = "codex.qqmail-viewer.authorization-code"
ACCOUNT_INDEX_SERVICE = "codex.qqmail-viewer.account-index.v2"
SETTINGS_SERVICE = "codex.qqmail-viewer.settings.v1"
CACHE_KEY_SERVICE = "codex.qqmail-viewer.cache-encryption-key.v1"
TRANSLATION_CONFIG_SERVICE = "codex.qqmail-viewer.translation-config.v1"
TRANSLATION_KEY_SERVICE = "codex.qqmail-viewer.translation-api-key.v1"
PROCESS_CSRF_TOKEN = secrets.token_urlsafe(32)
DEFAULT_LIMIT = 30
MAX_LIMIT = 100
MAX_MESSAGE_IMAGE_COUNT = 30
MAX_MESSAGE_IMAGE_BYTES = 30 * 1024 * 1024
APP_TITLE = "本地邮箱查看器"
CHINA_TIMEZONE = timezone(timedelta(hours=8))
NETEASE_IMAP_HOSTS = frozenset({"imap.163.com", "imap.126.com", "imap.yeah.net"})
PROVIDER_TAB_LABELS = {
    "qq": "QQ",
    "163": "163",
    "126": "126",
    "yeah": "yeah",
    "icloud": "iCloud",
    "gmail": "Gmail",
}
TRANSLATION_PROVIDER_LABELS = {
    "deepl_free": "DeepL Free",
    "deepl_pro": "DeepL Pro",
    "openai_compatible": "OpenAI 兼容接口",
}


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
    unread: bool | None = None


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
        return normalize_plain_text("".join(self.parts))


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
    safe_html: str = ""
    body_format: str = "plain"
    blocked_images: int = 0
    html_policy: str = ""
    image_resources: tuple[dict[str, object], ...] = ()
    cacheable: bool = True


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
    if address and "@" in address:
        return (name, address)
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


def _windows_credential_delete(service: str) -> None:
    keyring = _windows_keyring()
    try:
        if keyring.get_password(service, KEYCHAIN_ACCOUNT) is None:
            return
        keyring.delete_password(service, KEYCHAIN_ACCOUNT)
    except Exception as exc:
        raise ViewerError(f"无法从 Windows 凭据管理器删除凭据：{exc}") from exc


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


def keychain_delete(service: str) -> None:
    _ensure_supported_platform()
    if sys.platform == "win32":
        _windows_credential_delete(service)
        return
    if _keychain_get_optional(service) is None:
        return
    _security(
        [
            "delete-generic-password",
            "-a",
            KEYCHAIN_ACCOUNT,
            "-s",
            service,
        ]
    )


def _keychain_get_optional(service: str) -> str | None:
    """Return a credential when present, without exposing a missing-item error."""
    try:
        return keychain_get(service)
    except ViewerError:
        return None


def load_settings() -> CacheSettings:
    raw = _keychain_get_optional(SETTINGS_SERVICE)
    if raw is None:
        return CacheSettings()
    try:
        value = json.loads(raw)
        settings = CacheSettings(
            cache_mode=str(value["cache_mode"]),
            refresh_minutes=int(value["refresh_minutes"]),
        )
        return settings.validated()
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ViewerError("缓存设置无法读取。请使用 settings 命令重新保存设置。") from exc


def save_settings(settings: CacheSettings) -> None:
    try:
        validated = settings.validated()
    except ValueError as exc:
        raise ViewerError(str(exc)) from exc
    keychain_set(
        SETTINGS_SERVICE,
        json.dumps(validated.public_record(), ensure_ascii=False, separators=(",", ":")),
    )


def load_translation_config() -> TranslationConfig | None:
    raw = _keychain_get_optional(TRANSLATION_CONFIG_SERVICE)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise TypeError
        return TranslationConfig(
            provider=str(value["provider"]),
            base_url=str(value.get("base_url", "")),
            model=str(value.get("model", "")),
        ).validated()
    except (json.JSONDecodeError, KeyError, TypeError, TranslationError) as exc:
        raise ViewerError("翻译服务配置无法读取，请在设置页重新绑定。") from exc


def save_translation_config(config: TranslationConfig, api_key: str) -> None:
    try:
        validated = config.validated()
        validated_key = validate_api_key(validated, api_key)
    except TranslationError as exc:
        raise ViewerError(str(exc)) from exc
    if validated_key:
        keychain_set(TRANSLATION_KEY_SERVICE, validated_key)
    else:
        keychain_delete(TRANSLATION_KEY_SERVICE)
    keychain_set(
        TRANSLATION_CONFIG_SERVICE,
        json.dumps(
            validated.public_record(), ensure_ascii=False, separators=(",", ":")
        ),
    )


def delete_translation_config() -> None:
    keychain_delete(TRANSLATION_CONFIG_SERVICE)
    keychain_delete(TRANSLATION_KEY_SERVICE)


def _cache_encryption_key() -> tuple[bytes, bool]:
    raw = _keychain_get_optional(CACHE_KEY_SERVICE)
    if raw is not None:
        try:
            key = base64.b64decode(raw, validate=True)
            if len(key) == 32:
                return key, False
        except (ValueError, TypeError):
            pass
    key = secrets.token_bytes(32)
    keychain_set(CACHE_KEY_SERVICE, base64.b64encode(key).decode("ascii"))
    return key, True


def cache_path() -> Path:
    return default_cache_path(
        sys.platform,
        Path.home(),
        os.environ.get("LOCALAPPDATA"),
    )


def _is_cache_mode_downgrade(current: str, requested: str) -> bool:
    rank = {"memory": 0, "metadata": 1, "body": 2}
    return rank[requested] < rank[current]


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
        self._uidvalidity = ""

    def __enter__(self) -> "QQMailClient":
        return self.connect()

    def connect(self) -> "QQMailClient":
        """Open the account once; workers keep this connection for later jobs."""
        if self.connection is not None:
            return self
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
            self._capture_uidvalidity()
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
                "ID", '("name" "qqmail-readonly-viewer" "version" "1.3.0")'
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

    def noop(self) -> None:
        status, response = self._imap().noop()
        if status != "OK":
            reason = _imap_response_text(response)
            raise ViewerError(f"邮箱连接已断开。{reason}" if reason else "邮箱连接已断开。")

    def _capture_uidvalidity(self) -> None:
        connection = self._imap()
        value: object = None
        try:
            response = connection.response("UIDVALIDITY")
            if isinstance(response, tuple) and len(response) > 1:
                values = response[1]
                if isinstance(values, (list, tuple)) and values:
                    value = values[-1]
        except (AttributeError, imaplib.IMAP4.error):
            value = None
        if value is None:
            responses = getattr(connection, "untagged_responses", {})
            values = responses.get("UIDVALIDITY") if isinstance(responses, dict) else None
            if isinstance(values, (list, tuple)) and values:
                value = values[-1]
        if isinstance(value, bytes):
            value = value.decode("ascii", errors="ignore")
        if isinstance(value, str):
            match = re.search(r"\d+", value)
            self._uidvalidity = match.group(0) if match else ""

    def uidvalidity(self) -> str:
        return self._uidvalidity

    def _imap(self) -> imaplib.IMAP4_SSL:
        if self.connection is None:
            raise ViewerError("邮箱连接尚未建立。")
        return self.connection

    def search_uids(self, unread_only: bool) -> list[str]:
        criteria = "UNSEEN" if unread_only else "ALL"
        status, data = self._imap().uid("search", None, criteria)
        if status != "OK" or not data:
            raise ViewerError("无法搜索收件箱。")
        payload = data[0] if isinstance(data[0], bytes) else b""
        return [item.decode("ascii") for item in payload.split() if item.isdigit()]

    def priority_uids(self, *, unread_only: bool, limit: int) -> list[str]:
        """Return newest candidates using server SORT, then a highest-UID fallback."""
        criteria = "UNSEEN" if unread_only else "ALL"
        try:
            status, data = self._imap().uid(
                "sort", "(REVERSE ARRIVAL)", "UTF-8", criteria
            )
            if status == "OK" and data:
                payload = data[0] if isinstance(data[0], bytes) else b""
                sorted_uids = [
                    item.decode("ascii") for item in payload.split() if item.isdigit()
                ]
                return sorted_uids[:limit]
        except (imaplib.IMAP4.error, OSError):
            pass
        uids = self.search_uids(unread_only)
        uids.sort(key=int, reverse=True)
        return uids[:limit]

    def fetch_summaries(
        self,
        uids: Iterable[str],
        unread_uids: set[str] | None = None,
    ) -> list[dict[str, object]]:
        """Fetch bounded metadata and MIME structure without any message body payload."""
        requested = [str(uid) for uid in uids if str(uid).isdigit()]
        summaries: list[dict[str, object]] = []
        for start in range(0, len(requested), 200):
            uid_set = ",".join(requested[start : start + 200])
            if not uid_set:
                continue
            status, rows = self._imap().uid(
                "fetch",
                uid_set,
                "(UID FLAGS INTERNALDATE RFC822.SIZE BODYSTRUCTURE "
                "BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO DATE)])",
            )
            if status != "OK":
                raise ViewerError("无法读取邮件头。")
            for row in rows:
                if not isinstance(row, tuple) or len(row) < 2:
                    continue
                metadata = row[0] if isinstance(row[0], bytes) else b""
                header_bytes = row[1] if isinstance(row[1], bytes) else b""
                uid_match = re.search(rb"\bUID\s+(\d+)", metadata, re.IGNORECASE)
                if not uid_match:
                    continue
                uid = uid_match.group(1).decode("ascii")
                parsed = email.message_from_bytes(header_bytes)
                raw_date = parsed.get("Date")
                received_at = _internaldate_timestamp(metadata)
                if received_at is None:
                    date_value = parse_date(raw_date)
                    received_at = date_value.timestamp() if date_value is not None else float(uid)
                display_date = normalize_date(raw_date)
                if not display_date:
                    display_date = datetime.fromtimestamp(
                        received_at, CHINA_TIMEZONE
                    ).strftime("%Y-%m-%d %H:%M")
                size_match = re.search(
                    rb"RFC822\.SIZE\s+(\d+)", metadata, re.IGNORECASE
                )
                flags_match = re.search(
                    rb"FLAGS\s*\(([^)]*)\)", metadata, re.IGNORECASE
                )
                is_unread = (
                    uid in unread_uids
                    if unread_uids is not None
                    else not flags_match
                    or b"\\seen" not in flags_match.group(1).lower()
                )
                attachments: tuple[str, ...] = ()
                try:
                    plan = select_body_plan(
                        parse_bodystructure([row]), prefer_html=False
                    )
                    attachments = tuple(
                        decode_mime(name) or "未命名附件"
                        for name in plan.attachments
                    )
                except BodyStructureError:
                    pass
                summaries.append(
                    {
                        "uid": uid,
                        "subject": decode_mime(parsed.get("Subject")) or "（无主题）",
                        "sender": decode_mime(parsed.get("From")),
                        "recipients": decode_mime(parsed.get("To")),
                        "date": display_date,
                        "received_at": received_at,
                        "size": int(size_match.group(1)) if size_match else 0,
                        "unread": is_unread,
                        "attachments": attachments,
                    }
                )
        return summaries

    def _matching_messages(
        self,
        *,
        unread_only: bool,
        since_hours: float | None = None,
    ) -> list[MailSummary]:
        uids = self.search_uids(unread_only)
        dated_messages: list[tuple[float, int, MailSummary]] = []
        # UID order is not date order after mailbox migration or re-indexing.
        # Fetch headers in bounded batches, then sort by each message's Date.
        for item in self.fetch_summaries(uids):
            uid = str(item["uid"])
            summary = MailSummary(
                uid=uid,
                subject=str(item["subject"]),
                sender=str(item["sender"]),
                date=str(item["date"]),
                size=int(item["size"]),
            )
            dated_messages.append((float(item["received_at"]), int(uid), summary))
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

    def get_message(self, uid: str, *, prefer_html: bool = False) -> MailDetail:
        if not uid.isdigit():
            raise ViewerError("邮件 UID 无效。")
        status, header_rows = self._imap().uid(
            "fetch", uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO DATE)])"
        )
        if status != "OK":
            raise ViewerError("无法读取这封邮件。")
        header_bytes = extract_fetch_payload(header_rows)
        if not header_bytes:
            raise ViewerError("邮件不存在或已被移动。")
        parsed = email.message_from_bytes(header_bytes)
        text = "为避免下载附件，正文无法安全读取"
        attachments: tuple[str, ...] = ()
        safe_html = ""
        body_format = "unavailable"
        blocked_images = 0
        html_policy = ""
        image_resources: tuple[dict[str, object], ...] = ()
        cacheable = False
        try:
            structure_status, structure_rows = self._imap().uid(
                "fetch", uid, "(BODYSTRUCTURE)"
            )
            if structure_status != "OK":
                raise ViewerError("无法读取邮件结构。")
            plan = select_body_plan(
                parse_bodystructure(structure_rows), prefer_html=prefer_html
            )
            attachments = tuple(
                decode_mime(name) or "未命名附件" for name in plan.attachments
            )
            transport_error: ViewerError | None = None
            html_transport_failed = False
            empty_candidate_seen = False
            empty_blocked_images = 0
            unsafe_candidate_failed = False
            for candidate in (plan.primary, plan.fallback):
                if not candidate:
                    continue
                html_selected = all(
                    part.content_type == "text/html" for part in candidate
                )
                try:
                    decoded_parts: list[str] = []
                    for part in candidate:
                        if part.octets is not None and part.octets > MAX_INPUT_BYTES:
                            raise HtmlSanitizationError("邮件正文超过安全读取上限")
                        payload_status, payload_rows = self._imap().uid(
                            "fetch", uid, f"(BODY.PEEK[{part.section}])"
                        )
                        if payload_status != "OK":
                            raise ViewerError("无法读取邮件正文。")
                        raw_payload = extract_fetch_payload(payload_rows)
                        if part.octets and not raw_payload:
                            raise ViewerError("无法读取邮件正文。")
                        if len(raw_payload) > MAX_INPUT_BYTES:
                            raise HtmlSanitizationError("邮件正文超过安全读取上限")
                        payload = decode_transfer(raw_payload, part.encoding)
                        if len(payload) > MAX_INPUT_BYTES:
                            raise HtmlSanitizationError("邮件正文超过安全读取上限")
                        decoded = decode_bytes(payload, part.charset)
                        if len(decoded.encode("utf-8")) > MAX_INPUT_BYTES:
                            raise HtmlSanitizationError("邮件正文超过安全读取上限")
                        decoded_parts.append(decoded)

                    if html_selected:
                        selected_base = candidate[0].content_location if candidate else ""
                        sanitized = sanitize_email_html(
                            "\n".join(decoded_parts),
                            content_location=selected_base,
                        )
                        candidate_text = sanitized.plain_text
                        candidate_html = sanitized.safe_html.strip()
                        image_resources = _map_image_resources(
                            sanitized.images,
                            plan.inline_resources,
                            selected_base,
                        )
                        candidate_blocked_images = sanitized.blocked_images
                        if not candidate_html or (
                            not candidate_text
                            and "mail-image-placeholder" not in candidate_html
                        ):
                            empty_candidate_seen = True
                            empty_blocked_images = max(
                                empty_blocked_images,
                                candidate_blocked_images,
                            )
                            continue
                        text = candidate_text
                        safe_html = candidate_html
                        body_format = "html"
                        blocked_images = candidate_blocked_images
                        html_policy = HTML_POLICY_VERSION
                    elif all(
                        part.content_type == "text/plain" for part in candidate
                    ):
                        candidate_text = normalize_plain_text(
                            "\n\n".join(decoded_parts)
                        )
                        if len(candidate_text.encode("utf-8")) > MAX_OUTPUT_BYTES:
                            raise HtmlSanitizationError(
                                "纯文本正文超过安全显示上限"
                            )
                        if not candidate_text:
                            empty_candidate_seen = True
                            continue
                        text = candidate_text
                        body_format = "plain"
                        if prefer_html and not html_transport_failed:
                            html_policy = HTML_POLICY_VERSION
                    else:
                        raise BodyStructureError("mixed safe body candidate")
                    cacheable = True
                    break
                except ViewerError as exc:
                    transport_error = exc
                    if html_selected:
                        html_transport_failed = True
                except (HtmlSanitizationError, BodyStructureError, ValueError, TypeError):
                    unsafe_candidate_failed = True
                    continue

            if not cacheable:
                if transport_error is not None:
                    raise transport_error
                if empty_candidate_seen and not unsafe_candidate_failed:
                    text = ""
                    body_format = "plain"
                    blocked_images = empty_blocked_images
                    html_policy = HTML_POLICY_VERSION if prefer_html else ""
                    image_resources = ()
                    cacheable = True
                elif plan.blocked_reason == "encrypted":
                    text = "这封邮件的正文已加密，当前无法安全读取"
        except ViewerError:
            raise
        except (BodyStructureError, ValueError, TypeError):
            pass
        return MailDetail(
            uid=uid,
            subject=decode_mime(parsed.get("Subject")) or "（无主题）",
            sender=decode_mime(parsed.get("From")),
            recipients=decode_mime(parsed.get("To")),
            date=normalize_date(parsed.get("Date")),
            text=text,
            attachments=attachments,
            safe_html=safe_html,
            body_format=body_format,
            blocked_images=blocked_images,
            html_policy=html_policy,
            image_resources=image_resources,
            cacheable=cacheable,
        )

    def fetch_inline_image(self, uid: str, resource: object):
        """Read one approved inline image section through the account worker."""

        if not isinstance(resource, dict):
            raise ViewerError("图片资源无效。")
        section = str(resource.get("section", ""))
        content_type = str(resource.get("content_type", ""))
        encoding = str(resource.get("encoding", ""))
        octets = resource.get("octets")
        if not section or not re.fullmatch(r"[0-9.]+", section):
            raise ViewerError("图片 MIME section 无效。")
        if not content_type.startswith("image/"):
            raise ViewerError("该 MIME 资源不是图片。")
        if isinstance(octets, int) and octets > MAX_IMAGE_BYTES * 2:
            raise ViewerError("图片超过安全读取上限。")
        fetch_section = f"BODY.PEEK[{section}]"
        if octets is None:
            fetch_section = f"BODY.PEEK[{section}]<0.{MAX_IMAGE_BYTES * 2 + 1}>"
        status, rows = self._imap().uid("fetch", uid, f"({fetch_section})")
        if status != "OK":
            raise ViewerError("无法读取内嵌图片。")
        raw_payload = extract_fetch_payload(rows)
        if len(raw_payload) > MAX_IMAGE_BYTES * 2:
            raise ViewerError("图片超过安全读取上限。")
        payload = decode_transfer(raw_payload, encoding)
        try:
            return validate_image(payload, content_type)
        except ImageValidationError as exc:
            raise ViewerError("内嵌图片无法安全验证。") from exc


def _map_image_resources(
    references: Iterable[HtmlImageReference],
    inline_parts: Iterable[object],
    content_location: str = "",
) -> tuple[dict[str, object], ...]:
    parts = tuple(inline_parts)
    resources: list[dict[str, object]] = []
    for reference in references:
        source_type = reference.source_type
        source = reference.source
        matched = None
        if source_type == "cid":
            wanted = source.strip().strip("<>").casefold()
            matched = next(
                (
                    part
                    for part in parts
                    if str(getattr(part, "content_type", "")).startswith("image/")
                    and str(getattr(part, "content_id", "")).strip().strip("<>").casefold()
                    == wanted
                ),
                None,
            )
            if matched is None:
                continue
        elif source_type in {"location", "remote"}:
            matched = next(
                (
                    part
                    for part in parts
                    if str(getattr(part, "content_type", "")).startswith("image/")
                    and str(getattr(part, "content_location", "")).strip() == source
                ),
                None,
            )
            if matched is not None:
                # A Content-Location reference is an inline MIME resource,
                # even when its identifier looks like an HTTP URL.
                source_type = "cid"
            elif source_type == "location":
                if not content_location or not re.match(r"https?://", content_location, re.I):
                    continue
                source_type = "remote"
                source = urljoin(content_location, source)
        resource: dict[str, object] = {
            "id": reference.resource_id,
            "source_type": source_type,
            "source": source,
            "descriptor": reference.descriptor,
            "section": str(getattr(matched, "section", "")) if matched else "",
            "content_type": str(getattr(matched, "content_type", "")) if matched else "",
            "encoding": str(getattr(matched, "encoding", "")) if matched else "",
            "octets": getattr(matched, "octets", None) if matched else None,
        }
        if len(resources) < MAX_MESSAGE_IMAGE_COUNT:
            resources.append(resource)
    return tuple(resources)


def _internaldate_timestamp(metadata: bytes) -> float | None:
    match = re.search(
        rb'INTERNALDATE\s+"([^"]+)"', metadata, re.IGNORECASE
    )
    if not match:
        return None
    value = match.group(1).decode("ascii", errors="ignore")
    try:
        return datetime.strptime(value, "%d-%b-%Y %H:%M:%S %z").timestamp()
    except ValueError:
        return None


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


class _ImageTokenStore:
    def __init__(self, ttl_seconds: float = 3600.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._items: dict[str, tuple[float, str, str, dict[str, object]]] = {}
        self._lock = threading.RLock()

    def register(
        self, account_name: str, uid: str, resource: dict[str, object]
    ) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._purge_locked()
            self._items[token] = (
                time.time() + self.ttl_seconds,
                account_name,
                str(uid),
                dict(resource),
            )
        return token

    def resolve(self, token: str) -> tuple[str, str, dict[str, object]] | None:
        with self._lock:
            self._purge_locked()
            item = self._items.get(token)
            if item is None:
                return None
            _expires, account_name, uid, resource = item
            return account_name, uid, dict(resource)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def _purge_locked(self) -> None:
        now = time.time()
        expired = [token for token, item in self._items.items() if item[0] <= now]
        for token in expired:
            self._items.pop(token, None)


class ViewerRuntime:
    """Shared cache and per-account workers for one CLI command or web server."""

    def __init__(
        self,
        *,
        periodic: bool,
        accounts: Iterable[Account] | None = None,
        settings: CacheSettings | None = None,
        cache_file: Path | None = None,
        encryption_key: bytes | None = None,
    ) -> None:
        self.periodic = periodic
        self.accounts = tuple(load_accounts() if accounts is None else accounts)
        if not self.accounts:
            raise ViewerError("尚未配置邮箱。请先运行 configure。")
        self.settings = load_settings() if settings is None else settings.validated()
        self.cache_file = cache_path() if cache_file is None else cache_file
        if encryption_key is None:
            self.encryption_key, key_created = _cache_encryption_key()
        else:
            self.encryption_key, key_created = encryption_key, False
        self._lock = threading.RLock()
        self._translation_config: TranslationConfig | None = None
        self._translation_config_loaded = False
        self._translation_locks: dict[tuple[str, str], threading.Lock] = {}
        self.cache = CacheStore(self.cache_file, self.settings, self.encryption_key)
        self.image_tokens = _ImageTokenStore()
        self.remote_images = RemoteImageFetcher()
        self._image_budget_lock = threading.RLock()
        self._image_prefetch_slots = threading.BoundedSemaphore(2)
        if key_created and self.settings.cache_mode != "memory":
            self.cache.clear_bodies()
        self.sync = SyncManager(
            self.accounts,
            self.cache,
            self.settings,
            configured_client,
            periodic=periodic,
            prefetch_image=self._prefetch_image_resource,
        )

    def close(self) -> None:
        with self._lock:
            self.sync.stop()
            self.image_tokens.clear()
            self.cache.close()

    @property
    def translation_config(self) -> TranslationConfig | None:
        with self._lock:
            if not self._translation_config_loaded:
                self._translation_config = load_translation_config()
                self._translation_config_loaded = True
            return self._translation_config

    def configure_translation(
        self, config: TranslationConfig, api_key: str
    ) -> TranslationConfig:
        try:
            validated = config.validated()
        except TranslationError as exc:
            raise ViewerError(str(exc)) from exc
        save_translation_config(validated, api_key)
        with self._lock:
            self._translation_config = validated
            self._translation_config_loaded = True
        return validated

    def disconnect_translation(self) -> None:
        delete_translation_config()
        with self._lock:
            self._translation_config = None
            self._translation_config_loaded = True

    def cached_translation_for(
        self, account_name: str, uid: str, item: MailDetail
    ) -> CachedTranslation | None:
        digest = translation_source_digest(
            subject=item.subject,
            text=item.text,
            safe_html=item.safe_html,
            html_policy=item.html_policy,
        )
        try:
            return self.cache.cached_translation(
                account_name, uid, digest, target_language=TRANSLATION_TARGET
            )
        except RuntimeError as exc:
            raise ViewerError(str(exc)) from exc

    def translate_message(
        self,
        account_name: str,
        uid: str,
        *,
        force: bool = False,
        transport=None,
    ) -> CachedTranslation:
        if not uid.isdigit():
            raise ViewerError("邮件 UID 无效。")
        key = (account_name, uid)
        with self._lock:
            translation_lock = self._translation_locks.setdefault(
                key, threading.Lock()
            )
        with translation_lock:
            item = self.message_detail(account_name, uid, prefer_html=True)
            if not force:
                cached = self.cached_translation_for(account_name, uid, item)
                if cached is not None:
                    return cached
            config = self.translation_config
            if config is None:
                raise ViewerError("尚未绑定翻译 API。")
            api_key = _keychain_get_optional(TRANSLATION_KEY_SERVICE) or ""
            try:
                translated = translate_mail_content(
                    config,
                    api_key,
                    subject=item.subject,
                    text=item.text,
                    safe_html=(
                        item.safe_html
                        if item.body_format == "html"
                        and item.html_policy == HTML_POLICY_VERSION
                        else ""
                    ),
                    transport=transport,
                )
            except TranslationError as exc:
                raise ViewerError(str(exc)) from exc
            digest = translation_source_digest(
                subject=item.subject,
                text=item.text,
                safe_html=item.safe_html,
                html_policy=item.html_policy,
            )
            try:
                return self.cache.store_translation(
                    account_name,
                    uid,
                    source_digest=digest,
                    subject=translated.subject,
                    text=translated.text,
                    safe_html=translated.safe_html,
                    source_language=translated.source_language,
                    provider_fingerprint=config.fingerprint(),
                    target_language=TRANSLATION_TARGET,
                )
            except (RuntimeError, ValueError) as exc:
                raise ViewerError(f"译文缓存写入失败：{exc}") from exc

    def materialize_html(
        self,
        account_name: str,
        uid: str,
        item: MailDetail,
        *,
        safe_html: str | None = None,
    ) -> str:
        source_html = item.safe_html if safe_html is None else safe_html
        if not source_html or not item.image_resources:
            return source_html
        urls: dict[str, object] = {}
        for resource in item.image_resources:
            resource_id = str(resource.get("id", ""))
            if not resource_id:
                continue
            token = self.image_tokens.register(account_name, uid, resource)
            value: object = f"/message-image/{token}"
            descriptor = str(resource.get("descriptor", ""))
            urls[resource_id] = (value, descriptor)
        return materialize_image_placeholders(source_html, urls)

    def image_for_token(self, token: str):
        resolved = self.image_tokens.resolve(token)
        if resolved is None:
            raise ViewerError("图片资源已过期，请重新打开邮件。")
        account_name, uid, resource = resolved
        return self._image_for_resource(account_name, uid, resource)

    def _prefetch_image_resource(
        self, account_name: str, uid: str, resource: object, received_at: float
    ) -> None:
        if not isinstance(resource, dict):
            raise ViewerError("图片资源无效。")
        with self._image_prefetch_slots:
            self._image_for_resource(
                account_name,
                uid,
                resource,
                prefetch=True,
                cache_timestamp=received_at,
            )

    def _image_for_resource(
        self,
        account_name: str,
        uid: str,
        resource: dict[str, object],
        *,
        prefetch: bool = False,
        cache_timestamp: float | None = None,
    ):
        resource_id = str(resource.get("id", ""))
        source = str(resource.get("source", ""))
        source_digest = hashlib.sha256(
            json.dumps(resource, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        cached = self.cache.load_image(
            account_name,
            uid,
            resource_id,
            source_digest=source_digest,
            touch=not prefetch,
        )
        if cached is not None:
            return cached.mime_type, cached.data
        source_type = str(resource.get("source_type", ""))
        if source_type == "cid":
            image = self.sync.fetch_image(
                account_name, uid, resource, prefetch=prefetch
            )
        elif source_type == "remote":
            image = self.remote_images.fetch(source)
        elif source_type == "data":
            match = re.fullmatch(
                r"data:image/(?:png|jpeg|gif|webp);base64,([A-Za-z0-9+/=\s]+)",
                source,
                re.IGNORECASE,
            )
            if match is None:
                raise ViewerError("图片 data URI 无效。")
            try:
                payload = base64.b64decode(
                    re.sub(r"\s+", "", match.group(1)), validate=True
                )
                image = validate_image(payload, source[5:].split(";", 1)[0])
            except (ValueError, ImageValidationError) as exc:
                raise ViewerError("图片无法安全验证。") from exc
        else:
            raise ViewerError("图片来源无法安全解析。")
        with self._image_budget_lock:
            cached = self.cache.load_image(
                account_name,
                uid,
                resource_id,
                source_digest=source_digest,
                touch=not prefetch,
            )
            if cached is not None:
                return cached.mime_type, cached.data
            current_bytes = self.cache.image_bytes_for_message(account_name, uid)
            if current_bytes + len(image.data) > MAX_MESSAGE_IMAGE_BYTES:
                raise ViewerError("本邮件图片总量超过安全上限。")
            self.cache.store_image(
                account_name,
                uid,
                resource_id,
                mime_type=image.mime_type,
                source_type=source_type,
                source_digest=source_digest,
                data=image.data,
                now=cache_timestamp,
            )
        return image.mime_type, image.data

    def start_prefetch(self) -> None:
        self.sync.kick_background(DEFAULT_LIMIT, incomplete_only=True)
        self.sync.start_prefetch()

    def restart_prefetch(self) -> None:
        self.sync.restart_prefetch()

    def reconfigure(
        self,
        requested: CacheSettings,
        *,
        existing_cache: str | None,
    ) -> None:
        try:
            requested = requested.validated()
        except ValueError as exc:
            raise ViewerError(str(exc)) from exc
        downgrade = _is_cache_mode_downgrade(
            self.settings.cache_mode, requested.cache_mode
        )
        if downgrade and existing_cache not in {"keep", "purge"}:
            raise ViewerError("降低缓存模式时必须明确选择保留或清除现有缓存。")
        with self._lock:
            self.sync.stop()
            if downgrade and existing_cache == "purge":
                if requested.cache_mode == "metadata":
                    self.cache.clear_bodies()
                else:
                    self.cache.clear_all()
            self.cache.close()
            self.image_tokens.clear()
            save_settings(requested)
            self.settings = requested
            self.cache = CacheStore(
                self.cache_file, self.settings, self.encryption_key
            )
            self.sync = SyncManager(
                self.accounts,
                self.cache,
                self.settings,
                configured_client,
                periodic=self.periodic,
                prefetch_image=self._prefetch_image_resource,
            )
            if self.periodic:
                self.sync.start_prefetch()

    def message_detail(
        self, account_name: str, uid: str, *, prefer_html: bool = False
    ) -> MailDetail:
        if not uid.isdigit():
            raise ViewerError("邮件 UID 无效。")
        try:
            cached = self.cache.cached_detail(account_name, uid)
        except RuntimeError as exc:
            raise ViewerError(str(exc)) from exc
        cached_web_body_current = cached_web_body_is_current(cached)
        if cached is not None and (not prefer_html or cached_web_body_current):
            cached_has_html = bool(
                cached_web_body_current
                and cached.body_format == "html"
                and cached.safe_html
            )
            return MailDetail(
                uid=cached.message.uid,
                subject=cached.message.subject,
                sender=cached.message.sender,
                recipients=cached.message.recipients,
                date=cached.message.date,
                text=cached.text,
                attachments=cached.attachments,
                safe_html=cached.safe_html if cached_has_html else "",
                body_format=cached.body_format,
                blocked_images=(
                    cached.blocked_images if cached_web_body_current else 0
                ),
                html_policy=(
                    cached.html_policy if cached_web_body_current else ""
                ),
                image_resources=(
                    cached.image_resources if cached_web_body_current else ()
                ),
            )
        fetched: MailDetail | None = None
        try:
            fetched = self.sync.fetch_detail(
                account_name, uid, prefer_html=prefer_html
            )
        except ViewerError:
            if cached is None:
                raise
        except TimeoutError as exc:
            if cached is None:
                raise ViewerError(str(exc)) from exc
        except Exception as exc:
            if cached is None:
                raise ViewerError(str(exc)) from exc
        if fetched is not None and (
            cached is None or bool(getattr(fetched, "cacheable", True))
        ):
            return fetched
        if fetched is not None and cached is not None:
            try:
                cached = self.cache.cached_detail(account_name, uid) or cached
            except RuntimeError:
                pass
        fetched_attachments = tuple(
            getattr(fetched, "attachments", ()) if fetched is not None else ()
        )
        fetched_recipients = str(
            getattr(fetched, "recipients", "") if fetched is not None else ""
        )
        return MailDetail(
            uid=cached.message.uid,
            subject=cached.message.subject,
            sender=cached.message.sender,
            recipients=fetched_recipients or cached.message.recipients,
            date=cached.message.date,
            text=cached.text,
            attachments=fetched_attachments or cached.attachments,
            body_format="plain",
        )


def _mail_summary(item: CachedMessage) -> MailSummary:
    return MailSummary(item.uid, item.subject, item.sender, item.date, item.size)


def _mail_detail_record(item: MailDetail) -> dict[str, object]:
    """Keep the public CLI JSON shape independent from web-only body fields."""
    return {
        "uid": item.uid,
        "subject": item.subject,
        "sender": item.sender,
        "recipients": item.recipients,
        "date": item.date,
        "text": item.text,
        "attachments": item.attachments,
    }


def cached_page(
    runtime: ViewerRuntime,
    account_names: Iterable[str],
    *,
    unread_only: bool,
    limit: int,
    offset: int,
) -> tuple[MailPage, tuple[AccountMailSummary, ...]]:
    names = tuple(account_names)
    cached = runtime.cache.query_page(
        names,
        unread_only=unread_only,
        limit=limit,
        offset=offset,
    )
    account_map = {account.name: account for account in runtime.accounts}
    owned = tuple(
        AccountMailSummary(
            account_map[item.account_name], _mail_summary(item), item.unread
        )
        for item in cached.messages
        if item.account_name in account_map
    )
    page_data = MailPage(
        tuple(item.message for item in owned),
        cached.total,
        cached.offset,
        cached.limit,
    )
    return page_data, owned


def cached_errors(
    runtime: ViewerRuntime, account_names: Iterable[str]
) -> tuple[dict[str, str], ...]:
    errors: list[dict[str, str]] = []
    for name in account_names:
        state = runtime.cache.sync_state(name)
        if not state.last_error:
            continue
        detail = state.last_error
        if state.last_success is not None:
            updated = datetime.fromtimestamp(
                state.last_success, CHINA_TIMEZONE
            ).strftime("%Y-%m-%d %H:%M")
            detail += f"（显示缓存，上次成功同步：{updated}）"
        errors.append({"account": name, "error": detail})
    return tuple(errors)


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


MAIL_BODY_CSP = (
    "default-src 'none'; script-src 'none'; connect-src 'none'; "
    "img-src 'self'; font-src 'none'; media-src 'none'; object-src 'none'; "
    "frame-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
    "form-action 'none'"
)

MAIL_BODY_STYLE = """
:root{color-scheme:light}*{box-sizing:border-box}html{min-width:0;background:#fff;overflow-x:hidden;overflow-y:hidden}body{min-width:0;margin:0;padding:28px;background:#fff;color:#242424;font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;overflow-x:auto;overflow-y:hidden;overflow-wrap:anywhere}table{max-width:none}td,th{overflow-wrap:break-word}pre{white-space:pre-wrap;overflow-wrap:anywhere}a{color:#282828;font-weight:650;text-decoration:underline;text-underline-offset:2px;overflow-wrap:anywhere}.mail-image{display:inline-block;max-width:100%;height:auto;margin:4px 0;vertical-align:middle}.mail-image-placeholder{display:inline-flex;align-items:center;justify-content:center;min-width:120px;min-height:52px;max-width:100%;margin:4px 0;padding:10px 14px;border:1px dashed #b8b8b8;border-radius:8px;background:#f5f5f5;color:#6b6b6b;font:13px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;text-align:center;vertical-align:middle}@media(max-width:600px){body{padding:18px 16px;font-size:16px}}
""".strip()


def _mail_body_srcdoc(safe_html: str) -> str:
    document = (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta http-equiv="Content-Security-Policy" content="{html.escape(MAIL_BODY_CSP, quote=True)}">'
        f"<style>{MAIL_BODY_STYLE}</style></head><body>{safe_html}</body></html>"
    )
    return html.escape(document, quote=True)


def _translation_config_fields(
    current: TranslationConfig | None = None,
) -> str:
    selected = current.provider if current is not None else "deepl_free"
    options = "".join(
        f'<option value="{provider}"{" selected" if provider == selected else ""}>{label}</option>'
        for provider, label in TRANSLATION_PROVIDER_LABELS.items()
    )
    show_openai = selected == "openai_compatible"
    base_url = current.base_url if current is not None and show_openai else ""
    model = current.model if current is not None and show_openai else ""
    hidden = "" if show_openai else " hidden"
    return f'''<label class="translation-field"><span class="field-label">翻译服务</span><select class="select" name="provider" data-translation-provider>{options}</select></label>
<label class="translation-field" data-openai-field{hidden}><span class="field-label">Base URL</span><input class="text-input" name="base_url" type="url" maxlength="2048" value="{html.escape(base_url, quote=True)}" placeholder="https://api.openai.com/v1"><span class="form-help">远程接口必须使用 HTTPS；本机 Ollama 可使用 http://127.0.0.1:11434/v1。</span></label>
<label class="translation-field" data-openai-field{hidden}><span class="field-label">模型名称</span><input class="text-input" name="model" maxlength="200" value="{html.escape(model, quote=True)}" placeholder="例如 gpt-4.1-mini 或本机模型名称"></label>
<label class="translation-field"><span class="field-label">API Key</span><input class="text-input" name="api_key" type="password" maxlength="4096" autocomplete="off" spellcheck="false"><span class="form-help">DeepL 和远程接口必须填写；本机 Ollama 可以留空。密钥只写入系统凭据库，页面无法读回。</span></label>'''


BASE_STYLE = """
:root{color-scheme:light dark;--canvas:#fff;--surface:#fff;--surface-raised:#f5f5f5;--ink:#242424;--muted:#6b6b6b;--quiet:#969696;--line:#e6e6e6;--line-strong:#d4d4d4;--accent:#282828;--accent-ink:#fff;--accent-wash:#ededed;--danger:#9b2d30;--danger-wash:#fff2f2;--shadow:0 18px 42px rgba(0,0,0,.06)}
@media(prefers-color-scheme:dark){:root{--canvas:#191919;--surface:#202020;--surface-raised:#292929;--ink:#f2f2f2;--muted:#b5b5b5;--quiet:#858585;--line:#343434;--line-strong:#4a4a4a;--accent:#d9d9d9;--accent-ink:#1b1b1b;--accent-wash:#303030;--danger:#ffb6b6;--danger-wash:#392126;--shadow:0 18px 42px rgba(0,0,0,.24)}}
*{box-sizing:border-box}body{margin:0;background:var(--canvas);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}button,input,select{font:inherit}a{color:inherit}main{max-width:1160px;margin:0 auto;padding:48px 26px 76px}h1,h2,p{margin:0}.app-header{display:flex;align-items:flex-end;justify-content:space-between;gap:28px;padding-bottom:26px;border-bottom:1px solid var(--line)}.header-copy{flex:1;min-width:0}h1{font-size:clamp(26px,4vw,34px);line-height:1.12;letter-spacing:-.025em}.account{margin-top:8px;color:var(--muted);overflow-wrap:anywhere}.account-summary{display:flex;align-items:baseline;flex-wrap:wrap;gap:4px 7px;min-height:22px;margin-top:9px}.account-summary-name{color:var(--ink);font-size:15px;font-weight:700;white-space:nowrap}.account-summary-count,.account-summary-detail{min-width:0;color:var(--muted);font-size:13px;overflow-wrap:anywhere}.account-summary-separator{color:var(--quiet);font-size:12px}.mailbox-state{display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin-top:16px}.state-chip{flex:0 0 auto;padding:4px 9px;border-radius:999px;background:var(--accent-wash);color:var(--accent);font-size:13px;font-weight:700}.state-text{display:grid;grid-template-columns:72px 132px 96px max-content;align-items:center;color:var(--muted);font-size:13px;font-variant-numeric:tabular-nums}.state-stat{position:relative;white-space:nowrap}.state-stat:not(:last-child)::after{content:"·";position:absolute;right:7px;color:var(--quiet)}.state-text-empty{grid-template-columns:188px max-content}.control-bar{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:18px 0}.filter-group,.pagination,.jump-form,.account-switcher,.page-size-switcher{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.segmented{display:flex;padding:3px;border:1px solid var(--line);border-radius:11px;background:var(--surface-raised)}.account-tabs,.page-size-tabs{flex-wrap:wrap}.segment{padding:7px 11px;border-radius:8px;text-decoration:none;color:var(--muted);font-size:14px;font-weight:650}.segment[aria-current="page"],.segment[aria-pressed="true"]{background:var(--surface);box-shadow:0 2px 7px rgba(30,44,67,.12);color:var(--ink)}.field-label{color:var(--muted);font-size:13px;font-weight:650}.select,.page-input{height:35px;border:1px solid var(--line-strong);border-radius:8px;background:var(--surface);color:var(--ink);padding:0 9px}.page-input{width:52px;text-align:center}.button,.page-link{min-height:35px;display:inline-flex;align-items:center;justify-content:center;padding:0 11px;border:1px solid var(--line-strong);border-radius:8px;background:var(--surface);color:var(--ink);text-decoration:none;cursor:pointer;font-weight:650;font-size:14px}.button.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}.button:hover,.page-link:hover{border-color:var(--accent);color:var(--accent)}.button.primary:hover{filter:brightness(1.06);color:var(--accent-ink)}.page-link.disabled{border-color:var(--line);background:var(--surface-raised);color:var(--quiet);cursor:default}.pagination{justify-content:space-between;padding:15px 0}.page-controls{display:flex;align-items:center;gap:7px;flex-wrap:wrap}.page-status{color:var(--muted);font-size:14px;font-weight:650}.mailbox{border-top:1px solid var(--line);border-bottom:1px solid var(--line);background:var(--surface);box-shadow:var(--shadow)}.list-head,.mail{display:grid;grid-template-columns:minmax(220px,1.12fr) minmax(280px,1.85fr) 148px;gap:24px;align-items:center}.list-head{padding:10px 20px;background:var(--surface-raised);border-bottom:1px solid var(--line);color:var(--muted);font-size:12px;font-weight:750;letter-spacing:.04em}.mail{min-height:72px;padding:13px 20px;border-bottom:1px solid var(--line);text-decoration:none;position:relative;transition:background .16s ease,box-shadow .16s ease}.mail:last-child{border-bottom:0}.mail:hover{background:color-mix(in srgb,var(--accent) 6%,var(--surface))}.mail:focus-visible{outline:3px solid color-mix(in srgb,var(--accent) 55%,transparent);outline-offset:-3px;z-index:1}.sender{display:grid;gap:3px}.mail-address-row{display:grid;grid-template-columns:52px minmax(0,1fr);gap:8px;align-items:baseline;min-width:0}.mail-address-label{color:var(--quiet);font-size:11px;font-weight:750;line-height:1.4;white-space:nowrap}.recipient-account{margin-top:3px}.recipient-account .mail-address-label{color:var(--muted)}.sender-name{display:block;color:var(--ink);font-weight:650;overflow-wrap:anywhere}.sender-address,.account-tag{display:block;color:var(--muted);font-size:13px;overflow-wrap:anywhere}.sender-address-primary{color:var(--ink);font-size:15px;font-weight:650}.account-tag{font-size:12px}.subject{font-weight:700;overflow-wrap:anywhere;line-height:1.4}.date{color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap;text-align:right}.empty-state,.error-state{padding:64px 24px;text-align:center;background:var(--surface)}.empty-state h2,.error-state h2{font-size:20px}.empty-state p,.error-state p{max-width:48ch;margin:8px auto 0;color:var(--muted)}.notice{margin:0 0 14px;padding:10px 13px;border:1px solid var(--line-strong);background:var(--surface-raised);color:var(--muted);font-size:14px}.notice.warning{border-color:var(--line-strong)}.detail-header{display:flex;align-items:flex-start;justify-content:space-between;gap:24px;padding-bottom:22px;border-bottom:1px solid var(--line)}.detail-subject{max-width:800px;font-size:clamp(24px,3.6vw,32px);overflow-wrap:anywhere}.read-only-note{margin-top:8px;color:var(--muted)}.message-shell{width:100%;max-width:930px;margin:28px auto 0;background:var(--surface);box-shadow:var(--shadow)}.message-meta{display:grid;grid-template-columns:90px minmax(0,1fr);gap:10px 22px;padding:24px;border-bottom:1px solid var(--line)}.message-meta dt{color:var(--muted);font-weight:650}.message-meta dd{margin:0;overflow-wrap:anywhere}.attachments{margin:20px 24px 0;padding:13px 15px;border:1px solid var(--line);background:var(--surface-raised)}.attachment-label{font-weight:750}.message-body-toolbar{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:18px 24px 12px}.body-view-controls{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.body-view-tabs{display:inline-flex}.body-mode-button{border:0;background:transparent;cursor:pointer}.body-image-notice{color:var(--muted);font-size:13px}.body-panel{min-width:0;border-top:1px solid var(--line)}.body-panel[hidden]{display:none}.message-body-frame{display:block;width:100%;height:320px;min-height:320px;border:0;background:#fff}.body{max-width:76ch;min-height:260px;margin:0;padding:28px 24px 34px;white-space:pre-wrap;overflow-wrap:anywhere;font:15px/1.75 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;unicode-bidi:plaintext}.external-link-dialog{width:min(560px,calc(100vw - 32px));padding:0;border:1px solid var(--line-strong);border-radius:14px;background:var(--surface);color:var(--ink);box-shadow:0 24px 64px rgba(0,0,0,.24)}.external-link-dialog::backdrop{background:rgba(0,0,0,.46)}.external-link-content{padding:24px}.external-link-content h2{font-size:20px}.external-link-content p{margin-top:8px;color:var(--muted)}.external-link-url{display:block;max-height:150px;margin-top:16px;padding:11px 12px;overflow:auto;border:1px solid var(--line);background:var(--surface-raised);font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;overflow-wrap:anywhere;white-space:pre-wrap}.external-link-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:20px}.external-link-actions form{margin:0}.error-state{max-width:700px;margin:72px auto;box-shadow:var(--shadow)}.error-state h2{color:var(--danger)}.error-state .button{margin-top:20px}
.segmented{position:relative;isolation:isolate}.segment{position:relative;z-index:1;transition:color .16s ease}.segmented.is-sliding .segment[aria-current="page"]{background:transparent;box-shadow:none}.segment-slider{position:absolute;z-index:0;border-radius:8px;background:var(--surface);pointer-events:none;will-change:transform}.segment.is-transition-source{color:var(--muted)}.segment.is-transition-target{color:var(--ink)}
.sender,.subject{min-width:0}.subject-text{display:block;min-width:0;overflow-wrap:anywhere}.mailbox-with-status .list-head,.mailbox-with-status .mail{grid-template-columns:minmax(0,1.12fr) minmax(0,1.85fr) 52px 148px}.status-head{text-align:center}.message-status{justify-self:center;min-width:44px;padding:2px 6px;border:1px solid var(--line-strong);border-radius:8px;background:var(--surface-raised);color:var(--muted);font-size:13px;font-weight:750;line-height:1.2;text-align:center;white-space:nowrap}.mail-unread{background:color-mix(in srgb,var(--accent) 4%,var(--surface))}.mail-unread .sender-name,.mail-unread .subject-text{font-weight:750}.mail-unread .message-status{border-color:var(--accent);background:var(--accent);color:var(--accent-ink)}.mail-read .sender-name,.mail-read .subject-text{color:var(--muted);font-weight:500}
@media(prefers-reduced-motion:reduce){.segment{transition:none}}
@media(max-width:780px){main{padding:28px 16px 52px}.app-header,.detail-header{display:block}.account-summary{gap:3px 6px}.mailbox-state{display:block}.state-text{grid-template-columns:112px minmax(0,1fr);gap:4px 12px;margin-top:8px}.state-stat::after{display:none}.state-text-empty{grid-template-columns:1fr}.control-bar{align-items:flex-start;flex-direction:column}.pagination{align-items:flex-start;flex-direction:column}.list-head{display:none}.mail{grid-template-columns:minmax(0,1fr);gap:5px;padding:15px 16px}.subject{grid-column:1;grid-row:1}.mail .sender{grid-column:1;grid-row:2}.date{grid-column:1;grid-row:3;margin-top:2px;text-align:left;white-space:normal;font-size:13px}.mailbox-with-status .mail{grid-template-columns:52px minmax(0,1fr)}.mailbox-with-status .subject,.mailbox-with-status .mail .sender{grid-column:1 / -1}.mailbox-with-status .message-status{grid-column:1;grid-row:3;justify-self:start}.mailbox-with-status .date{grid-column:2;grid-row:3;justify-self:end;margin-top:0;text-align:right}.sender-name{font-size:14px}.sender-address{max-width:100%;white-space:normal;overflow-wrap:anywhere}.message-shell{margin:22px auto 0}.message-meta{grid-template-columns:1fr;gap:2px;padding:20px}.message-meta dt:not(:first-child){margin-top:12px}.message-body-toolbar{align-items:flex-start;flex-direction:column;padding:16px 20px 10px}.message-body-frame{height:520px}.body{padding:24px 20px}.jump-form{width:100%}.page-input{width:64px}}
.header-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.number-input{width:110px;height:35px;border:1px solid var(--line-strong);border-radius:8px;background:var(--surface);color:var(--ink);padding:0 9px}.button.danger{border-color:var(--danger);color:var(--danger)}.button:focus-visible,.page-link:focus-visible,.select:focus-visible,.page-input:focus-visible,.number-input:focus-visible,.text-input:focus-visible,.segment:focus-visible,.body-mode-button:focus-visible{outline:3px solid color-mix(in srgb,var(--accent) 55%,transparent);outline-offset:2px}.settings-header{display:flex;justify-content:space-between;align-items:flex-start;gap:24px;padding-bottom:24px;border-bottom:1px solid var(--line)}.settings-shell{max-width:760px;margin-top:28px;display:grid;gap:18px}.settings-card{padding:24px;background:var(--surface);border:1px solid var(--line);box-shadow:var(--shadow)}.settings-card h2{font-size:20px}.settings-card p{margin-top:7px;color:var(--muted)}.settings-form{display:grid;gap:18px;margin-top:20px}.form-row{display:grid;grid-template-columns:180px minmax(0,1fr);gap:16px;align-items:center}.form-help{display:block;margin-top:5px;color:var(--muted);font-size:13px}.settings-actions{display:flex;gap:8px;flex-wrap:wrap;padding-top:4px}.danger-zone{border-color:color-mix(in srgb,var(--danger) 35%,var(--line))}.inline-form{display:inline-flex;margin:14px 8px 0 0}@media(max-width:780px){.settings-header{display:block}.header-actions{margin-top:18px}.form-row{grid-template-columns:1fr;gap:6px}.settings-card{padding:20px}}
.translation-toolbar-side,.translation-actions{display:flex;align-items:center;justify-content:flex-end;gap:8px;flex-wrap:wrap}.translation-actions form{margin:0}.translation-status{max-width:930px;margin:18px auto 0}.button[disabled]{cursor:wait;opacity:.62}.translation-dialog{width:min(620px,calc(100vw - 32px));max-height:calc(100vh - 40px);padding:0;overflow:auto;border:0;border-radius:11px;background:var(--surface);color:var(--ink);box-shadow:var(--shadow)}.translation-dialog::backdrop{background:color-mix(in srgb,var(--ink) 46%,transparent)}.translation-dialog-content{padding:24px}.translation-dialog-content h2{font-size:20px}.translation-dialog-content>p{margin-top:8px;color:var(--muted)}.translation-config-form{display:grid;gap:16px;margin-top:20px}.translation-field{display:grid;gap:6px}.translation-field[hidden]{display:none}.text-input{width:100%;min-width:0;height:38px;padding:0 10px;border:1px solid var(--line-strong);border-radius:8px;background:var(--surface);color:var(--ink)}.translation-disclosure{padding:11px 12px;background:var(--surface-raised);color:var(--muted);font-size:13px}.translation-provider-status{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;margin-top:14px}.translation-provider-status strong{overflow-wrap:anywhere}.settings-inline-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:16px}.settings-inline-actions form{margin:0}@media(max-width:780px){.translation-toolbar-side{align-items:flex-start;justify-content:flex-start;flex-direction:column}.translation-actions{justify-content:flex-start}.translation-dialog-content{padding:20px}.text-input,.translation-config-form .select{font-size:16px}}
"""


BASE_SCRIPT = """
(() => {
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let navigating = false;
  const wiredFrameDocuments = new WeakSet();
  const frameResizeObservers = new WeakMap();

  const fetchPage = async (url) => {
    const response = await window.fetch(url, {
      credentials: "same-origin",
      headers: {Accept: "text/html", "X-Requested-With": "qqmail-viewer"},
    });
    const contentType = response.headers.get("Content-Type") || "";
    if (!contentType.includes("text/html")) {
      throw new Error("目标页面不是 HTML。");
    }
    const source = await response.text();
    const nextDocument = new DOMParser().parseFromString(source, "text/html");
    const nextMain = nextDocument.querySelector("main");
    if (!nextMain) throw new Error("目标页面缺少主内容。");
    return {
      main: nextMain,
      title: nextDocument.title,
      url: response.url || url,
    };
  };

  const replacePage = (nextPage, pushHistory, focusGroup) => {
    const currentMain = document.querySelector("main");
    if (!currentMain) throw new Error("当前页面缺少主内容。");
    currentMain.replaceWith(nextPage.main);
    document.title = nextPage.title;
    initializeMessageBodies();
    initializeTranslationControls();
    if (pushHistory) {
      window.history.pushState({mailViewer: true}, "", nextPage.url);
    }
    if (focusGroup) {
      const group = Array.from(document.querySelectorAll(".segmented")).find(
        (item) => item.getAttribute("aria-label") === focusGroup,
      );
      const selected = group?.querySelector('[aria-current="page"]');
      if (selected) {
        try {
          selected.focus({preventScroll: true});
        } catch (_error) {
          selected.focus();
        }
      }
    }
  };

  const slideTo = (group, current, target) => {
    if (reducedMotion.matches || typeof Element.prototype.animate !== "function") {
      return Promise.resolve();
    }
    const groupRect = group.getBoundingClientRect();
    const currentRect = current.getBoundingClientRect();
    const targetRect = target.getBoundingClientRect();
    const slider = document.createElement("span");
    slider.className = "segment-slider";
    slider.setAttribute("aria-hidden", "true");
    slider.style.left = `${currentRect.left - groupRect.left - group.clientLeft}px`;
    slider.style.top = `${currentRect.top - groupRect.top - group.clientTop}px`;
    slider.style.width = `${currentRect.width}px`;
    slider.style.height = `${currentRect.height}px`;
    slider.style.transformOrigin = "left center";
    group.prepend(slider);
    group.classList.add("is-sliding");
    current.classList.add("is-transition-source");
    target.classList.add("is-transition-target");

    const deltaX = targetRect.left - currentRect.left;
    const deltaY = targetRect.top - currentRect.top;
    const scaleX = targetRect.width / currentRect.width;
    const animation = slider.animate(
      [
        {transform: "translate3d(0, 0, 0) scaleX(1)"},
        {transform: `translate3d(${deltaX}px, ${deltaY}px, 0) scaleX(${scaleX})`},
      ],
      {
        duration: 240,
        easing: "cubic-bezier(0.16, 1, 0.3, 1)",
        fill: "forwards",
      },
    );
    return Promise.race([
      animation.finished.catch(() => undefined),
      new Promise((resolve) => window.setTimeout(resolve, 320)),
    ]);
  };

  const resizeMessageFrame = (frame) => {
    try {
      const frameDocument = frame.contentDocument;
      if (!frameDocument) return;
      frame.style.height = "320px";
      const height = Math.max(
        frameDocument.body?.scrollHeight || 0,
        frameDocument.documentElement?.scrollHeight || 0,
      );
      frame.style.height = `${Math.max(320, Math.ceil(height) + 2)}px`;
    } catch (_error) {
      frame.style.height = "560px";
    }
  };

  const confirmExternalLink = (shell, url) => {
    const dialog = shell.querySelector("[data-external-link-dialog]");
    const output = dialog?.querySelector("[data-external-link-url]");
    if (
      typeof HTMLDialogElement === "undefined" ||
      !(dialog instanceof HTMLDialogElement) ||
      !output
    ) {
      if (window.confirm(`即将打开外部网页：\n${url.href}\n\n是否继续？`)) {
        const opened = window.open(url.href, "_blank", "noopener,noreferrer");
        if (opened) opened.opener = null;
      }
      return;
    }
    output.textContent = url.href;
    dialog.dataset.externalUrl = url.href;
    dialog.showModal();
  };

  const wireFrameImages = (shell, frame, frameDocument) => {
    const status = shell.querySelector("[data-image-status]");
    const images = Array.from(frameDocument.images);
    if (!images.length || !status) return;
    let settled = 0;
    let failed = 0;
    const seen = new WeakSet();
    const update = () => {
      const total = images.length;
      if (settled < total) {
        status.textContent = `正在加载 ${settled} / ${total} 张图片`;
      } else if (failed) {
        status.textContent = `${total - failed} 张图片已加载，${failed} 张无法安全加载`;
      } else {
        status.textContent = `${total} 张图片已加载`;
      }
      resizeMessageFrame(frame);
    };
    const settle = (image, didFail) => {
      if (seen.has(image)) return;
      seen.add(image);
      settled += 1;
      if (didFail) {
        failed += 1;
        const placeholder = frameDocument.createElement("span");
        placeholder.className = "mail-image-placeholder";
        placeholder.setAttribute("role", "img");
        placeholder.setAttribute("aria-label", image.alt || "图片未加载");
        placeholder.textContent = image.alt || "图片未加载";
        image.replaceWith(placeholder);
      }
      update();
    };
    images.forEach((image) => {
      image.addEventListener("load", () => settle(image, false), {once: true});
      image.addEventListener("error", () => settle(image, true), {once: true});
      if (image.complete) {
        window.setTimeout(() => settle(image, image.naturalWidth === 0), 0);
      }
    });
    update();
  };

  const initializeMessageBodies = () => {
    document.querySelectorAll("[data-message-body]").forEach((shell) => {
      if (!(shell instanceof HTMLElement) || shell.dataset.initialized === "true") return;
      shell.dataset.initialized = "true";

      const buttons = Array.from(shell.querySelectorAll("[data-body-mode]"));
      const panels = Array.from(shell.querySelectorAll("[data-body-panel]"));
      buttons.forEach((button) => {
        button.addEventListener("click", () => {
          const mode = button.getAttribute("data-body-mode");
          buttons.forEach((item) => {
            item.setAttribute(
              "aria-pressed",
              item.getAttribute("data-body-mode") === mode ? "true" : "false",
            );
          });
          panels.forEach((panel) => {
            panel.hidden = panel.getAttribute("data-body-panel") !== mode;
          });
          const activePanel = panels.find(
            (panel) => panel.getAttribute("data-body-panel") === mode,
          );
          const frame = activePanel?.querySelector(".message-body-frame");
          if (frame instanceof HTMLIFrameElement) resizeMessageFrame(frame);
          const subject = document.querySelector("[data-detail-subject]");
          if (subject instanceof HTMLElement) {
            const value = mode === "translated"
              ? subject.dataset.translatedSubject
              : subject.dataset.originalSubject;
            if (value) {
              subject.textContent = value;
              document.title = value;
            }
          }
        });
      });

      shell.querySelectorAll(".message-body-frame").forEach((frame) => {
        if (!(frame instanceof HTMLIFrameElement)) return;
        const wireFrame = () => {
          resizeMessageFrame(frame);
          try {
            const frameDocument = frame.contentDocument;
            if (!frameDocument || wiredFrameDocuments.has(frameDocument)) return;
            wiredFrameDocuments.add(frameDocument);
            if (typeof ResizeObserver !== "undefined") {
              const observer = new ResizeObserver(() => resizeMessageFrame(frame));
              if (frameDocument.documentElement) observer.observe(frameDocument.documentElement);
              if (frameDocument.body) observer.observe(frameDocument.body);
              frameResizeObservers.set(frame, observer);
            }
            wireFrameImages(shell, frame, frameDocument);
            frameDocument.addEventListener("click", (event) => {
              const eventTarget = event.target;
              if (!eventTarget || eventTarget.nodeType !== 1) return;
              const anchor = eventTarget.closest?.("a[href]");
              if (!anchor) return;
              event.preventDefault();
              if (event.button !== 0) return;
              const href = anchor.getAttribute("href");
              if (!href) return;
              let destination;
              try {
                destination = new URL(href);
              } catch (_error) {
                return;
              }
              if (!['http:', 'https:'].includes(destination.protocol)) return;
              confirmExternalLink(shell, destination);
            });
          } catch (_error) {
            return;
          }
        };
        frame.addEventListener("load", wireFrame);
        if (frame.contentDocument?.readyState === "complete") {
          window.setTimeout(wireFrame, 0);
        }
      });

      const dialog = shell.querySelector("[data-external-link-dialog]");
      const openButton = dialog?.querySelector("[data-open-external-link]");
      if (
        typeof HTMLDialogElement !== "undefined" &&
        dialog instanceof HTMLDialogElement &&
        openButton instanceof HTMLButtonElement
      ) {
        openButton.addEventListener("click", () => {
          let destination;
          try {
            destination = new URL(dialog.dataset.externalUrl || "");
          } catch (_error) {
            dialog.close();
            return;
          }
          if (!['http:', 'https:'].includes(destination.protocol)) {
            dialog.close();
            return;
          }
          const opened = window.open(destination.href, "_blank", "noopener,noreferrer");
          if (opened) opened.opener = null;
          dialog.close();
        });
      }
    });
  };

  const initializeTranslationControls = () => {
    document.querySelectorAll("[data-translation-config-form]").forEach((form) => {
      if (!(form instanceof HTMLFormElement) || form.dataset.initialized === "true") return;
      form.dataset.initialized = "true";
      const provider = form.querySelector("[data-translation-provider]");
      const openAIFields = Array.from(form.querySelectorAll("[data-openai-field]"));
      const updateFields = () => {
        const showOpenAI = provider instanceof HTMLSelectElement
          && provider.value === "openai_compatible";
        openAIFields.forEach((field) => {
          field.hidden = !showOpenAI;
          field.querySelectorAll("input").forEach((input) => {
            input.disabled = !showOpenAI;
          });
        });
      };
      provider?.addEventListener("change", updateFields);
      updateFields();
    });

    document.querySelectorAll("[data-translation-config-open]").forEach((button) => {
      if (!(button instanceof HTMLButtonElement) || button.dataset.initialized === "true") return;
      button.dataset.initialized = "true";
      button.addEventListener("click", () => {
        const dialog = document.querySelector("[data-translation-dialog]");
        if (typeof HTMLDialogElement !== "undefined" && dialog instanceof HTMLDialogElement) {
          dialog.showModal();
          const firstField = dialog.querySelector("select, input");
          if (firstField instanceof HTMLElement) firstField.focus();
        } else {
          window.location.assign(button.dataset.settingsHref || "/settings#translation");
        }
      });
    });

    document.querySelectorAll("[data-translation-dialog-close]").forEach((button) => {
      if (!(button instanceof HTMLButtonElement) || button.dataset.initialized === "true") return;
      button.dataset.initialized = "true";
      button.addEventListener("click", () => {
        const dialog = button.closest("dialog");
        if (dialog instanceof HTMLDialogElement) dialog.close();
      });
    });

    document.querySelectorAll("[data-translation-submit]").forEach((form) => {
      if (!(form instanceof HTMLFormElement) || form.dataset.submitInitialized === "true") return;
      form.dataset.submitInitialized = "true";
      form.addEventListener("submit", () => {
        form.setAttribute("aria-busy", "true");
        form.querySelectorAll('button[type="submit"]').forEach((button) => {
          if (!(button instanceof HTMLButtonElement)) return;
          button.disabled = true;
          button.textContent = button.dataset.loadingLabel || "正在翻译…";
        });
      });
    });
  };

  document.addEventListener("click", async (event) => {
    if (!(event.target instanceof Element)) return;
    const target = event.target.closest("a.segment");
    if (!target) return;
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    ) return;
    event.preventDefault();
    if (navigating || target.getAttribute("aria-current") === "page") return;

    const destination = new URL(target.href, window.location.href);
    const group = target.closest(".segmented");
    const current = group?.querySelector('a.segment[aria-current="page"]');
    if (!group || !current || destination.origin !== window.location.origin) {
      window.location.assign(destination.href);
      return;
    }

    navigating = true;
    const currentMain = document.querySelector("main");
    currentMain?.setAttribute("aria-busy", "true");
    const focusGroup = event.detail === 0 ? group.getAttribute("aria-label") : null;
    const pageRequest = fetchPage(destination.href);
    const results = await Promise.allSettled([
      pageRequest,
      slideTo(group, current, target),
    ]);
    const pageResult = results[0];
    try {
      if (pageResult.status !== "fulfilled") throw pageResult.reason;
      replacePage(pageResult.value, true, focusGroup);
    } catch (_error) {
      window.location.assign(destination.href);
    } finally {
      currentMain?.removeAttribute("aria-busy");
      navigating = false;
    }
  });

  window.addEventListener("popstate", async () => {
    if (navigating) return;
    navigating = true;
    const currentMain = document.querySelector("main");
    currentMain?.setAttribute("aria-busy", "true");
    try {
      replacePage(await fetchPage(window.location.href), false, null);
    } catch (_error) {
      window.location.reload();
    } finally {
      currentMain?.removeAttribute("aria-busy");
      navigating = false;
    }
  });
  window.addEventListener("resize", () => {
    document.querySelectorAll(".message-body-frame").forEach((frame) => {
      if (frame instanceof HTMLIFrameElement) resizeMessageFrame(frame);
    });
  });
  initializeMessageBodies();
  initializeTranslationControls();
})();
"""


def page(title: str, content: str) -> bytes:
    document = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>{html.escape(title)}</title><style>{BASE_STYLE}</style><script src="/assets/viewer.js" defer></script></head><body><main>{content}</main></body></html>"""
    return document.encode("utf-8")


class ViewerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], runtime: ViewerRuntime) -> None:
        self.runtime = runtime
        super().__init__(address, ViewerHandler)


class ViewerHandler(BaseHTTPRequestHandler):
    server_version = "QQMailViewer/1.3.0"

    def _runtime(self) -> ViewerRuntime:
        runtime = getattr(self.server, "runtime", None)
        if not isinstance(runtime, ViewerRuntime):
            raise ViewerError("网页运行时尚未初始化。")
        return runtime

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path)
        try:
            query = parse_qs(route.query)
            if route.path == "/assets/viewer.js":
                self._send(
                    BASE_SCRIPT.encode("utf-8"),
                    content_type="text/javascript; charset=utf-8",
                )
            elif route.path == "/":
                self._home(query)
            elif route.path == "/message":
                self._message(query)
            elif route.path.startswith("/message-image/"):
                self._message_image(route.path.removeprefix("/message-image/"))
            elif route.path == "/api/messages":
                self._api_messages(query)
            elif route.path == "/settings":
                self._settings(query)
            else:
                self._send(
                    page(
                        "未找到",
                        '<section class="error-state"><h2>页面不存在</h2><p>请从邮箱列表重新开始。</p><a class="button primary" href="/">返回邮箱列表</a></section>',
                    ),
                    HTTPStatus.NOT_FOUND,
                )
        except ViewerError as exc:
            self._error_page(route, exc)

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path)
        try:
            handlers = {
                "/settings": self._settings_post,
                "/translation/configure": self._translation_configure_post,
                "/translation/run": self._translation_run_post,
                "/translation/disconnect": self._translation_disconnect_post,
                "/translation/cache/clear": self._translation_cache_clear_post,
            }
            handler = handlers.get(route.path)
            if handler is None:
                self._send(page("未找到", ""), HTTPStatus.NOT_FOUND)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ViewerError("请求长度无效。") from exc
            if not 0 < length <= 16_384:
                raise ViewerError("设置请求大小无效。")
            form = parse_qs(
                self.rfile.read(length).decode("utf-8", errors="strict"),
                keep_blank_values=True,
            )
            token = form.get("csrf", [""])[0]
            if not secrets.compare_digest(token, PROCESS_CSRF_TOKEN):
                self._send(
                    page(
                        "请求已拒绝",
                        '<section class="error-state"><h2>请求已拒绝</h2><p>设置页面已过期，请返回后重新提交。</p><a class="button primary" href="/settings">返回设置</a></section>',
                    ),
                    HTTPStatus.FORBIDDEN,
                )
                return
            handler(form)
        except (UnicodeDecodeError, ViewerError) as exc:
            self._error_page(route, ViewerError(str(exc)), HTTPStatus.BAD_REQUEST)

    def _translation_return_url(
        self,
        form: dict[str, list[str]],
        *,
        status: str,
        detail: str = "",
    ) -> str:
        scope = form.get("scope", ["settings"])[0]
        if scope != "message":
            values = {"translation": status}
            if detail:
                values["translation_detail"] = detail[:400]
            return f"/settings?{urlencode(values)}#translation"
        runtime = self._runtime()
        account_name = form.get("account", [""])[0]
        return_account = form.get("return_account", [account_name])[0]
        account = find_account(account_name, runtime.accounts)
        if return_account != "all":
            find_account(return_account, runtime.accounts)
        uid = form.get("uid", [""])[0]
        if not uid.isdigit():
            raise ViewerError("邮件 UID 无效。")
        params = parse_listing_params(form)
        values: dict[str, object] = {
            "account": account.name,
            "uid": uid,
            "return_account": return_account,
            "unread": "1" if params.unread_only else "0",
            "limit": params.limit,
            "page": params.requested_page,
            "translation": status,
        }
        if detail:
            values["translation_detail"] = detail[:400]
        return f"/message?{urlencode(values)}"

    @staticmethod
    def _translation_config_from_form(
        form: dict[str, list[str]],
    ) -> tuple[TranslationConfig, str]:
        provider = form.get("provider", [""])[0]
        config = TranslationConfig(
            provider=provider,
            base_url=(
                form.get("base_url", [""])[0]
                if provider == "openai_compatible"
                else ""
            ),
            model=(
                form.get("model", [""])[0]
                if provider == "openai_compatible"
                else ""
            ),
        )
        return config, form.get("api_key", [""])[0]

    def _translation_configure_post(
        self, form: dict[str, list[str]]
    ) -> None:
        runtime = self._runtime()
        try:
            config, api_key = self._translation_config_from_form(form)
            runtime.configure_translation(config, api_key)
            if form.get("scope", ["settings"])[0] == "message":
                runtime.translate_message(
                    form.get("account", [""])[0],
                    form.get("uid", [""])[0],
                    force=True,
                )
        except ViewerError as exc:
            self._redirect(
                self._translation_return_url(
                    form, status="error", detail=str(exc)
                )
            )
            return
        self._redirect(self._translation_return_url(form, status="saved"))

    def _translation_run_post(self, form: dict[str, list[str]]) -> None:
        runtime = self._runtime()
        try:
            runtime.translate_message(
                form.get("account", [""])[0],
                form.get("uid", [""])[0],
                force=form.get("force", ["0"])[0] == "1",
            )
        except ViewerError as exc:
            self._redirect(
                self._translation_return_url(
                    form, status="error", detail=str(exc)
                )
            )
            return
        self._redirect(self._translation_return_url(form, status="done"))

    def _translation_disconnect_post(
        self, form: dict[str, list[str]]
    ) -> None:
        self._runtime().disconnect_translation()
        self._redirect(self._translation_return_url(form, status="disconnected"))

    def _translation_cache_clear_post(
        self, form: dict[str, list[str]]
    ) -> None:
        self._runtime().cache.clear_translations()
        self._redirect(self._translation_return_url(form, status="cleared"))

    def _error_page(
        self,
        route,
        exc: ViewerError,
        status: HTTPStatus = HTTPStatus.BAD_GATEWAY,
    ) -> None:
        retry_path = route.path or "/"
        if route.query:
            retry_path = f"{retry_path}?{route.query}"
        content = f'''<section class="error-state"><h2>暂时无法完成操作</h2><p>{html.escape(str(exc))}</p><a class="button primary" href="{html.escape(retry_path, quote=True)}">重新尝试</a></section>'''
        self._send(page("发生错误", content), status)

    def _prepare_cache(
        self,
        runtime: ViewerRuntime,
        account_names: tuple[str, ...],
        *,
        unread_only: bool,
        limit: int,
        refresh: bool,
    ) -> tuple[dict[str, str], ...]:
        errors: tuple[dict[str, str], ...] = ()
        if refresh:
            errors = runtime.sync.sync_accounts(
                account_names, wait=True, timeout=8.0, force=True
            )
        else:
            needs_seed = any(
                not (
                    runtime.cache.sync_state(name).unread_seeded
                    if unread_only
                    else runtime.cache.sync_state(name).all_seeded
                )
                for name in account_names
            )
            if needs_seed:
                errors = runtime.sync.ensure_seed(
                    account_names,
                    unread_only=unread_only,
                    limit=limit,
                    timeout=8.0,
                )
            else:
                incomplete = tuple(
                    name
                    for name in account_names
                    if not runtime.cache.sync_state(name).full_sync_complete
                )
                if incomplete:
                    runtime.sync.sync_accounts(
                        incomplete, wait=False, force=True
                    )
                complete = tuple(name for name in account_names if name not in incomplete)
                runtime.sync.sync_accounts(complete, wait=False)
        start_prefetch = getattr(runtime, "start_prefetch", None)
        if callable(start_prefetch):
            start_prefetch()
        combined = [*errors, *cached_errors(runtime, account_names)]
        deduplicated: dict[str, dict[str, str]] = {}
        for error in combined:
            deduplicated[str(error["account"])] = error
        return tuple(deduplicated.values())

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
        runtime = self._runtime()
        accounts = runtime.accounts
        provider_counts = {
            provider: sum(account.provider == provider for account in accounts)
            for provider in {account.provider for account in accounts}
        }
        account_labels: dict[str, str] = {}
        for account in accounts:
            provider_label = PROVIDER_TAB_LABELS.get(account.provider, account.name)
            if account.provider == "custom":
                account_labels[account.name] = account.name
            elif provider_counts[account.provider] > 1:
                account_labels[account.name] = f"{provider_label} · {account.name}"
            else:
                account_labels[account.name] = provider_label
        requested = query.get("account", [None])[0]
        selection = requested or ("all" if len(accounts) >= 2 else default_account(accounts).name)
        if selection == "all":
            selected_names = tuple(account.name for account in accounts)
            scope_summary = (
                f'<p class="account-summary" aria-label="已添加邮箱，共 {len(accounts)} 个">'
                '<strong class="account-summary-name">已添加邮箱</strong>'
                f'<span class="account-summary-count">（{len(accounts)} 个）</span></p>'
            )
        else:
            selected_account = find_account(selection, accounts)
            selected_names = (selected_account.name,)
            scope_name = account_labels[selected_account.name]
            scope_summary = (
                '<p class="account-summary">'
                f'<strong class="account-summary-name" title="{html.escape(scope_name, quote=True)}">{html.escape(scope_name)}</strong>'
                '<span class="account-summary-separator" aria-hidden="true">·</span>'
                f'<span class="account-summary-detail" title="{html.escape(selected_account.email, quote=True)}">{html.escape(selected_account.email)}</span></p>'
            )
        errors = self._prepare_cache(
            runtime,
            selected_names,
            unread_only=params.unread_only,
            limit=params.limit,
            refresh=query.get("refresh", ["0"])[0] == "1",
        )
        page_data, aggregate_items = cached_page(
            runtime,
            selected_names,
            unread_only=params.unread_only,
            limit=params.limit,
            offset=params.offset,
        )
        items = tuple(
            (entry.message, entry.account, entry.unread is True)
            for entry in aggregate_items
        )
        rows = []
        for item, item_account, is_unread in items:
            sender_name, sender_address = sender_parts(item.sender)
            sender_name_row = (
                f'<span class="sender-name">{html.escape(sender_name)}</span>'
                if sender_name
                else ""
            )
            sender_address_class = (
                "sender-address"
                if sender_name
                else "sender-address sender-address-primary"
            )
            sender_address_row = (
                f'<span class="mail-address-row sender-email"><span class="mail-address-label">发件邮箱</span><span class="{sender_address_class}">{html.escape(sender_address)}</span></span>'
                if sender_address
                else ""
            )
            account_tag = (
                f'<span class="mail-address-row recipient-account"><span class="mail-address-label">收件账户</span><span class="account-tag">{html.escape(item_account.name)} · {html.escape(item_account.email)}</span></span>'
                if selection == "all"
                else ""
            )
            state_class = ""
            state_badge = ""
            if not params.unread_only:
                state_class = " mail-unread" if is_unread else " mail-read"
                state_label = "未读" if is_unread else "已读"
                state_badge = f'<span class="message-status">{state_label}</span>'
            rows.append(
                f'''<a class="mail{state_class}" href="{html.escape(self._message_url(item, params, page_data.offset, item_account.name, selection), quote=True)}">
  <span class="sender">{sender_name_row}{sender_address_row}{account_tag}</span>
  <span class="subject"><span class="subject-text">{html.escape(item.subject)}</span></span>{state_badge}<time class="date">{html.escape(item.date)}</time>
</a>'''
        )
        if page_data.total:
            state_text_class = "state-text"
            state_text = (
                f'<span class="state-stat state-total">共 {page_data.total} 封</span>'
                f'<span class="state-stat state-range">显示第 {page_data.offset + 1}–{page_data.offset + len(page_data.messages)} 封</span>'
                f'<span class="state-stat state-page">第 {page_data.current_page} / {page_data.page_count} 页</span>'
                '<span class="state-stat state-sort">按日期倒序</span>'
            )
        else:
            state_text_class = "state-text state-text-empty"
            state_text = (
                '<span class="state-stat state-empty">没有符合当前筛选的邮件</span>'
                '<span class="state-stat state-sort">按日期倒序</span>'
            )
        notice = ""
        if params.invalid_page:
            notice = '<p class="notice" role="status">页码必须是大于 0 的整数，已显示可用页面。</p>'
        elif page_data.total and page_data.offset != params.offset:
            notice = f'<p class="notice" role="status">第 {params.requested_page} 页不存在，已显示最后一页。</p>'
        indexing = any(
            not runtime.cache.sync_state(name).full_sync_complete
            for name in selected_names
        )
        if indexing:
            notice += '<p class="notice" role="status">正在后台补齐收件箱索引；当前总数只包含已缓存邮件。</p>'
        warnings = "".join(
            f'<p class="notice warning" role="status">账户 {html.escape(error["account"])} 暂时无法读取：{html.escape(error["error"])}</p>'
            for error in errors
        )
        unread_url = listing_url(True, params.limit, 0, selection)
        all_url = listing_url(False, params.limit, 0, selection)
        selected_mode = "未读邮件" if params.unread_only else "全部邮件"
        mailbox_class = "mailbox" if params.unread_only else "mailbox mailbox-with-status"
        status_heading = "" if params.unread_only else '<span class="status-head">状态</span>'
        listing = "".join(rows) if rows else '<div class="empty-state"><h2>这里没有符合条件的邮件</h2><p>你可以切换到全部邮件，或稍后刷新再试。</p></div>'
        account_link_parts: list[str] = []
        for account in accounts:
            current_attribute = ' aria-current="page"' if selection == account.name else ""
            account_link_parts.append(
                f'<a class="segment" href="{html.escape(listing_url(params.unread_only, params.limit, 0, account.name), quote=True)}" aria-label="{html.escape(account.name + "，" + account.email, quote=True)}" title="{html.escape(account.email, quote=True)}"{current_attribute}>{html.escape(account_labels[account.name])}</a>'
            )
        account_links = "".join(account_link_parts)
        if len(accounts) >= 2:
            current_attribute = ' aria-current="page"' if selection == "all" else ""
            all_accounts_link = f'<a class="segment" href="{html.escape(listing_url(params.unread_only, params.limit, 0, "all"), quote=True)}" aria-label="全部账户，共 {len(accounts)} 个"{current_attribute}>全部（{len(accounts)}）</a>'
            account_switcher = f'<div class="account-switcher"><span class="field-label">账户</span><nav class="segmented account-tabs" aria-label="账户切换">{all_accounts_link}{account_links}</nav></div>'
        else:
            account_switcher = ""
        page_size_link_parts: list[str] = []
        page_size_values = (30, 50, 100)
        for value in page_size_values:
            current_attribute = ' aria-current="page"' if params.limit == value else ""
            page_size_link_parts.append(
                f'<a class="segment" href="{html.escape(listing_url(params.unread_only, value, 0, selection), quote=True)}" aria-label="每页 {value} 封"{current_attribute}>{value}</a>'
            )
        page_size_switcher = f'<div class="page-size-switcher"><span class="field-label">每页</span><nav class="segmented page-size-tabs" aria-label="每页邮件数量">{"".join(page_size_link_parts)}</nav></div>'
        refresh_url = listing_url(
            params.unread_only, params.limit, page_data.offset, selection
        ) + "&refresh=1"
        content = f'''<header class="app-header">
  <div class="header-copy">
    <h1>{APP_TITLE}</h1>
    {scope_summary}
    <div class="mailbox-state"><span class="state-chip">{selected_mode}</span><span class="{state_text_class}">{state_text}</span></div>
  </div>
  <div class="header-actions"><a class="button" href="/settings">设置</a><a class="button primary" href="{html.escape(refresh_url, quote=True)}">刷新列表</a></div>
</header>
<div class="control-bar">
  <div class="filter-group">
    <nav class="segmented" aria-label="邮件筛选">
      <a class="segment" href="{html.escape(unread_url, quote=True)}"{ ' aria-current="page"' if params.unread_only else ''}>仅看未读</a>
      <a class="segment" href="{html.escape(all_url, quote=True)}"{ ' aria-current="page"' if not params.unread_only else ''}>查看全部</a>
    </nav>
    {account_switcher}
    {page_size_switcher}
  </div>
</div>
{notice}
{warnings}
{self._pagination(page_data, params, selection)}
<section class="{mailbox_class}" aria-label="{selected_mode}列表">
  <div class="list-head"><span>发件人</span><span>主题</span>{status_heading}<span>日期</span></div>
  {listing}
</section>
{self._pagination(page_data, params, selection)}'''
        self._send(page(APP_TITLE, content))

    def _message(self, query: dict[str, list[str]]) -> None:
        uid = query.get("uid", [""])[0]
        params = parse_listing_params(query)
        account_name = query.get("account", [""])[0]
        runtime = self._runtime()
        account = find_account(account_name, runtime.accounts)
        return_account = query.get("return_account", [account.name])[0]
        if return_account != "all":
            find_account(return_account, runtime.accounts)
        item = runtime.message_detail(account.name, uid, prefer_html=True)
        cached_translation_for = getattr(runtime, "cached_translation_for", None)
        translation = (
            cached_translation_for(account.name, uid, item)
            if callable(cached_translation_for)
            else None
        )
        translation_config = getattr(runtime, "translation_config", None)
        attachment_box = ""
        if item.attachments:
            names = "、".join(html.escape(name) for name in item.attachments)
            attachment_box = f'<aside class="attachments"><span class="attachment-label">附件</span>（仅列出，不下载）：{names}</aside>'
        plain_text = normalize_plain_text(item.text)
        plain_body = html.escape(plain_text) or "（邮件没有可显示的文本正文）"
        has_rich_body = bool(
            item.body_format == "html"
            and item.safe_html
            and item.html_policy == HTML_POLICY_VERSION
        )
        materialize_html = getattr(runtime, "materialize_html", None)
        rendered_html = (
            materialize_html(account.name, uid, item)
            if has_rich_body and callable(materialize_html)
            else (item.safe_html if has_rich_body else "")
        )
        translated_html = ""
        if translation is not None and translation.safe_html:
            translated_html = (
                materialize_html(
                    account.name,
                    uid,
                    item,
                    safe_html=translation.safe_html,
                )
                if callable(materialize_html)
                else translation.safe_html
            )
        image_count = len(item.image_resources)
        hidden_images = max(0, item.blocked_images - image_count)
        image_notice = (
            f'<p class="body-image-notice" role="status" data-image-status data-image-total="{image_count}">正在自动加载 {image_count} 张图片</p>'
            if image_count
            else (
                f'<p class="body-image-notice" role="note">本邮件中的 {hidden_images} 张图片无法安全加载</p>'
                if hidden_images > 0
                else ""
            )
        )
        context_inputs = f'''<input type="hidden" name="csrf" value="{html.escape(PROCESS_CSRF_TOKEN, quote=True)}">
<input type="hidden" name="scope" value="message"><input type="hidden" name="account" value="{html.escape(account.name, quote=True)}"><input type="hidden" name="uid" value="{html.escape(uid, quote=True)}"><input type="hidden" name="return_account" value="{html.escape(return_account, quote=True)}"><input type="hidden" name="unread" value="{'1' if params.unread_only else '0'}"><input type="hidden" name="limit" value="{params.limit}"><input type="hidden" name="page" value="{params.requested_page}">'''
        if translation_config is not None:
            action_label = "重新翻译" if translation is not None else "翻译为中文"
            translation_actions = f'''<div class="translation-actions"><form method="post" action="/translation/run" data-translation-submit>{context_inputs}<input type="hidden" name="force" value="{'1' if translation is not None else '0'}"><button class="button primary" type="submit" data-loading-label="正在翻译…">{action_label}</button></form><a class="button" href="/settings#translation">修改 API</a></div>'''
        else:
            open_label = "绑定 API" if translation is not None else "翻译为中文"
            translation_actions = f'''<div class="translation-actions"><button class="button primary" type="button" data-translation-config-open data-settings-href="/settings#translation">{open_label}</button><noscript><a class="button" href="/settings#translation">配置翻译 API</a></noscript></div>'''

        tabs: list[str] = []
        panels: list[str] = []
        default_mode = "translated" if translation is not None else (
            "html" if has_rich_body else "plain"
        )
        if translation is not None:
            tabs.append('<button class="segment body-mode-button" type="button" data-body-mode="translated" aria-pressed="true">中文翻译</button>')
            translated_body = html.escape(
                normalize_plain_text(translation.text)
            ) or "（译文没有可显示的文本正文）"
            if translated_html:
                panels.append(
                    f'<section class="body-panel" data-body-panel="translated" aria-label="中文翻译正文"><iframe class="message-body-frame" title="中文翻译正文" sandbox="allow-same-origin" referrerpolicy="no-referrer" srcdoc="{_mail_body_srcdoc(translated_html)}"></iframe></section>'
                )
            else:
                panels.append(
                    f'<section class="body-panel" data-body-panel="translated" aria-label="中文翻译正文"><div class="body">{translated_body}</div></section>'
                )
        if has_rich_body:
            rich_label = "原文排版" if translation is not None else "排版版"
            tabs.append(
                f'<button class="segment body-mode-button" type="button" data-body-mode="html" aria-pressed="{"true" if default_mode == "html" else "false"}">{rich_label}</button>'
            )
            panels.append(
                f'<section class="body-panel" data-body-panel="html" aria-label="原文排版正文"{"" if default_mode == "html" else " hidden"}><iframe class="message-body-frame" title="邮件原文排版正文" sandbox="allow-same-origin" referrerpolicy="no-referrer" srcdoc="{_mail_body_srcdoc(rendered_html)}"></iframe></section>'
            )
        plain_label = "原文纯文本" if translation is not None else "纯文本"
        tabs.append(
            f'<button class="segment body-mode-button" type="button" data-body-mode="plain" aria-pressed="{"true" if default_mode == "plain" else "false"}">{plain_label}</button>'
        )
        panels.append(
            f'<section class="body-panel" data-body-panel="plain" aria-label="原文纯文本正文"{"" if default_mode == "plain" else " hidden"}><div class="body">{plain_body}</div></section>'
        )
        body_toolbar = f'''<div class="message-body-toolbar">
  <div class="body-view-controls"><span class="field-label">正文</span><div class="segmented body-view-tabs" role="group" aria-label="正文显示方式">{"".join(tabs)}</div></div>
  <div class="translation-toolbar-side">{image_notice}{translation_actions}</div>
</div>'''
        body_content = f'''{body_toolbar}
{"".join(panels)}
<dialog class="external-link-dialog" data-external-link-dialog aria-labelledby="external-link-title">
  <div class="external-link-content"><h2 id="external-link-title">打开外部链接？</h2><p>这个地址将离开本地邮箱查看器。请确认完整地址后再继续。</p><output class="external-link-url" data-external-link-url></output><div class="external-link-actions"><form method="dialog"><button class="button" type="submit" value="cancel">取消</button></form><button class="button primary" type="button" data-open-external-link>确认打开</button></div></div>
</dialog>'''
        translation_dialog = ""
        if translation_config is None:
            translation_dialog = f'''<dialog class="translation-dialog" data-translation-dialog aria-labelledby="translation-dialog-title"><div class="translation-dialog-content"><h2 id="translation-dialog-title">绑定翻译 API</h2><p>选择你自己的翻译服务。邮件主题和正文会发送给该服务，项目不会提供或共享 API Key。</p><form class="translation-config-form" method="post" action="/translation/configure" data-translation-config-form data-translation-submit>{context_inputs}{_translation_config_fields()}<p class="translation-disclosure">只发送当前邮件的主题与可见正文；不发送发件人、收件人、附件、图片、链接地址或邮箱密码。</p><div class="settings-actions"><button class="button" type="button" data-translation-dialog-close>取消</button><button class="button primary" type="submit" data-loading-label="正在保存并翻译…">保存并翻译</button></div></form></div></dialog>'''
        translation_status = ""
        status_value = query.get("translation", [""])[0]
        if status_value in {"done", "saved"}:
            translation_status = '<p class="notice translation-status" role="status">中文译文已生成并缓存。</p>'
        elif status_value == "error":
            reason = query.get("translation_detail", ["翻译服务暂时不可用，请稍后重试。"])[0]
            translation_status = f'<p class="notice warning translation-status" role="alert">翻译失败：{html.escape(reason)}</p>'
        elif status_value == "disconnected":
            translation_status = '<p class="notice translation-status" role="status">翻译 API 已解绑，已有译文仍保留。</p>'
        back_url = listing_url(params.unread_only, params.limit, params.offset, return_account)
        displayed_subject = translation.subject if translation is not None else item.subject
        content = f'''<header class="detail-header">
  <div><h1 class="detail-subject" data-detail-subject data-original-subject="{html.escape(item.subject, quote=True)}" data-translated-subject="{html.escape(translation.subject if translation is not None else '', quote=True)}">{html.escape(displayed_subject)}</h1><p class="read-only-note">{html.escape(account.name)} · {html.escape(account.email)} · 只读查看，不会标为已读</p></div>
  <a class="button" href="{html.escape(back_url, quote=True)}">返回邮件列表</a>
</header>
{translation_status}
<article class="message-shell" data-message-body>
  <dl class="message-meta"><dt>发件人</dt><dd>{html.escape(item.sender)}</dd><dt>收件人</dt><dd>{html.escape(item.recipients)}</dd><dt>时间</dt><dd>{html.escape(item.date)}</dd></dl>
  {attachment_box}
  {body_content}
</article>
{translation_dialog}'''
        self._send(page(displayed_subject, content))

    def _api_messages(self, query: dict[str, list[str]]) -> None:
        params = parse_listing_params(query)
        runtime = self._runtime()
        selection = query.get("account", [None])[0]
        if selection == "all":
            names = tuple(account.name for account in runtime.accounts)
        else:
            account = find_account(selection, runtime.accounts)
            names = (account.name,)
        errors = self._prepare_cache(
            runtime,
            names,
            unread_only=params.unread_only,
            limit=params.limit,
            refresh=False,
        )
        messages = runtime.cache.query_messages(
            names,
            unread_only=params.unread_only,
            limit=params.limit,
            offset=params.offset,
        )
        if selection == "all":
            account_map = {account.name: account for account in runtime.accounts}
            records: list[dict[str, object]] = []
            for item in messages:
                record = asdict(_mail_summary(item))
                record["account"] = account_map[item.account_name].public_record()
                records.append(record)
            payload_value: object = {
                "messages": records,
                "errors": list(errors),
            }
        else:
            payload_value = [asdict(_mail_summary(item)) for item in messages]
        payload = json.dumps(payload_value, ensure_ascii=False, indent=2).encode("utf-8")
        self._send(payload, content_type="application/json; charset=utf-8")

    def _settings(self, query: dict[str, list[str]]) -> None:
        runtime = self._runtime()
        current = runtime.settings
        translation_config = runtime.translation_config
        statuses: list[str] = []
        if query.get("saved") == ["1"]:
            statuses.append('<p class="notice" role="status">缓存设置已保存。</p>')
        elif query.get("cleared") == ["bodies"]:
            statuses.append('<p class="notice" role="status">已清除正文、图片和译文缓存，邮件元数据仍保留。</p>')
        elif query.get("cleared") == ["all"]:
            statuses.append('<p class="notice" role="status">已清除全部邮件缓存，后台将重新建立索引。</p>')
        translation_status = query.get("translation", [""])[0]
        if translation_status == "saved":
            statuses.append('<p class="notice" role="status">翻译 API 已绑定。</p>')
        elif translation_status == "disconnected":
            statuses.append('<p class="notice" role="status">翻译 API 已解绑，已有译文仍保留。</p>')
        elif translation_status == "cleared":
            statuses.append('<p class="notice" role="status">译文缓存已清除。</p>')
        elif translation_status == "error":
            reason = query.get("translation_detail", ["翻译配置无法保存，请检查后重试。"])[0]
            statuses.append(
                f'<p class="notice warning" role="alert">翻译设置失败：{html.escape(reason)}</p>'
            )
        status = "".join(statuses)
        options = "".join(
            f'<option value="{mode}"{" selected" if current.cache_mode == mode else ""}>{label}</option>'
            for mode, label in (
                ("memory", "仅内存（退出即清除）"),
                ("metadata", "仅持久化元数据"),
                ("body", "自动加密缓存正文与图片（默认）"),
            )
        )
        token = html.escape(PROCESS_CSRF_TOKEN, quote=True)
        if translation_config is None:
            provider_status = '<div class="translation-provider-status"><strong>尚未绑定</strong><span class="form-help">首次翻译时也可以直接在邮件详情页绑定。</span></div>'
        else:
            provider_label = TRANSLATION_PROVIDER_LABELS.get(
                translation_config.provider, translation_config.provider
            )
            provider_detail = ""
            if translation_config.provider == "openai_compatible":
                provider_detail = f'<span class="form-help">{html.escape(translation_config.base_url)} · {html.escape(translation_config.model)}</span>'
            provider_status = f'<div class="translation-provider-status"><strong>{html.escape(provider_label)}</strong>{provider_detail}</div>'
        disconnect_action = ""
        if translation_config is not None:
            disconnect_action = f'''<form method="post" action="/translation/disconnect"><input type="hidden" name="csrf" value="{token}"><input type="hidden" name="scope" value="settings"><button class="button" type="submit">解绑 API</button></form>'''
        content = f'''<header class="settings-header"><div><h1>缓存、同步与翻译设置</h1><p class="account">缓存设置由网页与 CLI 共用；翻译配置和密钥保存在本机系统凭据库中。</p></div><a class="button" href="/">返回邮件列表</a></header>
<section class="settings-shell">
  {status}
  <article class="settings-card"><h2>缓存策略</h2><p>邮件列表始终从本地缓存读取；body 和 memory 模式会在进入网页后按日期从新到旧后台缓存正文与正文图片，metadata 模式只保存邮件元数据。远程正文图片可能在邮件尚未打开时向发件方暴露公网 IP 和预取时间。</p>
    <form class="settings-form" method="post" action="/settings">
      <input type="hidden" name="csrf" value="{token}"><input type="hidden" name="action" value="save">
      <div class="form-row"><label class="field-label" for="cache-mode">缓存模式</label><div><select class="select" id="cache-mode" name="cache_mode">{options}</select><span class="form-help">降低模式时，下一步会要求选择是否清除现有缓存。</span></div></div>
      <div class="form-row"><label class="field-label" for="refresh-minutes">自动同步间隔</label><div><input class="number-input" id="refresh-minutes" name="refresh_minutes" type="number" min="0" max="1440" value="{current.refresh_minutes}" required inputmode="numeric"><span class="form-help">分钟；0 表示只在手动刷新或 CLI 读取时同步。</span></div></div>
      <div class="settings-actions"><button class="button primary" type="submit">保存设置</button></div>
    </form>
  </article>
  <article class="settings-card" id="translation"><h2>中文翻译</h2><p>翻译只在你点击时执行，不会随正文预取自动调用。邮件主题和可见正文会发送给所选服务，账户信息、附件、图片、链接地址和邮箱密码不会发送。</p>
    {provider_status}
    <form class="translation-config-form" method="post" action="/translation/configure" data-translation-config-form><input type="hidden" name="csrf" value="{token}"><input type="hidden" name="scope" value="settings">{_translation_config_fields(translation_config)}<p class="translation-disclosure">DeepL 使用固定官方端点；OpenAI 兼容模式可用于 OpenAI、DeepSeek、通义或本机 Ollama。API 账号、额度与费用由你自行承担。</p><div class="settings-actions"><button class="button primary" type="submit">{ '更新 API 配置' if translation_config is not None else '绑定翻译 API' }</button></div></form>
    <div class="settings-inline-actions">{disconnect_action}<form method="post" action="/translation/cache/clear"><input type="hidden" name="csrf" value="{token}"><input type="hidden" name="scope" value="settings"><button class="button" type="submit">只清除译文缓存</button></form></div>
  </article>
  <article class="settings-card danger-zone"><h2>清理缓存</h2><p>清除正文会同时清除图片和译文，但不会影响邮件列表；重建全部缓存会暂时让列表为空，并在后台重新索引。</p>
    <form class="inline-form" method="post" action="/settings"><input type="hidden" name="csrf" value="{token}"><input type="hidden" name="action" value="clear_bodies"><button class="button" type="submit">清除正文缓存</button></form>
    <form class="inline-form" method="post" action="/settings"><input type="hidden" name="csrf" value="{token}"><input type="hidden" name="action" value="clear_all"><button class="button danger" type="submit">重建全部缓存</button></form>
  </article>
  <article class="settings-card"><h2>本机位置</h2><p>{html.escape(str(runtime.cache_file))}</p></article>
</section>'''
        self._send(page("缓存、同步与翻译设置", content))

    def _settings_post(self, form: dict[str, list[str]]) -> None:
        runtime = self._runtime()
        action = form.get("action", [""])[0]
        if action == "clear_bodies":
            runtime.cache.clear_bodies()
            runtime.image_tokens.clear()
            runtime.restart_prefetch()
            self._redirect("/settings?cleared=bodies")
            return
        if action == "clear_all":
            runtime.cache.clear_all()
            runtime.image_tokens.clear()
            runtime.sync.kick_background(DEFAULT_LIMIT)
            runtime.restart_prefetch()
            self._redirect("/settings?cleared=all")
            return
        if action != "save":
            raise ViewerError("未知的设置操作。")
        mode = form.get("cache_mode", [""])[0]
        try:
            refresh_minutes = int(form.get("refresh_minutes", [""])[0])
            requested = CacheSettings(mode, refresh_minutes).validated()
        except ValueError as exc:
            raise ViewerError("缓存模式无效，刷新间隔必须为 0–1440 分钟。") from exc
        existing_cache = form.get("existing_cache", [None])[0]
        if _is_cache_mode_downgrade(runtime.settings.cache_mode, requested.cache_mode) and existing_cache not in {"keep", "purge"}:
            self._settings_confirmation(requested)
            return
        runtime.reconfigure(requested, existing_cache=existing_cache)
        self._redirect("/settings?saved=1")

    def _settings_confirmation(self, requested: CacheSettings) -> None:
        token = html.escape(PROCESS_CSRF_TOKEN, quote=True)
        content = f'''<header class="settings-header"><div><h1>处理现有缓存</h1><p class="account">你正在降低缓存模式，需要明确决定已有数据如何处理。</p></div><a class="button" href="/settings">取消</a></header>
<section class="settings-shell"><article class="settings-card"><h2>保留还是清除？</h2><p>保留后旧缓存仍留在磁盘，但新模式不会继续写入不允许的数据；清除会删除新模式不再允许保存的数据。</p>
  <form class="settings-form" method="post" action="/settings"><input type="hidden" name="csrf" value="{token}"><input type="hidden" name="action" value="save"><input type="hidden" name="cache_mode" value="{html.escape(requested.cache_mode, quote=True)}"><input type="hidden" name="refresh_minutes" value="{requested.refresh_minutes}"><div class="settings-actions"><button class="button" type="submit" name="existing_cache" value="keep">保留旧缓存</button><button class="button danger" type="submit" name="existing_cache" value="purge">清除不再允许的数据</button></div></form>
</article></section>'''
        self._send(page("处理现有缓存", content))

    def _message_image(self, token: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", token):
            self._send_image_error(HTTPStatus.NOT_FOUND)
            return
        try:
            mime_type, data = self._runtime().image_for_token(token)
        except (ViewerError, TimeoutError, ValueError):
            self._send_image_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _send_image_error(self, status: HTTPStatus) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _send(self, body: bytes, status: HTTPStatus = HTTPStatus.OK, *, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; connect-src 'self'; img-src 'self'; frame-src 'self'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'")
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
    runtime = ViewerRuntime(periodic=False)
    try:
        if all_accounts:
            selected_accounts = runtime.accounts
        else:
            selected_accounts = (find_account(account_name, runtime.accounts),)
        names = tuple(account.name for account in selected_accounts)
        errors = _sync_for_cli(runtime, names)
        since_timestamp = (
            time.time() - since_hours * 60 * 60
            if since_hours is not None
            else None
        )
        messages = runtime.cache.query_messages(
            names,
            unread_only=unread,
            limit=limit,
            offset=offset,
            since_timestamp=since_timestamp,
        )
        account_map = {account.name: account for account in runtime.accounts}
        records: list[dict[str, object]] = []
        for item in messages:
            record: dict[str, object] = asdict(_mail_summary(item))
            if all_accounts:
                record["account"] = account_map[item.account_name].public_record()
            if include_text:
                try:
                    detail = runtime.message_detail(item.account_name, item.uid)
                    record["preview"] = detail.text[:1200]
                    record["attachments"] = list(detail.attachments)
                except (ViewerError, RuntimeError) as exc:
                    if all_accounts:
                        record["error"] = str(exc)
                    else:
                        raise ViewerError(str(exc)) from exc
            records.append(record)
        if all_accounts:
            output: object = {"messages": records, "errors": list(errors)}
        else:
            if errors:
                if not runtime.cache.account_message_count(names[0]):
                    raise ViewerError(errors[0]["error"])
                print(
                    f"警告：账户 {names[0]} 同步失败，正在返回缓存数据：{errors[0]['error']}",
                    file=sys.stderr,
                )
            output = records
        print(json.dumps(output, ensure_ascii=False, indent=2))
    finally:
        runtime.close()


def _sync_for_cli(
    runtime: ViewerRuntime,
    account_names: tuple[str, ...],
) -> tuple[dict[str, str], ...]:
    incomplete = tuple(
        name
        for name in account_names
        if not runtime.cache.sync_state(name).full_sync_complete
    )
    complete = tuple(name for name in account_names if name not in incomplete)
    errors: list[dict[str, str]] = []
    if incomplete:
        errors.extend(
            runtime.sync.sync_accounts(
                incomplete, wait=True, timeout=None, force=True
            )
        )
    if complete:
        errors.extend(
            runtime.sync.sync_accounts(
                complete,
                wait=True,
                timeout=None,
                force=runtime.settings.refresh_minutes == 0,
            )
        )
    errors.extend(cached_errors(runtime, account_names))
    deduplicated: dict[str, dict[str, str]] = {}
    for error in errors:
        deduplicated[str(error["account"])] = error
    return tuple(deduplicated.values())


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

    settings_parser = sub.add_parser("settings", help="查看或修改缓存与同步设置")
    settings_parser.add_argument("--cache-mode", choices=CACHE_MODES, help="memory、metadata 或 body")
    settings_parser.add_argument("--refresh-minutes", type=int, help="自动同步间隔：0 或 1–1440 分钟")
    settings_parser.add_argument(
        "--existing-cache",
        choices=("keep", "purge"),
        help="降低缓存模式时保留或清除旧缓存",
    )

    cache_parser = sub.add_parser("cache", help="清理本地邮件缓存")
    cache_sub = cache_parser.add_subparsers(dest="cache_command", required=True)
    cache_clear = cache_sub.add_parser("clear", help="清除正文或全部缓存")
    clear_mode = cache_clear.add_mutually_exclusive_group(required=True)
    clear_mode.add_argument("--bodies", action="store_true", help="只清除正文缓存")
    clear_mode.add_argument("--all", action="store_true", help="清除全部邮件缓存")

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
            runtime = ViewerRuntime(periodic=False)
            try:
                account = find_account(args.account, runtime.accounts)
                errors = _sync_for_cli(runtime, (account.name,))
                if errors and runtime.cache.message(account.name, args.uid) is not None:
                    print(
                        f"警告：账户 {account.name} 同步失败，正在尝试读取缓存：{errors[0]['error']}",
                        file=sys.stderr,
                    )
                print(
                    json.dumps(
                        _mail_detail_record(
                            runtime.message_detail(account.name, args.uid)
                        ),
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            finally:
                runtime.close()
        elif args.command == "settings":
            current = load_settings()
            if args.cache_mode is None and args.refresh_minutes is None:
                if args.existing_cache is not None:
                    raise ViewerError("--existing-cache 只能在修改设置时使用。")
                print(json.dumps(current.public_record(), ensure_ascii=False, indent=2))
            else:
                requested = CacheSettings(
                    args.cache_mode or current.cache_mode,
                    current.refresh_minutes
                    if args.refresh_minutes is None
                    else args.refresh_minutes,
                )
                try:
                    requested = requested.validated()
                except ValueError as exc:
                    raise ViewerError("刷新间隔必须为 0–1440 分钟。") from exc
                if _is_cache_mode_downgrade(current.cache_mode, requested.cache_mode) and args.existing_cache is None:
                    raise ViewerError("降低缓存模式时必须附加 --existing-cache keep|purge。")
                runtime = ViewerRuntime(periodic=False, settings=current)
                try:
                    runtime.reconfigure(
                        requested, existing_cache=args.existing_cache
                    )
                finally:
                    runtime.close()
                print(json.dumps(requested.public_record(), ensure_ascii=False, indent=2))
        elif args.command == "cache":
            runtime = ViewerRuntime(periodic=False)
            try:
                if args.bodies:
                    runtime.cache.clear_bodies()
                    print("已清除正文缓存。")
                else:
                    runtime.cache.clear_all()
                    print("已清除全部邮件缓存。")
            finally:
                runtime.close()
        elif args.command == "serve":
            if not 1024 <= args.port <= 65535:
                raise ViewerError("端口必须在 1024–65535 之间。")
            if not load_accounts():
                raise ViewerError("尚未配置邮箱。请先运行 configure。")
            runtime = ViewerRuntime(periodic=True)
            try:
                server = ViewerHTTPServer(("127.0.0.1", args.port), runtime)
            except OSError as exc:
                runtime.close()
                raise ViewerError(f"无法启动本机网页服务：{exc}") from exc
            print(f"{APP_TITLE}已启动：http://127.0.0.1:{args.port}")
            print("按 Control-C 停止。")
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print("\n已停止。")
            finally:
                server.server_close()
                runtime.close()
        return 0
    except ViewerError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
