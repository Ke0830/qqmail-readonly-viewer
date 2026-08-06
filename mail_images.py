"""Validate and retrieve email images without exposing the browser to remote URLs."""

from __future__ import annotations

import http.client
import io
import ipaddress
import socket
import ssl
import threading
import time
import unicodedata
import warnings
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Protocol
from urllib.parse import quote, urljoin, urlsplit

from PIL import Image, UnidentifiedImageError


MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
MAX_IMAGE_EDGE = 12_000
MAX_ANIMATION_FRAMES = 100
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_TOTAL_TIMEOUT = 15.0
DEFAULT_MAX_REDIRECTS = 3
DEFAULT_MAX_CONCURRENCY = 4
_READ_CHUNK_SIZE = 64 * 1024
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_PROXY_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
_FORMAT_MIME_TYPES = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}
_MIME_ALIASES = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "image/x-png": "image/png",
}


class MailImageError(ValueError):
    """Base class for safely handled image failures."""


class ImageValidationError(MailImageError):
    """Raised when bytes are not a supported, valid raster image."""


class ImageLimitError(ImageValidationError):
    """Raised when an image exceeds a configured resource limit."""


class RemoteImageError(MailImageError):
    """Raised when a remote image cannot be retrieved safely."""


class RemoteImageSecurityError(RemoteImageError):
    """Raised when a URL or resolved address violates the network policy."""


class RemoteImageTimeoutError(RemoteImageError):
    """Raised when the overall remote-image deadline expires."""


class RemoteImageResponseError(RemoteImageError):
    """Raised when a remote server returns an unusable response."""


@dataclass(frozen=True)
class ValidatedImage:
    data: bytes
    mime_type: str
    width: int
    height: int
    frame_count: int = 1

    @property
    def dimensions(self) -> tuple[int, int]:
        return self.width, self.height


@dataclass(frozen=True)
class RemoteImage:
    data: bytes
    mime_type: str
    width: int
    height: int
    frame_count: int
    final_url: str
    redirect_count: int

    @property
    def dimensions(self) -> tuple[int, int]:
        return self.width, self.height


@dataclass(frozen=True)
class RemoteImageRequest:
    """One request pinned to an already validated IP address."""

    url: str
    scheme: str
    hostname: str
    port: int
    ip_address: str
    target: str
    headers: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class RemoteImageResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes = b""


class Resolver(Protocol):
    def __call__(self, hostname: str, port: int) -> Iterable[str]: ...


class Transport(Protocol):
    def __call__(
        self,
        request: RemoteImageRequest,
        *,
        max_bytes: int,
        connect_timeout: float,
        deadline: float,
    ) -> RemoteImageResponse: ...


@dataclass(frozen=True)
class _ParsedUrl:
    url: str
    scheme: str
    hostname: str
    port: int
    target: str
    host_header: str


def validate_image(
    data: bytes | bytearray | memoryview,
    declared_content_type: str = "",
) -> ValidatedImage:
    """Return validated raster bytes and their canonical MIME metadata."""

    try:
        payload = bytes(data)
    except (TypeError, ValueError) as exc:
        raise ImageValidationError("image data must be bytes") from exc
    if not payload:
        raise ImageValidationError("image data is empty")
    if len(payload) > MAX_IMAGE_BYTES:
        raise ImageLimitError("image exceeds the 8 MiB limit")

    prefix = payload[:1024].lstrip().lower()
    if b"<svg" in prefix or prefix.startswith(b"<?xml"):
        raise ImageValidationError("SVG images are not supported")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload)) as image:
                image_format = str(image.format or "").upper()
                mime_type = _FORMAT_MIME_TYPES.get(image_format)
                if mime_type is None:
                    raise ImageValidationError("image format is not supported")
                width, height = image.size
                frame_count = int(getattr(image, "n_frames", 1) or 1)
                _validate_dimensions(width, height, frame_count)
                image.verify()
    except (ImageValidationError, ImageLimitError):
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
    ) as exc:
        raise ImageValidationError("image data is malformed or unsupported") from exc

    declared = _normalize_content_type(declared_content_type)
    if declared and _MIME_ALIASES.get(declared, declared) != mime_type:
        raise ImageValidationError("declared content type does not match image bytes")
    return ValidatedImage(
        data=payload,
        mime_type=mime_type,
        width=width,
        height=height,
        frame_count=frame_count,
    )


