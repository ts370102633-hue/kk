# Roadmap

The roadmap prioritizes maintainability, safety, and operator trust before broad feature expansion.

## Near Term

### Background Worker

Move long-running clone, ASR, TTS, and media tasks out of the web request path.

Expected impact:

- fewer request timeouts
- clearer retry behavior
- safer paid provider usage
- easier horizontal scaling

### Consent Audit Export

Add exportable audit records for voice creation and generated audio.

Expected impact:

- easier internal compliance review
- better speaker deletion workflows
- stronger open-source safety posture

### Route-Level Tests

Add FastAPI tests for login, admin-only routes, owner-only downloads, voice library access, video task access, and failure handling.

Expected impact:

- safer refactors
- better contributor confidence
- stronger CI signal for maintainers

## Mid Term

### Docker Compose Deployment

Add a local and server-oriented Compose example with persistent volumes.

Expected impact:

- easier self-hosting
- fewer deployment mistakes
- clearer backup paths

### Object Storage Backend

Allow generated files to be stored in S3-compatible storage.

Expected impact:

- larger deployments
- easier backups
- cleaner separation between app servers and media storage

### Role-Based Access Control

Extend admin/user to roles such as owner, operator, reviewer, and auditor.

Expected impact:

- safer team workflows
- fewer overpowered admin accounts
- better fit for agencies and internal production teams

## Later

### Provider Plugin Interface

Make provider integrations explicit modules with cost, quota, timeout, and audit metadata.

Expected impact:

- easier replacement of external providers
- better transparency around paid API calls
- safer failure handling

### Release Automation

Add release notes, changelog checks, and versioned migration notes.

Expected impact:

- easier maintenance
- clearer upgrade path for self-hosters

## Non-Goals

The project should not become a tool for bypassing platform controls, paywalls, DRM, or provider usage limits.

The project should not optimize for unauthorized voice cloning, impersonation, or hidden media scraping.
