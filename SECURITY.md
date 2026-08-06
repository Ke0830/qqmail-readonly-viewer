# Security Policy

## Supported version

Only the latest version on the `main` branch receives security fixes.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting feature when it is available for this repository. Do not open a public Issue containing:

- email passwords, QQ / NetEase authorization codes, or Apple / Google app-specific passwords;
- real email addresses, message bodies, headers, or attachments;
- macOS Keychain or Windows Credential Manager output and other credentials.
- DeepL, OpenAI-compatible or other translation API keys.

If private reporting is unavailable, open a public Issue containing only a minimal, redacted description and ask the maintainer for a private contact channel.

## Credential and cache-key storage

The viewer stores each configured account's address and authorization code or app-specific password in the current user's operating-system credential store: the login Keychain on macOS or Credential Manager on Windows. The account index contains only account names, service-provider settings and credential-service identifiers; it never contains the credential value. On Windows, the application rejects overridden `keyring` backends so that it does not silently fall back to a file-based or third-party store.

The viewer never intentionally writes credentials to the repository. Users should revoke the affected authorization code or app-specific password with its email provider immediately if they suspect it has been exposed.

Cache settings, translation-provider configuration, translation API keys, and the random 256-bit body-encryption key use separate system credential records. Translation settings exposed to the web page contain only the provider, Base URL and model name; the API key cannot be read back through the page. The cache key is never stored in SQLite. If the key is missing or invalid, a new key is generated and unreadable cached bodies are discarded on access; account and translation credentials are not changed.

## Local cache boundary

The viewer stores mailbox metadata in SQLite so lists, filters and pagination do not need to rescan IMAP. Metadata includes account name, UID, subject, sender, recipient, message dates, size, unread state and attachment names. This metadata is not encrypted by the application and should be protected as local user data.

In the default `body` mode, entering the web view starts a newest-first background prefetch of every indexed message body and its approved body images, regardless of read state. The versioned encrypted record can contain the plain-text representation and strictly sanitized HTML, but never the original HTML. Bodies, approved images and user-requested translations are encrypted with AES-GCM and authenticated with the account name and UID. A translation is also bound to its target language and a digest of the original subject, text, sanitized HTML and sanitization policy, so changed source content invalidates it. `metadata` mode never persists or prefetches bodies; its translations remain in process memory. `memory` mode performs the same prefetch into an in-process SQLite database and creates no cache file. The persistent database uses WAL mode and is stored under the current user's local cache directory.

Changing to a lower cache mode requires an explicit keep-or-purge decision. Users can clear only encrypted bodies or rebuild the entire cache from the web settings page or CLI.

## Read-only connection boundary

All built-in providers and custom accounts use TLS-protected IMAPS with certificate and hostname verification. Custom accounts cannot opt into plaintext IMAP. The viewer opens `INBOX` in read-only mode and uses `BODY.PEEK`; it does not send, delete, move or mark messages read. Image payloads are fetched only from explicitly selected MIME sections; the viewer never uses `BODY.PEEK[]`.

The web view automatically prefetches visible images referenced by indexed message bodies. The browser only requests opaque localhost image paths. CID and Content-Location resources are read through the account worker; HTTP(S) resources use a local proxy that sends no cookies or referrer, pins each DNS result, rejects private and reserved address ranges, and permits DNS-derived addresses in `198.18.0.0/15` for transparent-proxy Fake-IP compatibility. Literal URLs in that range remain blocked. Redirect, byte, pixel, animation, concurrency, and timeout limits still apply. A remote image can still reveal the user's public IP, prefetch time, and a sender-controlled tracking token even when the message has never been opened. SVG, CSS image URLs, remote fonts, obvious tracking pixels, attachments, and active content remain blocked.

Message lists fetch headers, flags, `INTERNALDATE`, size and `BODYSTRUCTURE`, never a complete RFC message. Opening a detail walks the MIME tree and fetches only the explicitly selected, non-attachment text section. The web view prefers an HTML alternative and falls back to plain text; CLI commands and existing JSON fields retain plain-text semantics. Related CID image sections are fetched only when the sanitized HTML actually references them; attachment payloads, embedded `message/rfc822` messages and the complete RFC message are never requested. Attachment names are read from `BODYSTRUCTURE`. If the MIME structure cannot be identified safely, the viewer refuses to fetch the body instead of falling back to the full message.

Original HTML exists only briefly in memory and is never rendered or cached. Before display, HTML and inline CSS are sanitized with strict allowlists. Scripts, forms, iframes, SVG, objects, event handlers, automatic redirects, dangerous URL schemes, remote fonts, CSS imports and CSS `url()` values are removed. The sanitized result is rendered in a sandboxed `srcdoc` iframe with an independent Content Security Policy that denies scripts, fonts, media, network connections and direct remote image requests; only opaque same-origin image routes are allowed.

Only visible image references that pass the MIME and remote-resource checks are loaded. Failed images become size-limited text placeholders, while obvious tracking pixels are removed. Only `http` and `https` links remain eligible for navigation, and the local application displays the full destination for confirmation before opening a new tab with `noopener` and `noreferrer`. Blocking unsafe images, CSS image URLs and remote fonts means the viewer cannot reproduce the original mailbox rendering pixel-for-pixel, although text hierarchy, tables, colors and button-like layout can usually be preserved.

The settings page accepts changes only through POST requests carrying a process-local CSRF token. The web server remains bound to `127.0.0.1`; the parent page and mail iframe use restrictive Content Security Policies and do not relax access to remote resources.

## Translation service boundary

Translation is bring-your-own-key and runs only after an explicit user action. The viewer supports fixed official DeepL Free and Pro endpoints plus OpenAI-compatible endpoints. Remote custom endpoints must use HTTPS; plaintext HTTP is permitted only for loopback Ollama hosts. URLs containing credentials, query strings or fragments are rejected. The client does not follow redirects, uses bounded connection and total timeouts, and limits each response to 5 MiB.

At most 100,000 characters from one message are translated. Only the subject and visible body text are submitted. Sender and recipient fields, email addresses, remote URLs, attachment names, image data and mailbox credentials are excluded or protected from modification. Mail text is treated as untrusted data in model prompts. Responses must contain every expected opaque segment ID exactly once; missing, duplicate, malformed or reordered data is rejected before caching.

Model output is never interpreted as HTML. It is escaped into the existing sanitized text-node positions, and the original tag, attribute, link and image-resource structure must remain unchanged. Translation errors leave the original message and any previous valid translation available. Disconnecting or changing a provider removes credentials but deliberately retains encrypted translations until the user clears them.