def _validate_dimensions(width: int, height: int, frame_count: int) -> None:
    if width <= 0 or height <= 0:
        raise ImageValidationError("image dimensions are invalid")
    if width > MAX_IMAGE_EDGE or height > MAX_IMAGE_EDGE:
        raise ImageLimitError("image edge exceeds 12000 pixels")
    if width * height > MAX_IMAGE_PIXELS:
        raise ImageLimitError("image exceeds 25 megapixels")
    if frame_count > MAX_ANIMATION_FRAMES:
        raise ImageLimitError("animation exceeds 100 frames")


def _normalize_content_type(value: str) -> str:
    if not value:
        return ""
    media_type = value.split(";", 1)[0].strip().lower()
    if not media_type or any(character.isspace() for character in media_type):
        raise ImageValidationError("declared content type is invalid")
    return media_type


class RemoteImageFetcher:
    """Fetch images through a direct, IP-pinned HTTP transport."""

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        transport: Transport | None = None,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        if connect_timeout <= 0 or total_timeout <= 0:
            raise ValueError("timeouts must be positive")
        if max_redirects < 0:
            raise ValueError("max_redirects cannot be negative")
        self.resolver = resolver or resolve_host
        self.transport = transport or default_transport
        self.connect_timeout = connect_timeout
        self.total_timeout = total_timeout
        self.max_redirects = max_redirects
        self._semaphore = threading.BoundedSemaphore(max_concurrency)

    def fetch(self, url: str) -> RemoteImage:
        deadline = time.monotonic() + self.total_timeout
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._semaphore.acquire(timeout=remaining):
            raise RemoteImageTimeoutError("remote image fetch timed out")
        try:
            return self._fetch(url, deadline)
        finally:
            self._semaphore.release()

    def _fetch(self, url: str, deadline: float) -> RemoteImage:
        current = _parse_remote_url(url)
        redirect_count = 0
        while True:
            response = self._request(current, deadline)
            if response.status in _REDIRECT_STATUSES:
                if redirect_count >= self.max_redirects:
                    raise RemoteImageResponseError("remote image redirected too many times")
                location = _header(response.headers, "location")
                if not location:
                    raise RemoteImageResponseError("redirect response has no location")
                redirected = _parse_remote_url(urljoin(current.url, location))
                if current.scheme == "https" and redirected.scheme != "https":
                    raise RemoteImageSecurityError("HTTPS redirects cannot downgrade to HTTP")
                current = redirected
                redirect_count += 1
                continue
            if response.status != 200:
                raise RemoteImageResponseError(
                    f"remote image returned HTTP {response.status}"
                )

            content_encoding = _header(response.headers, "content-encoding").lower()
            if content_encoding not in {"", "identity"}:
                raise RemoteImageResponseError("encoded image responses are not supported")
            content_type = _header(response.headers, "content-type")
            if not content_type:
                raise RemoteImageResponseError("remote image has no Content-Type")
            content_length = _content_length(response.headers)
            if content_length is not None and content_length > MAX_IMAGE_BYTES:
                raise ImageLimitError("image exceeds the 8 MiB limit")
            if len(response.body) > MAX_IMAGE_BYTES:
                raise ImageLimitError("image exceeds the 8 MiB limit")
            validated = validate_image(response.body, content_type)
            return RemoteImage(
                data=validated.data,
                mime_type=validated.mime_type,
                width=validated.width,
                height=validated.height,
                frame_count=validated.frame_count,
                final_url=current.url,
                redirect_count=redirect_count,
            )

    def _request(
        self,
        target: _ParsedUrl,
        deadline: float,
    ) -> RemoteImageResponse:
        addresses = _resolve_public_addresses(target.hostname, target.port, self.resolver)
        last_error: BaseException | None = None
        for address in addresses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RemoteImageTimeoutError("remote image fetch timed out")
            request = RemoteImageRequest(
                url=target.url,
                scheme=target.scheme,
                hostname=target.hostname,
                port=target.port,
                ip_address=address,
                target=target.target,
                headers=(
                    ("Accept", "image/jpeg, image/png, image/gif, image/webp"),
                    ("Accept-Encoding", "identity"),
                    ("Connection", "close"),
                    ("Host", target.host_header),
                    ("User-Agent", "local-readonly-mail-viewer/1"),
                ),
            )
            try:
                response = self.transport(
                    request,
                    max_bytes=MAX_IMAGE_BYTES,
                    connect_timeout=min(self.connect_timeout, remaining),
                    deadline=deadline,
                )
            except RemoteImageError:
                raise
            except (TimeoutError, socket.timeout) as exc:
                last_error = exc
                continue
            except OSError as exc:
                last_error = exc
                continue
            if not isinstance(response, RemoteImageResponse):
                raise RemoteImageResponseError("transport returned an invalid response")
            return response
        if isinstance(last_error, (TimeoutError, socket.timeout)):
            raise RemoteImageTimeoutError("remote image fetch timed out") from last_error
        raise RemoteImageError("remote image connection failed") from last_error


