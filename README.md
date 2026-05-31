# StepAudio Voice Studio

StepAudio Voice Studio is a self-hosted AI voice workflow app for authorized voice assets. It helps small content teams and developers upload audio or video samples, extract speech, create voice assets, generate TTS audio, and keep a local audit trail for users, credits, and jobs.

The project is designed as a transparent alternative to closed internal voice-cloning tools. It is not intended for impersonation, unauthorized scraping, or use of voices or media without permission.

## Features

- Voice sample upload from audio or video files
- Automatic reference-audio preprocessing and speech segment selection
- StepAudio voice cloning through configurable cloud API credentials
- TTS generation with task history and downloadable audio
- Voice library with ownership and admin access checks
- Video audio extraction and transcript parsing for authorized media workflows
- Optional video retrieval providers for user-owned or authorized source links
- User accounts, invite codes, credits, and admin password management
- Persistent SQLite storage for users, tasks, settings, and video records
- Admin-managed platform cookies and provider diagnostics
- Docker, Render, and Linux deployment examples

## Intended Use

This project is for teams that need a self-hosted voice production workflow where operators can manage authorized voice samples and generated audio in one place.

Good use cases include:

- Creating internal voice assets from speakers who have given permission
- Turning approved source media into internal draft voiceover material
- Managing TTS jobs for short-form content production
- Auditing who created a voice asset or generated audio
- Experimenting with queue, storage, and provider integrations in an open codebase

Do not use this project to clone a voice without consent, bypass platform controls, rehost copyrighted media without rights, or evade provider usage limits.

## Architecture

```text
Browser UI
  |
FastAPI app
  |
SQLite persistent store
  |
Local file storage
  |
External providers
  |- StepAudio / StepFun API for ASR, voice clone, and TTS
  |- Optional authorized video/media providers
```

The default deployment stores application data outside the code checkout:

```text
DATABASE_URL=sqlite:////var/lib/stepaudio/stepaudio.db
LOCAL_STORAGE_DIR=/var/lib/stepaudio/files
```

## Quick Start

1. Create a virtual environment.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Create local environment config.

```bash
cp .env.example .env
```

3. Edit `.env`.

At minimum, set:

```bash
STEP_API_KEY=your_stepaudio_api_key
ADMIN_PASSWORD=change-this-before-use
```

4. Run the app.

```bash
./start.sh
```

The default local URL is:

```text
http://localhost:8808
```

## Required Environment Variables

| Variable | Purpose |
| --- | --- |
| `STEP_API_KEY` | StepAudio / StepFun API key. Required for clone, ASR, and TTS. |
| `ADMIN_PASSWORD` | Initial admin password. Required in production. |
| `DATABASE_URL` | SQLite database URL. |
| `LOCAL_STORAGE_DIR` | Directory for generated files and downloads. |
| `STEP_API_BASE` | StepAudio plan API base URL. |
| `STEP_FILE_API_BASE` | StepFun file API base URL. |
| `STEP_ASR_MODEL` | ASR model name. |
| `STEP_TTS_MODEL` | TTS model name. |

Optional variables for media retrieval and diagnostics are documented in [.env.example](./.env.example) and [DEPLOYMENT.md](./DEPLOYMENT.md).

## Deployment

For a Linux server deployment, see [DEPLOYMENT.md](./DEPLOYMENT.md).

The deployment guide covers:

- persistent database and file paths
- environment file setup
- systemd service setup
- Nginx/HTTPS placement
- production checks
- video-provider configuration

## Security And Compliance

Voice cloning and media retrieval have meaningful abuse and compliance risks. This project expects operators to enforce consent, source-media rights, and provider terms.

Baseline protections included in the app:

- API keys are read from environment variables, not source code
- production startup rejects the default admin password
- admin password can be changed in the UI
- user and task records are persisted
- video and audio download endpoints enforce owner/admin checks
- platform cookies are admin-only settings

Still required before public or production use:

- rotate any API key that has ever been committed, pasted, or exposed
- configure strong `ADMIN_PASSWORD`
- review CORS and network exposure for your deployment
- add external backups for the SQLite database and generated files
- document speaker consent and deletion workflows for your team

See [SECURITY.md](./SECURITY.md) for vulnerability reporting and operator guidance.

## Roadmap

- Dedicated worker process for long-running clone, ASR, and TTS jobs
- Docker Compose deployment with persistent volume examples
- Object storage backend for generated files
- Consent audit export
- Role-based access control beyond admin/user
- Provider plugin interface for media retrieval
- Automated tests for FastAPI routes and storage behavior
- Release workflow and changelog automation

## Contributing

Contributions are welcome when they improve safety, maintainability, documentation, deployment reliability, or authorized voice workflows.

Start with [CONTRIBUTING.md](./CONTRIBUTING.md).

## License

This project is licensed under the MIT License. See [LICENSE](./LICENSE).

