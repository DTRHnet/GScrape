# AGENTS.md — Crypto Asset Discovery and Wallet Recovery

## 1. Mission and Scope

This workspace contains tools for locating historical email evidence associated with GoURL, cryptocurrency services, wallet backups, recovery material, and related identities.

The primary workflows are:

- `gscrape.py` — search an authorized Gmail account for relevant messages and record redacted findings.
- `recover_wallet.py` — perform offline wallet-backup recovery only when explicitly requested.
- `email-wallet-search.log` — append-only audit trail of non-secret search findings.

Keep email discovery, artifact preservation, and wallet recovery as distinct workflows. Finding a potentially relevant email does not authorize downloading its attachments, extracting wallet contents, or attempting to recover a password.

## 2. Mandatory Safety and Data-Handling Rules

- Treat wallet files, seed phrases, private keys, passwords, access tokens, OAuth credentials, and recovery outputs as highly sensitive.
- Never print, expose, transmit, or write secret material to logs, reports, test output, or source control.
- Redact sensitive values as `[REDACTED]`. Retain only the non-sensitive metadata needed to locate and evaluate a finding.
- Never ask the user to provide a password, MFA code, recovery phrase, private key, or other secret.
- Never launch password-recovery attempts, modify recovery parameters, or initiate a wallet-recovery operation unless the user explicitly requests it.
- Preserve existing artifacts. Write any requested recovered output to a new path; never overwrite an existing wallet or backup.
- Do not download, open, extract, or modify wallet attachments during email discovery unless the user separately authorizes that operation.
- Do not commit `.env`, OAuth tokens, mailbox contents, wallet files, or sensitive logs.
- Minimize data collection: record evidence and metadata, not full message bodies or attachment contents.

## 3. Python Development Workflow

- Follow the repository’s existing Python conventions and prefer the standard library where practical.
- Use the workspace `.venv` when available.
- Keep search logic, pattern definitions, authentication, message parsing, scoring, and logging separated into testable functions.
- Do not introduce a dependency solely for functionality that is already adequately supported by the standard library.
- For each Python change, run a syntax check and the relevant focused tests.
- Test matching behavior with synthetic examples. Never use real recovery phrases, private keys, passwords, or live mailbox contents as test fixtures.
- Keep command-line help accurate and update tests and documentation whenever options, categories, or output schemas change.

## 4. Gmail Scanner: `gscrape.py`

### 4.1 Authentication and authorization

- Use the Gmail API only with user-authorized OAuth credentials.
- `GOOGLE_API_KEY` alone does not grant access to private Gmail messages.
- Read configuration from the supported environment variables or configured `.env` file.
- Never print or log API keys, access tokens, refresh tokens, client secrets, authorization codes, or PKCE values.
- Preserve the existing local OAuth flow and validate its state and redirect URI.
- Use the minimum necessary Gmail permission scope, currently `https://www.googleapis.com/auth/gmail.readonly`.
- If authentication, consent, MFA, or account selection requires user action, stop and ask the user to complete it. Do not attempt to bypass account protections.
- Do not create a remote browser session or separate browser profile to circumvent the user’s existing authenticated session.

### 4.1.1 Explicit operator authorization (recorded 2026-10-19)

- The workspace owner has explicitly authorized the agent to restart the local dashboard, use the existing local browser session where available, and run controlled Gmail live scans without requesting separate confirmation for each run.
- This authorization covers discovery scans and attachment-metadata discovery. It does not authorize bypassing Gmail authentication, MFA, consent, account selection, or other provider protections.
- The agent may acquire attachment bytes only when the scan request explicitly enables acquisition; acquisition remains separate from discovery and must preserve originals and write to a new path.
- The authorization does permit printing, logging, transmitting, or committing secrets, message bodies, attachment contents, tokens, or OAuth credentials. Reports and logs must remain redacted.
- If no already-authorized account/session is available, the agent must stop at the authorization boundary and report the non-secret reason rather than attempting to bypass it.

### 4.2 Search strategy

Use multiple focused search passes rather than depending on a single query or keyword. Keep each pass identifiable so that results can be audited and deduplicated.

Search relevant message text and available attachment metadata using the scanner’s configured categories and patterns. Consider spelling variations, punctuation, whitespace, case differences, common obfuscations, and related terminology.

Search categories should cover, where supported by the scanner:

- **GoURL and service references:** `gourl`, `go url`, `gourl.io`, and plausible spacing or punctuation variations.
- **Wallet terminology:** cryptocurrency wallets, wallet backups, wallet exports, wallet files, and backup-related language.
- **Recovery terminology:** recovery seed, seed phrase, mnemonic, backup phrase, wallet recovery, and related terms.
- **Wallet filenames:** names matching `wal*.dat`, case-insensitively, including filenames surfaced through attachment metadata.
- **Cryptocurrency identifiers:** recognizable Bitcoin and Ethereum address patterns and related asset terminology. Treat an address-shaped string as a candidate, not proof of ownership or relevance.
- **Identity variants:** `jstacey`, `jonstacey`, `jstacy`, `jonstacy`, `johnstacy`, and `johnstacey`, together with relevant email-address variations when known and authorized for the search.

Add patterns when new evidence justifies them. Keep broad exploratory patterns separate from high-confidence patterns to reduce false positives.

Do not treat every message mentioning cryptocurrency as a wallet-recovery lead. Evaluate the match category, surrounding non-secret context, date, sender, subject, and other available metadata.

### 4.3 Date boundaries