def fetch_remote_image(
    url: str,
    *,
    resolver: Resolver | None = None,
    transport: Transport | None = None,
) -> RemoteImage:
    """Fetch one remote image using the default security limits."""

    if resolver is None and transport is None:
        return _DEFAULT_FETCHER.fetch(url)
    return RemoteImageFetcher(resolver=resolver, transport=transport).fetch(url)


def resolve_host(hostname: str, port: int) -> tuple[str, ...]:
    """Resolve a host to concrete addresses without performing a connection."""

    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            rows = socket.getaddrinfo(
                hostname,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise RemoteImageError("remote image host could not be resolved") from exc
        addresses: list[str] = []
        for row in rows:
            address = str(row[4][0])
            if address not in addresses:
                addresses.append(address)
        return tuple(addresses)
    return (str(literal),)


def _resolve_public_addresses(
    hostname: str,
    port: int,
    resolver: Callable[[str, int], Iterable[str]],
) -> tuple[str, ...]:
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        hostname_is_literal = False
    else:
        hostname_is_literal = True
    try:
        raw_addresses = tuple(resolver(hostname, port))
    except RemoteImageError:
        raise
    except Exception as exc:
        raise RemoteImageError("remote image host could not be resolved") from exc
    if not raw_addresses:
        raise RemoteImageError("remote image host has no addresses")

    addresses: list[str] = []
    for raw_address in raw_addresses:
        try:
            address = ipaddress.ip_address(str(raw_address))
        except ValueError as exc:
            raise RemoteImageSecurityError("resolver returned an invalid IP address") from exc
        proxy_fake_ip = (
            not hostname_is_literal
            and isinstance(address, ipaddress.IPv4Address)
            and address in _PROXY_FAKE_IP_NETWORK
        )
        if not _is_public_address(address) and not proxy_fake_ip:
            raise RemoteImageSecurityError("remote image resolved to a non-public address")
        canonical = str(address)
        if canonical not in addresses:
            addresses.append(canonical)
    return tuple(addresses)


def _is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        address.is_global
        and not address.is_loopback
        and not address.is_private
        and not address.is_link_local
        and not address.is_reserved
        and not address.is_multicast
        and not address.is_unspecified
        and not getattr(address, "is_site_local", False)
    )


