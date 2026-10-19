# GScrape implementation roadmap

Status is tracked here so implementation can proceed in small, reviewable
increments without spending time on low-value or redundant test runs.

## Current milestone

- [x] Establish reusable core package boundaries.
- [x] Add versioned SQLite control-plane schema for providers, accounts,
  credentials, OAuth sessions, jobs, and job events.
- [x] Add atomic OAuth-state consumption and OS-assigned loopback-port helper.
- [x] Add secret-reference and provider-neutral model interfaces.
- [x] Wire dashboard settings to shared provider account records and OAuth-session state.
- [x] Inspect existing legacy `.env` authorization without copying secrets into SQLite.
- [x] Add shared OAuth callback state consumption and explicit token-exchange boundary.

## Planned milestones

1. Shared OAuth callback and multi-account authorization.
2. Credential-store backend and legacy `.env` import.
3. Provider adapter boundary around Gmail.
4. Isolated parallel job workers with per-account rate limiting.
5. Analysis plugin registry and crypto-discovery plugin extraction.
6. Dashboard/API migration to core services.
7. Database migration cleanup and release `1.0.0`.

## Validation policy

Run focused tests for changed core components and syntax checks for changed
Python files. Run the complete suite only at milestone boundaries. Never use a
live mailbox scan as a test substitute, and never run wallet recovery as part
of ordinary validation.