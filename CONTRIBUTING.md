# Contributing

Thank you for considering a contribution to StepAudio Voice Studio.

This project is focused on safe, self-hosted voice workflows for authorized media. Contributions should improve reliability, transparency, safety, or maintainability.

## Good First Areas

- documentation and deployment examples
- tests for authentication, storage, and task behavior
- safer handling of long-running jobs
- consent audit metadata
- admin and operator UX improvements
- provider abstraction for authorized media sources
- security hardening around secrets, cookies, and generated files

## Local Development

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
```

Edit `.env` with local credentials. Never commit `.env`.

Run verification:

```bash
./init.sh
python -m pytest -q
```

Run the app:

```bash
./start.sh
```

## Pull Request Expectations

Before opening a PR:

- keep changes focused
- avoid committing generated media, databases, cookies, or secrets
- update documentation for operator-visible behavior
- add or update tests when touching shared behavior
- run `./init.sh`
- run `python -m pytest -q` when development dependencies are installed
- explain any provider/API behavior that may cost money or consume quota

## Safety Expectations

Do not submit changes that:

- bypass provider rate limits, paywalls, DRM, or access controls
- enable unauthorized voice cloning
- hide provider usage from administrators
- hard-code API keys, platform cookies, or tokens
- silently retry paid provider calls without clear limits

## Issue Guidelines

When opening an issue, include:

- expected behavior
- actual behavior
- deployment method
- relevant provider mode, if any
- sanitized logs or screenshots

Do not include secrets, full cookies, private media URLs, or API keys.

## Documentation Standards

Documentation should follow the structure used in `docs/`:

- tutorials help a new user complete setup
- how-to guides solve operational problems
- explanations clarify safety and architecture decisions
- reference pages document variables, providers, and release behavior
