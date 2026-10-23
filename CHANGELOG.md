# Changelog

All notable changes are documented here.

## [1.3.0] - 2026-10-23

### Added

- Added an isolated Raw Export workflow with a separate SQLite database for complete unredacted Gmail messages and optional attachment blobs.

### Changed

- Prepared the repository for public release with expanded privacy-focused
  ignore rules and removal of local-only operator artifacts from the workspace.
- Added step-by-step setup, OAuth, dashboard, scanning, testing, and security
  guidance to the README.
- Added centered dashboard screenshots and maintainer credits to the README.

## [1.1.0] - 2026-10-19

### Added

- Added bounded scrolling for expanded run details.
- Added full-page run views that open in a new tab.
- Added cross-run resolution for deduplicated attachment files.

## [1.0.0] - 2026-10-19

### Security and discovery

- Added fail-closed protected-artifact capture contracts and non-secret artifact metadata tables.
- Added promotional/bulk-mail relevance classification that preserves strong wallet evidence.
- Changed attachment acquisition defaults to opt-in; secure capture requires an approved encrypted backend.

### Added

- Added operator stop, resume, and retry controls for dashboard discovery runs.
- Added subprocess tracking and durable cancelled-run state handling.
- Added dashboard diagnostics for the configured local HTTP endpoint.
- Added SHA-256 attachment deduplication and completed-run thumbnail support.

### Fixed

- Fixed the debug health check to report the dashboard's actual configured host and port.
- Fixed the README logo reference to load from GitHub's raw content URL.

## [0.7.0] - 2026-10-18

### Added

- Added non-secret account health snapshots and operational job metrics.
- Added configurable result retention through `GSCRAPE_RETENTION_DAYS`.
- Added operator-visible control-plane health details without exposing credentials.
- Added bounded job-event cursor pagination and operator-approved account health updates.

## [0.6.0] - 2026-10-18

### Added

- Added bounded scanner retry behavior for transient nonzero exits.
- Added dashboard cancel/retry controls with safe event delegation.
- Added explicit operator-gated Gmail health metadata and Part 5 lifecycle hooks.
- Added synthetic validation coverage for retry policy and safe endpoint metadata.

## [0.5.0] - 2026-10-18

### Added

- Added bounded event retention and generated-result cleanup helpers.
- Added control-plane health/status metadata and operator-gated live-scan state.
- Added dashboard support for cancelled jobs and lifecycle action polling.
- Added endpoint and lifecycle test coverage for the Part 4 safeguards.

## [0.3.0] - 2026-10-18

### Added

- Added bounded Gmail job execution with account-derived session paths.
- Added categorized scanner timeout, startup, and nonzero-exit failure metadata.
- Added redacted match counts for successful Gmail jobs, including zero-match runs.
- Added control-plane status polling hooks for dashboard consumers.
- Added operational notes for controlled scans and explicit live authorization.

## [0.4.0] - 2026-10-18

### Added

- Added queued-job cancellation and safe retry creation endpoints.
- Added allowlisted provider result payloads and provider-failure propagation.
- Added lifecycle safeguards for non-secret event metadata and dashboard polling.
- Added synthetic coverage for cancellation, retry, failure, and redaction behavior.

## [0.2.0] - 2026-10-17

### Added

- Introduced the `core` package with provider-neutral account, OAuth-session,
  and job models.
- Added versioned SQLite control-plane storage with WAL mode, foreign keys,
  busy-timeout handling, and migration support.
- Added atomic OAuth-state consumption to prevent callback replay and support
  concurrent authorization sessions.
- Added OS-assigned loopback port allocation for standalone OAuth listeners.
- Added a secret-reference interface that keeps secret values outside the
  database model.
- Added tracked implementation roadmap and focused-validation policy.

### Not yet migrated

- The existing dashboard and Gmail scanner still use their legacy paths. The
  next milestone will wire them to the new core services.
