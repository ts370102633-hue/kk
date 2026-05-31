# Consent And Safety Model

This project is designed for authorized voice workflows. It should not be used to impersonate people, clone voices without permission, bypass platform controls, or publish generated audio without appropriate rights.

## Policy Baseline

Operators should require explicit permission before creating a voice asset.

A practical consent record should include:

- speaker name or internal identifier
- source file or recording session reference
- operator who uploaded the sample
- creation time
- allowed use case
- retention or deletion requirement
- whether public publishing is allowed

## In-App Controls

The current app includes these baseline controls:

- the upload form requires an authorization confirmation checkbox
- clone and TTS tasks are linked to the user who created them
- normal users only see their own records
- administrators can inspect user and task history
- generated files are served through authenticated endpoints
- platform cookies are admin-managed and returned only as masked previews
- production startup rejects the default admin password

## Operator Responsibilities

Before using the app in production:

1. Define who can approve a speaker sample.
2. Store consent records outside the generated media file itself.
3. Keep API keys and platform cookies in server secrets, not Git.
4. Review generated output before publishing.
5. Give speakers a deletion process when required by policy or law.
6. Back up the database and generated files.
7. Track which provider was used for each expensive or paid operation.

## Prohibited Uses

Do not use this project to:

- clone a person who has not given permission
- create deceptive audio for fraud, harassment, or reputational harm
- bypass DRM, paywalls, platform rate limits, or access controls
- hide paid provider usage from administrators
- rehost copyrighted media without rights
- publish generated audio as another person without disclosure where disclosure is required

## Roadmap Improvements

The next safety improvements are:

- exportable consent audit reports
- richer speaker metadata
- role-based access control beyond admin/user
- retention policy helpers
- provider-cost and provider-quality reporting
- route-level tests for owner/admin authorization behavior
