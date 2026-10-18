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