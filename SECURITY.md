# Security Policy

## Supported version

Only the latest version on the `main` branch receives security fixes.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting feature when it is available for this repository. Do not open a public Issue containing:

- email passwords, QQ / NetEase authorization codes, or Apple / Google app-specific passwords;
- real email addresses, message bodies, headers, or attachments;
- macOS Keychain or Windows Credential Manager output and other credentials.

If private reporting is unavailable, open a public Issue containing only a minimal, redacted description and ask the maintainer for a private contact channel.

## Credential storage

The viewer stores each configured account's address and authorization code or app-specific password in the current user's operating-system credential store: the login Keychain on macOS or Credential Manager on Windows. The account index contains only account names, service-provider settings and credential-service identifiers; it never contains the credential value. On Windows, the application rejects overridden `keyring` backends so that it does not silently fall back to a file-based or third-party store.

The viewer never intentionally writes credentials to the repository. Users should revoke the affected authorization code or app-specific password with its email provider immediately if they suspect it has been exposed.

## Read-only connection boundary

All built-in providers and custom accounts use TLS-protected IMAPS with certificate and hostname verification. Custom accounts cannot opt into plaintext IMAP. The viewer opens `INBOX` in read-only mode and uses `BODY.PEEK`; it does not send, delete, move, mark read, or persist message bodies. Opening a message fetches the complete RFC message into memory, so attachment bytes may be transferred with it, but the viewer does not parse, expose, or save attachments.
