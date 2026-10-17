# GScrape

Utilities for authorized, metadata-focused Gmail discovery of historical
GoURL and cryptocurrency-wallet related messages.

## Safety boundary

This repository intentionally excludes credentials, OAuth sessions, mailbox
contents, wallet files, recovery outputs, logs, databases, and generated
search results. Email discovery does not authorize downloading attachments or
recovering wallet passwords. See `AGENTS.md` for the operating rules.

## Local setup

1. Create a virtual environment with Python 3.
2. Copy `.env.example` to `.env` and fill in authorized local configuration.
3. Review the configured OAuth redirect URI and Gmail read-only scope before
   authorizing access.
4. Run the focused unit tests before using the scanner.

The scanner uses the Python standard library and the existing local OAuth
flow. Do not commit `.env`, tokens, or mailbox-derived artifacts.