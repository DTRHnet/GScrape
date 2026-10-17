# GScrape

> Authorized, metadata-focused Gmail discovery for historical GoURL,
> cryptocurrency-service, wallet-backup, recovery, and identity evidence.

[![Release](https://img.shields.io/badge/release-v1.0.0-2ea44f?style=flat-square)](https://github.com/DTRHnet/GScrape/releases/tag/v1.0.0)
[![Python](https://img.shields.io/badge/python-3.9%2B-3776ab?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Gmail API](https://img.shields.io/badge/API-Gmail%20readonly-ea4335?style=flat-square&logo=gmail&logoColor=white)](https://developers.google.com/gmail/api)
[![Tests](https://img.shields.io/badge/tests-26%20passing-2ea44f?style=flat-square)](#testing)
[![Security](https://img.shields.io/badge/security-redacted%20output-d73a4a?style=flat-square)](#security-and-data-handling)

**Version:** `1.0.0` · **Status:** stable initial release

## Overview

GScrape searches a user-authorized Gmail mailbox for narrowly defined historical
evidence. It uses Gmail search passes, in-memory matching, defensive MIME
parsing, explainable scoring, duplicate suppression, and redacted JSONL audit
records.

The project deliberately separates **email discovery** from **wallet recovery**.
Finding a relevant message never authorizes downloading an attachment, opening a
wallet file, extracting secrets, or attempting password recovery.

## Features

- Focused categories for GoURL, wallets, recovery terms, wallet filenames,
  cryptocurrency address-shaped candidates, identities, and phone numbers.
- Spaced or punctuated GoURL matching and attachment filename metadata matching.
- Historical date filtering with documented inclusive end-date handling.
- Redacted findings and append-only audit records.
- Adaptive Gmail API rate limiting with bounded retries and `Retry-After` support.
- Local-only dashboard for account selection, scan configuration, progress, and
  redacted reporting.
- Synthetic unit tests that never use live mailbox data or real recovery secrets.

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
controls as the CLI. Select exactly one authorized account per search.

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

## Output and audit records

GScrape records only minimum non-secret metadata: categories, evidence source,
date, sender, subject, score, query/pass, and message location. Sensitive values
are redacted as `[REDACTED]`.

Typical local outputs include:

- `email-wallet-search.log` — append-only redacted audit log
- `results/` — redacted reports and progress records
- `logs/` — local dashboard operational logs
- `dashboard.db` — local dashboard state

Runtime artifacts are ignored by Git where appropriate. Preserve existing
artifacts and never overwrite original wallet or backup files.

## Security and data handling

This project is for authorized mailbox discovery only.

- Do not search an account without authorization.
- Do not print or log OAuth credentials, access tokens, refresh tokens, API keys,
  passwords, seed phrases, private keys, wallet contents, or recovery output.
- Do not download, open, extract, or modify wallet attachments during discovery.
- Do not pass discovered material automatically to `recover_wallet.py`.
- Wallet recovery is a separate, explicitly authorized offline workflow.
- Do not commit `.env`, sessions, mailbox contents, wallet files, recovered files,
  or sensitive logs.

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

The current release is **v1.0.0**, the first stable release containing focused
filter coverage, redacted audit workflows, local dashboard controls, and
adaptive Gmail API rate limiting.

## Contributing

Keep changes narrowly scoped, preserve the safety boundary, add synthetic tests
for behavior changes, update this README when CLI/UI behavior changes, and run
the full validation commands before opening a pull request.

## License

No explicit license file is currently included. Until one is added, all rights
are reserved by the repository owner; do not redistribute or reuse the code
without permission.