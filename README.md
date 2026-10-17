# GScrape

<p align="center">
  <img src="logo.jpg" alt="GScrape logo" width="720">
</p>

<p align="center">
  <strong>Your mail · your data · your rules</strong>
</p>


> Take control of your email. Search it, understand it, and turn years of
> messages into useful personal data — locally, privately, and on your terms.

[![Release](https://img.shields.io/badge/release-v1.0.0-2ea44f?style=flat-square)](https://github.com/DTRHnet/GScrape/releases/tag/v1.0.0)
[![Python](https://img.shields.io/badge/python-3.9%2B-3776ab?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Gmail API](https://img.shields.io/badge/API-Gmail%20readonly-ea4335?style=flat-square&logo=gmail&logoColor=white)](https://developers.google.com/gmail/api)
[![Tests](https://img.shields.io/badge/tests-26%20passing-2ea44f?style=flat-square)](#testing)
[![Security](https://img.shields.io/badge/security-redacted%20output-d73a4a?style=flat-square)](#security-and-data-handling)

**Version:** `1.0.0` · **Status:** stable foundation · **Deployment:** self-hosted

## Overview

GScrape is a self-hosted toolkit for quickly parsing your own email accounts.
Start with a ready-made preset, refine it with filters and date ranges, or write
your own regular expressions and extraction rules. Use deterministic local
matching when you want predictable results; optionally add an LLM later when
semantic understanding, classification, or summarization is more useful than a
literal pattern.

The current implementation is Gmail-focused and has strong coverage for
cryptocurrency, wallet, recovery, and service evidence. The larger goal is much
broader: make it easy to find the attachment you forgot about, count electronic
transfers over a particular period, reconstruct a subscription history, locate
receipts, understand correspondence, or answer a question about your own data
without handing your entire mailbox to a third party.

GScrape is not a hosted analytics service. It is a personal instrument for
learning from your own records while keeping the account connection, parser,
results, and secrets under your control.

## What it is for

- **Find forgotten things:** locate a filename, invoice, receipt, account notice,
  document, or conversation buried in years of email.
- **Measure your activity:** count transfers, payments, subscriptions, invoices,
  or messages within a defined time window.
- **Search with increasing precision:** begin with a preset, add terms and date
  boundaries, then graduate to custom regular expressions and configuration.
- **Understand your archive:** group evidence, rank likely matches, export
  structured results, and build a personal view of your digital history.
- **Choose your analysis mode:** use local deterministic rules, an optional local
  or approved LLM workflow, or both with the rule-based results as a traceable
  baseline.

## Design principles

1. **Your data stays yours.** Self-host the application and choose where its
   database, reports, credentials, and model calls live.
2. **Presets before complexity.** Common tasks should take seconds; advanced
   users can reach the full power of regex and configuration without changing
   tools.
3. **Explainable by default.** Every match should show why it was selected,
   which rule or model produced it, and what scope was searched.
4. **Secrets are useful, not careless.** Personal parsing sometimes requires
   recording sensitive values. Store them locally with appropriate protection,
   display only partially redacted values in the dashboard, and never leak them
   into logs, exports, prompts, or source control.
5. **Discovery is not recovery.** Finding a wallet-related message is evidence
   gathering, not permission to open attachments or attempt password recovery.

## Current scope and roadmap

Today, GScrape provides a Gmail scanner, focused search categories, defensive
MIME parsing, configurable date ranges, explainable scoring, attachment filename
matching, redacted audit records, adaptive API pacing, and a local dashboard.

The project is intended to grow toward:

- reusable search and extraction presets;
- user-defined regex expressions and advanced configuration files;
- provider adapters beyond Gmail;
- optional LLM classification, extraction, and natural-language queries;
- local model support and explicit provider/model controls;
- saved queries, reusable reports, and richer personal analytics;
- encrypted-at-rest secret storage with dashboard-specific reveal controls.

The LLM layer is intentionally optional. A mailbox should remain useful with no
model, no cloud inference, and no external data transfer.

## Requirements

- Python 3.9 or newer.
- An authorized Google Cloud project with the Gmail API enabled.
- OAuth client credentials configured for the local callback URI.
- Gmail readonly scope:
  `https://www.googleapis.com/auth/gmail.readonly`

GScrape uses the Python standard library for its scanner and dashboard. A
virtual environment is recommended.

## Installation

```bash
git clone https://github.com/DTRHnet/GScrape.git
cd GScrape
python3 -m venv .venv
source .venv/bin/activate
cp .env.example .env
```

Edit `.env` only on the local authorized system. Never commit `.env`, OAuth
tokens, mailbox contents, wallet files, recovery outputs, or sensitive logs.
For a self-hosted deployment, treat the host, filesystem permissions, backups,
and browser session as part of the security boundary.

## Authentication

Use the existing local OAuth flow and complete consent in the browser when
prompted:

```bash
python gscrape.py --authorize --env-file .env
```

The redirect URI must exactly match the URI registered in Google Cloud. API keys
do not grant access to private Gmail messages; mailbox access requires an
authorized OAuth credential.

## Command-line usage

```bash
python gscrape.py --help
```

Run a focused historical search:

```bash
python gscrape.py \
  --categories gourl,wallet,seed,wallet-file,crypto-address,identity,phone \
  --after 2013/01/01 \
  --before 2018/12/31 \
  --max-messages 1000 \
  --env-file .env \
  --log-file email-wallet-search.log \
  --verbose
```

Run one category only:

```bash
python gscrape.py --category wallet-file --env-file .env
```

The CLI is the stable entry point for repeatable scans. Presets and advanced
custom-rule configuration are part of the planned expansion; until those
interfaces are available, use the existing categories and the scanner's current
configuration options shown by `--help`.

Generate a report or export existing redacted records without contacting Gmail:

```bash
python gscrape.py --report-only --report --log-file email-wallet-search.log
python gscrape.py --report-only --export-csv results/matches.csv
```

## Rate limiting and quota handling

Gmail quotas vary by project and user; there is no universal safe request rate.
GScrape uses a conservative local default and bounded adaptive backoff rather
than claiming to control Google's quota.

```bash
python gscrape.py \
  --requests-per-second 1 \
  --rate-limit-max-delay 60 \
  --retries 4 \
  --env-file .env
```

| Option | Default | Allowed range | Purpose |
| --- | ---: | ---: | --- |
| `--requests-per-second` | `1` | `0–10` | Local pacing ceiling; `0` disables proactive pacing |
| `--rate-limit-max-delay` | `60` | `1–300` seconds | Maximum adaptive cooldown |
| `--retries` | `4` | `0–10` | Maximum transient retry count |

The scanner handles rate-limit, backend, and transient HTTP errors, honors
numeric `Retry-After` values, and never retries indefinitely. Google recommends
exponential backoff; see the official [quota](https://developers.google.com/gmail/api/reference/quota)
and [error-handling](https://developers.google.com/gmail/api/guides/handle-errors)
documentation.

## Local dashboard

```bash
python dashboard_server.py
```

Open `http://127.0.0.1:8766/` locally. The search form exposes the same rate
controls as the CLI and provides a review surface for findings, reports, and
account-scoped progress.

Because this is a personal data tool, the dashboard follows a deliberate
display/storage split:

- values needed for an authorized personal workflow may be recorded locally;
- sensitive values are partially redacted in dashboard views and summaries;
- raw secrets must not be placed in operational logs, test output, or exports by
  default;
- reveal actions, when implemented, should be deliberate, local, and auditable;
- dashboard access must remain limited to the self-hosted operator.

Do not expose the dashboard publicly or use it to bypass authentication, MFA,
account selection, or provider access controls.

## Search categories

| Category | Evidence examples |
| --- | --- |
| `gourl` | GoURL, `go url`, punctuated and obfuscated variants |
| `wallet` | Wallet software, exchanges, hardware wallets, blockchain terms |
| `seed` | Recovery phrase, mnemonic, backup phrase, BIP terminology |
| `wallet-file` | `wallet.dat`, `wal*.dat`, keystore and backup filenames |
| `crypto-address` | Candidate Bitcoin, Litecoin, or Ethereum address shapes |
| `identity` | Configured identity spelling variants |
| `phone` | Candidate phone-number formats |

Address-shaped strings and keyword matches are candidates, not proof of
ownership, relevance, or successful recovery.

## Presets, rules, and LLM workflows

GScrape is designed as a progression rather than a single search mode:

```text
preset  →  filters  →  custom regex  →  structured extraction  →  optional LLM
```

A future preset might answer “find bank transfers from 2022,” “locate every
receipt attachment,” or “show messages mentioning a wallet backup.” Advanced
users should be able to override matching with regex expressions, field-specific
rules, scoring weights, date windows, sender constraints, and extraction
configuration.

An LLM can help interpret meaning, normalize varied language, classify messages,
or summarize a result set. It should not silently replace deterministic evidence:
record the model, prompt/configuration version, scope, and confidence alongside
any model-derived result. Sensitive fields should be redacted before remote
inference unless the operator explicitly chooses an approved local or remote
provider.

## Output and audit records

GScrape records the minimum useful metadata for traceability: categories,
evidence source, date, sender, subject, score, query/pass, and message location.
Where a personal workflow requires sensitive values to be retained, they must be
stored only in the local protected data store and shown in the dashboard in
partially redacted form. Logs and routine reports should use `[REDACTED]` or a
safe partial representation instead.

Typical local outputs include:

- `email-wallet-search.log` — append-only redacted audit log
- `results/` — redacted reports and progress records
- `logs/` — local dashboard operational logs
- `dashboard.db` — local dashboard state

Runtime artifacts are ignored by Git where appropriate. Preserve existing
artifacts and never overwrite original wallet or backup files.

## Security and data handling

This project is for authorized, self-hosted mailbox discovery and analysis only.
The fact that a secret may be useful to the owner does not make it safe to print,
transmit, or place in source control.

- Do not search an account without authorization.
- Keep OAuth credentials, access tokens, refresh tokens, API keys, passwords, seed
  phrases, private keys, wallet contents, and recovery output in protected local
  storage only when the personal workflow explicitly requires them.
- Partially redact secrets in dashboard views; never expose the complete value in
  ordinary logs, reports, test output, notifications, or source control.
- Minimize what is retained, restrict filesystem and dashboard access, and protect
  backups of the local data store.
- Do not download, open, extract, or modify wallet attachments during discovery.
- Do not pass discovered material automatically to `recover_wallet.py`.
- Wallet recovery is a separate, explicitly authorized offline workflow.
- Do not commit `.env`, sessions, mailbox contents, wallet files, recovered files,
  local databases, raw exports, or sensitive logs.

See [`AGENTS.md`](AGENTS.md) for the complete operating rules.

## Testing

```bash
python -m py_compile gscrape.py dashboard_server.py test_search_gmail_patterns.py
python -m unittest discover -v
git diff --check
```

Tests use synthetic text and metadata. Do not run a live mailbox scan as a
substitute for the unit suite.

## Versioning and releases

GScrape follows [Semantic Versioning 2.0.0](https://semver.org/):

- **MAJOR** — incompatible behavior or workflow changes
- **MINOR** — backward-compatible features
- **PATCH** — backward-compatible fixes and documentation changes

The current release is **v1.0.0**, the stable foundation for the broader
self-hosted email parsing vision. It contains focused filter coverage, redacted
audit workflows, local dashboard controls, and adaptive Gmail API rate limiting;
presets, advanced rule configuration, provider expansion, and optional LLM
analysis are the direction of future releases.

## Contributing

Keep changes narrowly scoped, preserve the safety boundary, add synthetic tests
for behavior changes, update this README when CLI/UI behavior changes, and run
the full validation commands before opening a pull request.

## License

No explicit license file is currently included. Until one is added, all rights
are reserved by the repository owner; do not redistribute or reuse the code
without permission.