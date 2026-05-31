# Architecture

StepAudio Voice Studio is a small self-hosted FastAPI application. It keeps application state in SQLite and generated media on local disk by default.

## Components

```text
Browser UI
  |
FastAPI app
  |
Persistent store
  |- users
  |- tokens
  |- invite codes
  |- clone/TTS tasks
  |- video/media tasks
  |- admin settings
  |
Local file storage
  |- uploaded samples
  |- processed references
  |- generated audio
  |- authorized downloaded media
  |
External providers
  |- StepAudio / StepFun API
  |- optional authorized media providers
```

## Request Flow

1. A user logs in with a bearer token issued by the app.
2. The user creates a clone, TTS, or video/media task.
3. The app checks credits and access permissions.
4. The app calls the configured provider only when needed.
5. Results are stored as task records and local files.
6. Users can only download their own files. Administrators can inspect all records.

## Persistence Model

The application intentionally keeps runtime state outside Git:

- SQLite stores users, credits, tasks, invite codes, settings, and video history.
- Local file storage stores generated audio, processed reference audio, and downloaded files.
- `.env` or a server secret manager stores provider credentials.

Production deployments should use:

```text
DATABASE_URL=sqlite:////var/lib/stepaudio/stepaudio.db
LOCAL_STORAGE_DIR=/var/lib/stepaudio/files
```

This prevents data loss when code is replaced during deploy.

## Provider Boundary

Provider integrations are treated as replaceable edges of the system.

The core app should not depend on a single paid media provider, shortcut parser, or browser extraction strategy. Each provider call should be visible in task metadata so operators can understand cost, quality, and failure reasons.

## Security Boundary

The app currently has two roles:

- normal user: can access their own voice assets, generated audio, and video/media records
- administrator: can generate invite codes, view users, manage platform cookies, inspect records, and change admin password

Important defaults:

- passwords use salted PBKDF2
- production CORS is same-origin by default
- default admin password is rejected when `APP_ENV` is not `local`
- API keys and cookies are not committed to source code

## Known Limitations

- Long-running clone and TTS work currently runs inside the web process.
- SQLite is the only persistent store implemented in this baseline.
- Role-based access control is limited to admin/user.
- Consent records are represented in task metadata and UI confirmation, but exportable consent audit reports are still on the roadmap.
- More route-level automated tests are needed before multi-team deployment.

These limitations are tracked in [Roadmap](./roadmap.md).
