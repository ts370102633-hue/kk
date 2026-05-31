# Quickstart

This tutorial gets StepAudio Voice Studio running locally with persistent local storage.

## Prerequisites

- Python 3.11 or 3.12
- A StepAudio / StepFun API key for clone, ASR, and TTS features
- A terminal with access to this repository

Python 3.14 is not recommended for this dependency set yet because native dependencies may not publish compatible wheels.

## 1. Create A Virtual Environment

```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

If your machine only has Python 3.11, use:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
```

## 2. Install Dependencies

```bash
pip install -r requirements.txt
```

For tests and contributor checks:

```bash
pip install -r requirements-dev.txt
```

## 3. Configure Environment Variables

```bash
cp .env.example .env
```

For local development, set:

```bash
APP_ENV=local
DATABASE_URL=sqlite:///./data/stepaudio.db
LOCAL_STORAGE_DIR=./data/files
STEP_API_KEY=your_stepaudio_api_key
ADMIN_USERNAME=admin
ADMIN_PASSWORD=change-this-local-password
```

Never commit `.env`.

## 4. Verify The App Imports

```bash
./init.sh
```

Expected output includes:

```text
StepAudio Voice Studio
Verification script finished.
```

## 5. Start The App

```bash
./start.sh
```

Open:

```text
http://localhost:8808
```

## 6. First Admin Login

Use the admin username and password from `.env`.

Recommended first actions:

1. Change the admin password from the admin panel.
2. Generate an invite code.
3. Register a normal user with that invite code.
4. Upload only a voice sample that you are authorized to process.
5. Generate a short TTS test.

## 7. Run Tests

```bash
python -m pytest -q
```

The test suite currently focuses on security basics and import-level stability. More FastAPI route tests are tracked in the roadmap.

## Troubleshooting

If clone, ASR, or TTS fails, confirm `STEP_API_KEY`, `STEP_API_BASE`, and `STEP_TTS_MODEL`.

If the app starts but data disappears after deploy, your `DATABASE_URL` or `LOCAL_STORAGE_DIR` is probably inside a disposable code checkout. Use absolute persistent paths as described in [Deployment](../DEPLOYMENT.md).
