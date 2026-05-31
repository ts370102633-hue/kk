# Security Policy

## Supported Versions

The project is currently pre-1.0. Security fixes should target the latest `main` branch unless a release branch is explicitly created.

## Reporting A Vulnerability

Please do not open a public issue for sensitive vulnerabilities, leaked keys, authentication bypasses, or provider credential exposure.

Use a private maintainer contact channel for:

- API keys, tokens, cookies, or credentials found in code, logs, issues, or artifacts
- authentication or authorization bypasses
- arbitrary file read/write paths
- unsafe handling of uploaded media
- consent or impersonation abuse paths
- stored generated audio or video exposure

If no private contact is configured yet, open a minimal public issue that says a private security contact is needed, without exploit details or secrets.

## Operator Responsibilities

This project handles voice, media, generated audio, platform cookies, and paid API credentials. Operators are responsible for configuring and running it safely.

Before production use:

- rotate any key that has ever been committed, pasted into chat, or shown in logs
- store secrets only in `.env`, systemd environment files, cloud secret managers, or equivalent protected stores
- never commit `.env`, cookies, database files, generated media, or downloaded videos
- set a strong `ADMIN_PASSWORD`
- restrict admin access to trusted operators
- configure HTTPS in front of the app
- set `CORS_ORIGINS` to the public HTTPS origin of the deployed app
- back up the database and generated file storage
- review provider terms for all configured media and AI APIs

## Implemented Baseline Controls

- New password hashes use salted PBKDF2.
- Legacy SHA256 password hashes from earlier local installs still verify and are upgraded after successful login.
- Production CORS defaults to same-origin only unless `CORS_ORIGINS` is configured.
- Production startup rejects the default admin password.
- Generated audio and video file downloads require authentication and owner/admin access.
- Admin-managed cookies are stored as application settings and only masked previews are returned to the UI.

## Voice Consent Requirements

Voice cloning must only be used with explicit authorization from the speaker or with content that the operator has the right to process.

Recommended production policy:

- keep a consent record for each cloned voice
- store speaker name, source file, operator, and creation time
- allow deletion of voice assets and generated samples
- prohibit impersonation, fraud, harassment, or unauthorized public use
- review generated audio before publishing

## Media Retrieval Requirements

Optional video/media retrieval features are intended for authorized source media. Do not use this project to bypass platform access controls, provider rate limits, paywalls, DRM, or copyright restrictions.

When integrating third-party media providers:

- use provider keys or authorization codes only when licensed to do so
- do not hard-code keys or cookies
- keep provider calls single-purpose and auditable
- handle rate limits without automatic uncontrolled retries
- document which provider is used for each downloaded task

## Secret Rotation Checklist

If a secret is exposed:

1. Revoke or rotate it in the provider console immediately.
2. Remove it from local files and deployment configs.
3. Confirm it is not present in current source files.
4. Assume old Git history, screenshots, and chat logs may remain compromised.
5. Review provider usage logs for unexpected spend or abuse.