- The historical search target is January 1, 2013 through December 31, 2018, inclusive.
- Apply date filters consistently across search passes.
- When a date is missing, ambiguous, or cannot be normalized reliably, record that limitation instead of silently assigning a date.
- If the user requests a different time range, change the range only for that request and document the scope used.
- Do not claim that a mailbox was exhaustively searched unless the scanner’s output supports that conclusion.

### 4.4 Message and attachment handling

- Process message text in memory when feasible.
- Decode MIME parts defensively and tolerate malformed, missing, or unexpected content.
- Normalize text for matching without changing the original message or attachment.
- Inspect attachment names and available metadata when supported. Filename matches are evidence for review, not permission to download or inspect the file.
- Do not log raw MIME payloads, full message bodies, attachment bytes, seeds, keys, or wallet contents.
- Distinguish a match in the message body from a match in an attachment filename or other metadata.
- Handle duplicate message IDs and overlapping search passes without creating misleading duplicate findings.

### 4.5 Evidence and confidence

- Preserve the matched category and the type of evidence that triggered the match.
- Where the scanner provides weighted evidence or ranking, treat the score as a prioritization aid, not a probability or proof.
- Prefer explainable evidence: identify the categories or patterns that matched without reproducing sensitive matched content.
- Keep potential matches separate from confirmed findings.
- Do not infer ownership, account identity, wallet contents, or successful recovery from a keyword match alone.
- Make false positives visible during review rather than silently discarding all lower-confidence candidates.

### 4.6 Reliability and failure handling

- Use bounded timeouts and the scanner’s retry/backoff behavior for transient network errors and rate limits.
- Do not retry indefinitely. Respect API responses and stop when the configured attempt or runtime limits are reached.
- Refresh OAuth access tokens only through the supported refresh flow.
- If authorization fails, explain the non-secret cause and required user action without exposing credential material.
- Handle pagination, repeated IDs, missing fields, malformed messages, and partial failures defensively.
- Record scan scope and completion status so an interrupted or partial run is not mistaken for a complete search.
- Do not silently convert API failures into a successful zero-match result.

## 5. Audit Log: `email-wallet-search.log`

Append findings; never truncate or overwrite the existing log.

Where supported by the scanner, record structured JSON Lines entries containing non-secret fields such as:

- Event type and provider
- Account label or configured Gmail user identifier
- Search category and matched pattern category
- Match source, such as message text or attachment metadata
- Message date, sender, and subject
- Message location or Gmail message link
- Search pass or query identifier
- Scan counts, pagination information, and completion status
- Evidence score or confidence tier, if produced by the scanner

Never include full message bodies, passwords, recovery seeds, private keys, tokens, raw attachment data, or wallet contents.

Keep each record independently parseable. Preserve existing entries, and handle logging failures explicitly. If a log contains legacy sensitive data, do not reproduce it in reports; ask before performing any destructive cleanup or rewriting historical records.

## 6. Browser-Based Search Across Providers

When the user authorizes a broader search beyond Gmail:

- Use only the existing local Chrome or Chromium session through the approved browser-use workflow.
- Check the currently authenticated providers systematically, including Gmail, Hushmail, and other providers exposed in the existing session.
- Maintain one working tab per provider/account where practical, and record which accounts were checked.
- Apply the same historical date range, identity variants, crypto terminology, privacy rules, and audit-log requirements as the Gmail API workflow.
- If an account is signed out, blocked, or requires a password, MFA, consent, or account selection, stop and ask the user to authenticate it.
- Never claim that a provider was searched when it was unavailable or not checked.
- Do not use browser access to circumvent authentication or access an account without authorization.

## 7. Wallet Recovery Workflow

- Treat email discovery as evidence gathering, not a recovery attempt.
- Do not automatically pass discovered filenames, attachments, passwords, or candidate recovery material to `recover_wallet.py`.
- Before recovery, confirm that the user explicitly requested the operation and that the intended input artifact is clear.
- Preserve original files and work on a copy where appropriate.
- Write recovered outputs to a new, non-conflicting path.
- Never include recovered secrets in logs, console output, summaries, or test artifacts.
- Report the outcome and output location without revealing wallet contents or secret material.

## 8. Testing and Validation

Use synthetic fixtures to verify, as applicable:

- Case-insensitive and obfuscated GoURL matches
- Wallet, backup, and recovery terminology
- `wal*.dat` filename matching, including case variations
- Bitcoin and Ethereum address-shaped candidates
- Identity spelling variants
- MIME text decoding and attachment filename extraction
- Date-range boundaries and unknown dates
- Evidence categorization, scoring, and duplicate suppression
- Redaction of secrets from all log events
- Pagination, retry limits, and interrupted-run reporting
- OAuth failure handling without credential leakage
- Append-only JSONL output and correct audit counts

At minimum, run:

```bash
python -m py_compile gscrape.py
python -m unittest discover -v
```

Run the focused scanner test module explicitly as well if the repository’s test layout requires it. Do not run a live mailbox scan as a substitute for unit tests.

## 9. Completion Reports

When reporting a scan:

- State which provider/account was checked, the configured date range, categories or passes used, and the number of messages scanned.
- Separate confirmed pattern matches from candidate findings and false positives.
- State whether the run completed, was interrupted, or was limited by authorization, API errors, or scan limits.
- Point to the audit log and message locations using non-secret metadata.
- Do not reproduce message bodies, passwords, seeds, private keys, access tokens, or wallet contents.
- Clearly distinguish “no matches found in the completed search” from “no relevant emails exist.”

## 10. Operating Principle

Search broadly, record narrowly, preserve originals, and make every conclusion traceable to non-secret evidence. Discovery must remain reversible and non-destructive; recovery must remain a separate, explicitly authorized action.
