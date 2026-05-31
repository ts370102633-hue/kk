# Maintenance Guide

This guide describes the maintainer workflow for releases, issues, security, and routine checks.

## Local Checks

```bash
python -m compileall -q backend/app
python -m pytest -q
```

The CI workflow runs these checks on Python 3.11 and 3.12.

## Release Checklist

1. Confirm `main` is green in CI.
2. Review open security or data-loss issues.
3. Update `CHANGELOG.md`.
4. Confirm `.env`, database files, cookies, and generated media are not tracked.
5. Create a GitHub release with a clear operator impact summary.
6. Mention any migration or environment variable changes.

## Issue Triage

Use these labels when the project grows:

- `bug`: user-visible defect
- `security`: auth, secret, generated-file exposure, or abuse risk
- `docs`: documentation and onboarding
- `deployment`: Linux, Docker, Render, Nginx, HTTPS, backup
- `provider`: StepAudio, ASR, TTS, or authorized media provider behavior
- `good first issue`: low-risk contributor task

## Pull Request Review Priorities

Review changes in this order:

1. Consent, abuse, or impersonation risk
2. Secret handling and provider key exposure
3. Authorization behavior for user-owned records
4. Persistence and migration impact
5. Provider cost, retry, and quota behavior
6. Operator documentation
7. UI and workflow polish

## Security Triage

Handle these privately when possible:

- leaked API keys, cookies, or tokens
- auth bypass
- generated-file exposure
- arbitrary file read/write
- unsafe uploaded media handling
- changes that make unauthorized voice cloning easier

If a key was ever committed or pasted into a public channel, rotate it at the provider. Git history cleanup is helpful, but it does not invalidate a leaked key.

## Backup Verification

For production SQLite deployments:

```bash
sqlite3 /var/lib/stepaudio/stepaudio.db "PRAGMA integrity_check;"
sqlite3 /var/lib/stepaudio/stepaudio.db ".backup '/root/stepaudio-backups/stepaudio-$(date +%F-%H%M).db'"
tar -czf "/root/stepaudio-backups/stepaudio-files-$(date +%F-%H%M).tar.gz" /var/lib/stepaudio/files
```

Backups must include both the database and generated files.
