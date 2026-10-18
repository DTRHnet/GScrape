# Changelog

All notable changes are documented here. Versions follow semantic versioning.

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