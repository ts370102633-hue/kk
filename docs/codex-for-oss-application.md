# Codex For Open Source Application Draft

This page keeps concise application copy for the maintainer to adapt before submitting.

## Repository Qualification

Form limit target: under 500 characters.

```text
This repository is a self-hosted AI Voice Studio for authorized voice workflows. It helps small teams manage consent-based voice assets, extract speech from legally owned media, generate TTS jobs, and keep auditable records through a FastAPI backend and lightweight web UI. The project provides a transparent OSS alternative to closed voice automation tools, with deployment, security, and consent documentation.
```

## API Credit Usage

Form limit target: under 500 characters.

```text
I would use API credits to improve OSS maintenance: review pull requests, triage issues, write FastAPI route and security tests, improve documentation, generate release notes, and refactor long-running voice/TTS jobs into a worker-based architecture. Codex would also help harden API key handling, authorization checks, consent audit logs, and contributor onboarding.
```

## Additional Context

```text
I am the primary maintainer. The project focuses on legally authorized voice asset workflows, not impersonation or unauthorized scraping. Recent work added MIT licensing, security policy, contribution guide, deployment docs, CI, roadmap issues, environment-based secret handling, salted password hashing, production CORS defaults, and a v0.1.x release.
```

## Recommended Positioning

Use this positioning:

```text
Self-hosted AI voice workflow for authorized voice assets, TTS jobs, and auditable media operations.
```

Avoid this positioning:

```text
Personal voice clone and video download tool.
```

## Evidence To Mention

- Public repository
- MIT license
- Security policy
- Contribution guide
- CI workflow
- v0.1.x release
- Roadmap issues
- Persistent storage design
- Consent and safety documentation
- Primary maintainer role

## Current Weaknesses

- Low public adoption signal until the project receives stars, forks, issues, or external users
- Pre-1.0 maturity
- Voice cloning requires strong safety framing
- More automated route tests are still needed

## Submission Recommendation

Apply after the v0.1.1 release is published and the repository name, description, topics, and README all reflect the open-source positioning.
