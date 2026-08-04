# Security Policy

## Supported version

Only the latest version on the `main` branch receives security fixes.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting feature when it is available for this repository. Do not open a public Issue containing:

- QQ Mail authorization codes or passwords;
- real email addresses, message bodies, headers, or attachments;
- macOS Keychain output or other credentials.

If private reporting is unavailable, open a public Issue containing only a minimal, redacted description and ask the maintainer for a private contact channel.

## Credential storage

The viewer stores the QQ Mail address and authorization code in the current macOS user's login Keychain. It never intentionally writes those values to the repository. Users should revoke the authorization code in QQ Mail settings immediately if they suspect it has been exposed.
