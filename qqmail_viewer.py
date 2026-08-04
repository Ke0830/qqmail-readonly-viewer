#!/usr/bin/env python3
"""Local, read-only QQ Mail viewer.

Credentials are stored in the macOS login keychain. Mail is fetched over IMAPS
and BODY.PEEK is used so viewing a message does not mark it as read.
"""

from __future__ import annotations

import argparse
import email
import getpass
import html
import imaplib
import json
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from email.header import Header, decode_header
from email.message import Message
from datetime import timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterable
from urllib.parse import parse_qs, urlparse


IMAP_HOST = "imap.qq.com"
IMAP_PORT = 993
KEYCHAIN_ACCOUNT = "qqmail-viewer"
EMAIL_SERVICE = "codex.qqmail-viewer.email"
AUTH_SERVICE = "codex.qqmail-viewer.authorization-code"
DEFAULT_LIMIT = 30
MAX_LIMIT = 100
CHINA_TIMEZONE = timezone(timedelta(hours=8))


class ViewerError(RuntimeError):
    """An expected, user-facing error."""


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
        raise ViewerError("未找到 macOS 钥匙串工具 security。此查看器目前仅支持 macOS。") from exc
    if result.returncode != 0:
        reason = result.stderr.strip() or "钥匙串操作失败"
        raise ViewerError(reason)
    return result.stdout.strip()


def keychain_get(service: str) -> str:
    try:
        return _security(["find-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", service, "-w"])
    except ViewerError as exc:
        raise ViewerError("尚未配置 QQ 邮箱。请先运行 configure。") from exc


def keychain_set(service: str, value: str) -> None:
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