def _parse_remote_url(url: str) -> _ParsedUrl:
    if not isinstance(url, str) or not url:
        raise RemoteImageSecurityError("remote image URL is missing")
    if "\\" in url or "#" in url:
        raise RemoteImageSecurityError("remote image URL contains unsafe syntax")
    if any(unicodedata.category(character).startswith("C") for character in url):
        raise RemoteImageSecurityError("remote image URL contains control characters")
    try:
        split = urlsplit(url)
    except ValueError as exc:
        raise RemoteImageSecurityError("remote image URL is invalid") from exc
    scheme = split.scheme.lower()
    if scheme not in {"http", "https"}:
        raise RemoteImageSecurityError("remote images must use HTTP or HTTPS")
    if split.username is not None or split.password is not None:
        raise RemoteImageSecurityError("remote image URLs cannot contain credentials")
    if split.fragment:
        raise RemoteImageSecurityError("remote image URLs cannot contain fragments")
    if not split.hostname:
        raise RemoteImageSecurityError("remote image URL has no host")

    hostname = unicodedata.normalize("NFKC", split.hostname).rstrip(".")
    if not hostname or "%" in hostname:
        raise RemoteImageSecurityError("remote image host is invalid")
    if any(unicodedata.category(character).startswith("C") for character in hostname):
        raise RemoteImageSecurityError("remote image host contains hidden characters")
    try:
        hostname = hostname.encode("idna").decode("ascii").lower()
        port = split.port
    except (UnicodeError, ValueError) as exc:
        raise RemoteImageSecurityError("remote image host or port is invalid") from exc
    expected_port = 443 if scheme == "https" else 80
    if port is not None and port != expected_port:
        raise RemoteImageSecurityError("remote images may use only default ports")
    port = expected_port

    path = quote(split.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(split.query, safe="=&?/:@!$'()*+,;%-._~")
    target = path + (f"?{query}" if query else "")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        host_header = hostname
    else:
        host_header = f"[{hostname}]" if literal.version == 6 else hostname
    canonical_url = f"{scheme}://{host_header}{target}"
    return _ParsedUrl(
        url=canonical_url,
        scheme=scheme,
        hostname=hostname,
        port=port,
        target=target,
        host_header=host_header,
    )


def _header(headers: Mapping[str, str], name: str) -> str:
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value).strip()
    return ""


def _content_length(headers: Mapping[str, str]) -> int | None:
    value = _header(headers, "content-length")
    if not value:
        return None
    try:
        length = int(value, 10)
    except ValueError as exc:
        raise RemoteImageResponseError("remote image has an invalid Content-Length") from exc
    if length < 0:
        raise RemoteImageResponseError("remote image has an invalid Content-Length")
    return length


class _PinnedHttpsConnection(http.client.HTTPConnection):
    def __init__(
        self,
        ip_address: str,
        port: int,
        hostname: str,
        timeout: float,
    ) -> None:
        super().__init__(ip_address, port=port, timeout=timeout)
        self._tls_hostname = hostname

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self.host, self.port),
            self.timeout,
            self.source_address,
        )
        try:
            self.sock = _TLS_CONTEXT.wrap_socket(
                raw_socket,
                server_hostname=self._tls_hostname,
            )
        except BaseException:
            raw_socket.close()
            raise


def default_transport(
    request: RemoteImageRequest,
    *,
    max_bytes: int,
    connect_timeout: float,
    deadline: float,
) -> RemoteImageResponse:
    """Perform one direct HTTP request to the request's pinned address."""

    connection: http.client.HTTPConnection
    if request.scheme == "https":
        connection = _PinnedHttpsConnection(
            request.ip_address,
            request.port,
            request.hostname,
            connect_timeout,
        )
    else:
        connection = http.client.HTTPConnection(
            request.ip_address,
            request.port,
            timeout=connect_timeout,
        )
    try:
        _ensure_time(deadline)
        connection.connect()
        _set_connection_timeout(connection, deadline)
        connection.request("GET", request.target, headers=dict(request.headers))
        _set_connection_timeout(connection, deadline)
        response = connection.getresponse()
        headers = {key: value for key, value in response.getheaders()}
        if response.status != 200:
            return RemoteImageResponse(status=response.status, headers=headers)

        content_length = _content_length(headers)
        if content_length is not None and content_length > max_bytes:
            raise ImageLimitError("image exceeds the 8 MiB limit")
        body = bytearray()
        while True:
            _set_connection_timeout(connection, deadline)
            chunk = response.read(min(_READ_CHUNK_SIZE, max_bytes + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > max_bytes:
                raise ImageLimitError("image exceeds the 8 MiB limit")
        return RemoteImageResponse(
            status=response.status,
            headers=headers,
            body=bytes(body),
        )
    except socket.timeout as exc:
        raise RemoteImageTimeoutError("remote image fetch timed out") from exc
    finally:
        connection.close()


def _set_connection_timeout(
    connection: http.client.HTTPConnection,
    deadline: float,
) -> None:
    remaining = _ensure_time(deadline)
    if connection.sock is not None:
        connection.sock.settimeout(min(DEFAULT_CONNECT_TIMEOUT, remaining))


def _ensure_time(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RemoteImageTimeoutError("remote image fetch timed out")
    return remaining


_TLS_CONTEXT = ssl.create_default_context()
_DEFAULT_FETCHER = RemoteImageFetcher()
