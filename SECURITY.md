# Security Policy

## Supported version

Only the latest version on the `main` branch receives security fixes.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting feature when it is available for this repository. Do not open a public Issue containing:

- QQ Mail authorization codes or passwords;
- real email addresses, message bodies, headers, or attachments;
- macOS Keychain or Windows Credential Manager output and other credentials.

If private reporting is unavailable, open a public Issue containing only a minimal, redacted description and ask the maintainer for a private contact channel.

## Credential storage

The viewer stores the QQ Mail address and authorization code in the current user's operating-system credential store: the login Keychain on macOS or Credential Manager on Windows. On Windows, the application rejects overridden `keyring` backends so that it does not silently fall back to a file-based or third-party store.

The viewer never intentionally writes credentials to the repository. Users should revoke the authorization code in QQ Mail settings immediately if they suspect it has been exposed.
