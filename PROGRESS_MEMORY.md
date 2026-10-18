# GScrape Modular Demo Progress

## Current milestone

Part 2 is implemented as bounded control-plane and dashboard plumbing. Live Gmail
execution remains an explicit operator action and was not started automatically.

- [x] Existing `core/` package reviewed.
- [x] Durable queued/running/terminal job transitions added to `core.storage.Database`.
- [x] Thread-backed asynchronous demo job runner added in `core.async_jobs`.
- [x] Synthetic success/failure tests added for async job execution.
- [x] Provider adapter protocol and deterministic demo provider added.
- [x] Read-only dashboard endpoints added for provider-neutral core jobs/events.
- [x] Dashboard can create asynchronous deterministic demo jobs without Gmail access.
- [x] Bounded Gmail provider adapter added around the legacy scanner; live execution remains explicit.
- [x] Dashboard provider registry routes demo and bounded Gmail jobs through the async runner.
- [ ] Migrate legacy Gmail/OAuth paths behind the adapter without changing CLI behavior.
- [x] Add categorized timeout/startup/nonzero-exit failure semantics.
- [x] Add redacted Gmail match counts and dashboard core-job polling hooks.
- [x] Add version/changelog documentation for the controlled scan milestone.

## Decisions

- SQLite remains the source of truth for job state.
- Worker operations receive configuration snapshots only; secret values remain outside the control plane.
- Job events store status and error type, never message bodies, tokens, or recovery material.
- The legacy scanner remains unchanged until an adapter and migration tests exist.

## Next slice

Register the Gmail adapter with dashboard job submission after focused validation; do not initiate a live scan automatically.

Validation: `python -m py_compile core/*.py tests/test_core_control_plane.py gscrape.py`; `python -m unittest discover -v` (32 tests passed).

Updated: 2026-10-18

## Part 3 completion note

- [x] Added durable cancellation and retry primitives.
- [x] Added allowlisted provider result/event metadata and failure propagation.
- [x] Added synthetic lifecycle/redaction coverage.
- [x] Updated dashboard/server version to 0.4.0 milestone.
- [ ] Authorized live Gmail scan remains operator-gated and requires an existing authorized session.

## Part 4 completion note

- [x] Added operator-gated live-scan health/status reporting.
- [x] Added durable cancellation/retry dashboard hooks.
- [x] Added bounded event retention and generated-result cleanup.
- [x] Added endpoint/lifecycle test coverage and versioned operational notes.
- [ ] Live Gmail validation remains pending an explicitly authorized session.

Part 5 should focus on OAuth/session health, live authorized validation, provider retry
policy, result pagination, and production deployment/observability controls.

## Part 5 completion note

- [x] Added bounded provider retry handling without automatic authorization.
- [x] Added dashboard cancel/retry lifecycle controls.
- [x] Added health/version metadata and bounded cleanup/event retention.
- [x] Preserved redaction and explicit live-scan authorization boundaries.
- [ ] Live Gmail validation and token refresh remain operator actions.

Part 6 should focus on authorized end-to-end validation, OAuth account health, pagination
telemetry, configurable retention, and production deployment hardening.