class QQMailClient:
    def __init__(self, address: str, authorization_code: str) -> None:
        self.address = address
        self.authorization_code = authorization_code
        self.connection: imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> "QQMailClient":
        try:
            self.connection = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=30)
            self.connection.login(self.address, self.authorization_code)
            status, _ = self.connection.select("INBOX", readonly=True)
            if status != "OK":
                raise ViewerError("无法以只读方式打开收件箱。")
            return self
        except imaplib.IMAP4.error as exc:
            self.close()
            raise ViewerError("QQ 邮箱登录失败。请确认已开启 IMAP，并使用授权码而不是 QQ 密码。") from exc
        except OSError as exc:
            self.close()
            raise ViewerError(f"无法连接 {IMAP_HOST}:{IMAP_PORT}：{exc}") from exc

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

    def list_messages(
        self,
        *,
        unread_only: bool,
        limit: int | None,
        offset: int = 0,
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
        end = offset + limit if limit is not None else None
        return [item[2] for item in dated_messages[offset:end]]

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


def configured_client() -> QQMailClient:
    return QQMailClient(keychain_get(EMAIL_SERVICE), keychain_get(AUTH_SERVICE))


def configure(address: str) -> None:
    address = address.strip().lower()
    if not re.fullmatch(r"[^@\s]+@(qq\.com|foxmail\.com)", address):
        raise ViewerError("请输入完整的 @qq.com 或 @foxmail.com 邮箱地址。")
    code = getpass.getpass("QQ 邮箱授权码（输入不会显示）：").strip().replace(" ", "")
    if not code:
        raise ViewerError("授权码不能为空。")
    print("正在测试只读 IMAP 连接……")
    with QQMailClient(address, code):
        pass
    keychain_set(EMAIL_SERVICE, address)
    keychain_set(AUTH_SERVICE, code)
    print("配置成功。授权码已保存在 macOS 钥匙串中。")


BASE_STYLE = """
:root{color-scheme:light dark;--bg:#f4f7fb;--card:#fff;--ink:#172033;--muted:#687386;--line:#dce3ed;--accent:#1769e0}
@media(prefers-color-scheme:dark){:root{--bg:#10141b;--card:#171d27;--ink:#edf3ff;--muted:#9aa7ba;--line:#2b3545;--accent:#72a7ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:980px;margin:0 auto;padding:34px 22px 70px}header{display:flex;justify-content:space-between;align-items:end;gap:18px;margin-bottom:22px}
h1{font-size:28px;margin:0}h2{font-size:21px;margin:0 0 12px}.muted{color:var(--muted)}.card{background:var(--card);border:1px solid var(--line);border-radius:16px;overflow:hidden;box-shadow:0 8px 28px rgba(20,34,58,.06)}
.toolbar{display:flex;gap:9px;flex-wrap:wrap}.button{display:inline-block;padding:8px 12px;border-radius:9px;border:1px solid var(--line);color:var(--ink);text-decoration:none;background:var(--card)}.button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.mail{display:grid;grid-template-columns:minmax(220px,1.2fr) minmax(0,2fr) auto;gap:16px;padding:15px 18px;border-top:1px solid var(--line);text-decoration:none;color:inherit;align-items:center}.mail:first-child{border-top:0}.mail:hover{background:color-mix(in srgb,var(--accent) 7%,var(--card))}.subject{font-weight:650;overflow-wrap:anywhere}.sender{color:var(--muted);overflow-wrap:anywhere}.date{color:var(--muted);white-space:nowrap}
.message{padding:24px}.meta{display:grid;grid-template-columns:76px 1fr;gap:6px 12px;padding-bottom:20px;border-bottom:1px solid var(--line);margin-bottom:20px}.body{white-space:pre-wrap;overflow-wrap:anywhere;font:15px/1.7 ui-monospace,SFMono-Regular,Menlo,monospace}.attachments{padding:12px 14px;background:var(--bg);border-radius:10px;margin:14px 0}.empty{padding:55px 20px;text-align:center;color:var(--muted)}
@media(max-width:700px){header{display:block}.toolbar{margin-top:14px}.mail{grid-template-columns:1fr}.date{font-size:13px}}
"""


def page(title: str, content: str) -> bytes:
    document = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>{html.escape(title)}</title><style>{BASE_STYLE}</style></head><body><main>{content}</main></body></html>"""
    return document.encode("utf-8")


class ViewerHandler(BaseHTTPRequestHandler):
    server_version = "QQMailViewer/1.0"

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
                self._send(page("未找到", '<div class="card empty">页面不存在。</div>'), HTTPStatus.NOT_FOUND)
        except ViewerError as exc:
            content = f'<header><h1>QQ 邮箱查看器</h1></header><div class="card empty">{html.escape(str(exc))}</div>'
            self._send(page("发生错误", content), HTTPStatus.BAD_GATEWAY)

    def _params(self, query: dict[str, list[str]]) -> tuple[bool, int, int]:
        unread = query.get("unread", ["1"])[0] != "0"
        try:
            limit = min(max(int(query.get("limit", [str(DEFAULT_LIMIT)])[0]), 1), MAX_LIMIT)
        except ValueError:
            limit = DEFAULT_LIMIT
        try:
            offset = max(int(query.get("offset", ["0"])[0]), 0)
        except ValueError:
            offset = 0
        return unread, limit, offset

    def _home(self, query: dict[str, list[str]]) -> None:
        unread, limit, offset = self._params(query)
        address = keychain_get(EMAIL_SERVICE)
        with configured_client() as client:
            messages = client.list_messages(unread_only=unread, limit=limit, offset=offset)
        rows = []
        for item in messages:
            rows.append(
                f'<a class="mail" href="/message?uid={item.uid}"><span class="sender">{html.escape(item.sender)}</span><span class="subject">{html.escape(item.subject)}</span><time class="date">{html.escape(item.date)}</time></a>'
            )
        listing = "".join(rows) if rows else '<div class="empty">这里暂时没有符合条件的邮件。</div>'
        opposite = "0" if unread else "1"
        label = "查看全部" if unread else "仅看未读"
        mode_value = "1" if unread else "0"
        previous = ""
        if offset > 0:
            previous_offset = max(0, offset - limit)
            previous = f'<a class="button" href="/?unread={mode_value}&limit={limit}&offset={previous_offset}">上一页</a>'
        next_link = ""
        if len(messages) == limit:
            next_link = f'<a class="button" href="/?unread={mode_value}&limit={limit}&offset={offset + limit}">下一页</a>'
        range_text = f"第 {offset + 1}–{offset + len(messages)} 封" if messages else "没有邮件"
        content = f"""<header><div><h1>QQ 邮箱查看器</h1><div class="muted">{html.escape(address)} · 按日期倒序 · {range_text}</div></div><nav class="toolbar"><a class="button" href="/?unread={opposite}&limit={limit}&offset=0">{label}</a><a class="button" href="/?unread={mode_value}&limit=100&offset=0">每页 100 封</a>{previous}{next_link}<a class="button primary" href="/?unread={mode_value}&limit={limit}&offset={offset}">刷新</a></nav></header><section class="card">{listing}</section>"""
        self._send(page("QQ 邮箱查看器", content))

    def _message(self, query: dict[str, list[str]]) -> None:
        uid = query.get("uid", [""])[0]
        with configured_client() as client:
            item = client.get_message(uid)
        attachment_box = ""
        if item.attachments:
            names = "、".join(html.escape(name) for name in item.attachments)
            attachment_box = f'<div class="attachments">附件（仅列出，不下载）：{names}</div>'
        content = f"""<header><div><h1>{html.escape(item.subject)}</h1><div class="muted">只读查看，不会标为已读</div></div><a class="button" href="/">返回</a></header><article class="card message"><div class="meta"><strong>发件人</strong><span>{html.escape(item.sender)}</span><strong>收件人</strong><span>{html.escape(item.recipients)}</span><strong>时间</strong><span>{html.escape(item.date)}</span></div>{attachment_box}<div class="body">{html.escape(item.text) or '（邮件没有可显示的文本正文）'}</div></article>"""
        self._send(page(item.subject, content))

    def _api_messages(self, query: dict[str, list[str]]) -> None:
        unread, limit, offset = self._params(query)
        with configured_client() as client:
            messages = client.list_messages(unread_only=unread, limit=limit, offset=offset)
        payload = json.dumps([asdict(item) for item in messages], ensure_ascii=False, indent=2).encode("utf-8")
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
) -> None:
    with configured_client() as client:
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
    parser = argparse.ArgumentParser(description="本地只读 QQ 邮箱查看器")
    sub = parser.add_subparsers(dest="command", required=True)

    configure_parser = sub.add_parser("configure", help="测试连接并把授权信息保存到 macOS 钥匙串")
    configure_parser.add_argument("--email", required=True, help="完整的 QQ 或 Foxmail 邮箱地址")

    list_parser = sub.add_parser("list", help="以 JSON 列出邮件，供人工或定时任务读取")
    mode = list_parser.add_mutually_exclusive_group()
    mode.add_argument("--unread", action="store_true", default=True, help="仅列出未读邮件（默认）")
    mode.add_argument("--all", action="store_true", help="列出最近的全部邮件")
    list_parser.add_argument("--limit", type=int, default=20, help="单页最多返回多少封，不设上限")
    list_parser.add_argument("--offset", type=int, default=0, help="跳过前 N 封，配合 --limit 分页")
    list_parser.add_argument("--all-pages", action="store_true", help="返回所有符合条件的邮件，不分页、不设上限")
    list_parser.add_argument("--since-hours", type=float, help="仅返回最近 N 小时内收到的邮件")
    list_parser.add_argument("--include-text", action="store_true", help="附带每封邮件前 1200 字正文预览")

    show_parser = sub.add_parser("show", help="以 JSON 读取一封邮件的完整文本正文")
    show_parser.add_argument("uid", help="邮件 UID，可从 list 输出获得")

    serve_parser = sub.add_parser("serve", help="启动仅限本机访问的网页查看器")
    serve_parser.add_argument("--port", type=int, default=8765, help="本机端口（默认 8765）")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "configure":
            configure(args.email)
        elif args.command == "list":
            if args.limit <= 0:
                raise ViewerError("--limit 必须大于 0。")
            if args.offset < 0:
                raise ViewerError("--offset 不能小于 0。")
            if args.since_hours is not None and args.since_hours <= 0:
                raise ViewerError("--since-hours 必须大于 0。")
            limit = None if args.all_pages else args.limit
            list_cli(not args.all, limit, args.include_text, args.since_hours, args.offset)
        elif args.command == "show":
            with configured_client() as client:
                print(json.dumps(asdict(client.get_message(args.uid)), ensure_ascii=False, indent=2))
        elif args.command == "serve":
            if not 1024 <= args.port <= 65535:
                raise ViewerError("端口必须在 1024–65535 之间。")
            keychain_get(EMAIL_SERVICE)
            server = ThreadingHTTPServer(("127.0.0.1", args.port), ViewerHandler)
            print(f"QQ 邮箱查看器已启动：http://127.0.0.1:{args.port}")
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
