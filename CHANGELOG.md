# Changelog

All notable changes to this project are documented here.

## v0.1.1 - Open Source Readiness

- Added CI for Python 3.11 and 3.12.
- Added security-focused tests for password hashing and production CORS defaults.
- Upgraded new password hashes to salted PBKDF2 with legacy SHA256 verification for existing local installs.
- Changed production CORS default to same-origin only unless `CORS_ORIGINS` is configured.
- Expanded README with positioning, architecture, safety, development, and roadmap sections.
- Added quickstart, architecture, consent and safety, maintenance, roadmap, and Codex OSS application docs.
- Added code of conduct.
- Added workflow diagram for repository onboarding.

## v0.1.0 - Open Source Baseline

- Published the initial open-source baseline.
- Added MIT license.
- Added security policy and contribution guide.
- Added environment variable example.
- Added deployment notes for persistent SQLite and generated file storage.
- Included FastAPI backend, static UI, voice workflow, TTS tasks, user/invite/credit logic, admin controls, and persistent local storage.
