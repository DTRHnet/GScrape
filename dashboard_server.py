#!/usr/bin/env python3
"""Local-only GScrape dashboard for generic, redacted email discovery workflows."""

from __future__ import annotations

import csv
import hashlib
import base64
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import mimetypes
import os
import re
import selectors
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse, urlencode
import urllib.request
import urllib.error

import gscrape
from core.storage import Database
from core.async_jobs import AsyncJobRunner
from core.providers import DemoProvider, GmailProvider
from core.secrets import LocalFileSecretStore, SecretRef


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "web"
DB_PATH = ROOT / "dashboard.db"
LOG_PATH = ROOT / "email-wallet-search.log"
SCANNER = ROOT / "gscrape.py"
PYTHON = ROOT / ".venv" / "bin" / "python"
SESSION_DIR = ROOT / "sessions"
SECRET_DIR = ROOT / "credentials"
SECRET_STORE = LocalFileSecretStore(SECRET_DIR)
HOST = "127.0.0.1"
PORT = 8766
MAX_QUERY = 2000
LOG_DIR = ROOT / "logs"
LOG_CONFIG_PATH = LOG_DIR / "logging.json"
RESULTS_DIR = ROOT / "results"
ACQUIRED_DIR = ROOT / "acquired-attachments"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff", ".bmp"}
DEFAULT_LOG_CONFIG = {"level": "INFO", "max_bytes": 5 * 1024 * 1024, "backup_count": 5}
METRICS = {"jobs_created": 0, "jobs_completed": 0, "jobs_failed": 0, "jobs_cancelled": 0}
LOGGER = logging.getLogger("gscrape.dashboard")
ACTIVE_LOG_HANDLER = None
CORE_JOB_RUNNER = AsyncJobRunner(str(DB_PATH))
DEMO_PROVIDER = DemoProvider()
GMAIL_PROVIDER = GmailProvider(SCANNER, ROOT, str(PYTHON if PYTHON.exists() else sys.executable), SESSION_DIR)
CORE_PROVIDERS = {DEMO_PROVIDER.provider_id: DEMO_PROVIDER, GMAIL_PROVIDER.provider_id: GMAIL_PROVIDER}


def load_dotenv_file(path=ROOT / ".env"):
    """Load local .env into process env.

    OAuth config keys from the project .env override a stale shell environment so
    Connect always uses the same redirect URI as gmail_authorization_url.
    Other keys are applied only when unset.
    """
    if not path.exists():
        return
    oauth_keys = {
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_REDIRECT_URI",
    }
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        if key in oauth_keys or key not in os.environ:
            os.environ[key] = value


load_dotenv_file()
RETENTION_DAYS = int(os.environ.get("GSCRAPE_RETENTION_DAYS", "30"))
OAUTH_REDIRECT_URI = os.environ.get(
    "GOOGLE_REDIRECT_URI",
    f"http://{HOST}:{PORT}/api/core/oauth/callback",
).strip() or f"http://{HOST}:{PORT}/api/core/oauth/callback"


def configure_logging(level=None, max_bytes=None, backup_count=None):
    """Configure dated, size-rotated dashboard logs without recording secrets."""
    global ACTIVE_LOG_HANDLER
    config = dict(DEFAULT_LOG_CONFIG)
    if LOG_CONFIG_PATH.exists():
        try:
            config.update(json.loads(LOG_CONFIG_PATH.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    if level is not None:
        config["level"] = str(level).upper()
    if max_bytes is not None:
        config["max_bytes"] = int(max_bytes)
    if backup_count is not None:
        config["backup_count"] = int(backup_count)
    if config["level"] not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("unsupported log level")
    if not 100_000 <= config["max_bytes"] <= 50_000_000:
        raise ValueError("max_bytes must be between 100000 and 50000000")
    if not 1 <= config["backup_count"] <= 20:
        raise ValueError("backup_count must be between 1 and 20")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if ACTIVE_LOG_HANDLER:
        LOGGER.removeHandler(ACTIVE_LOG_HANDLER)
        ACTIVE_LOG_HANDLER.close()
    filename = LOG_DIR / (
        f"gscrape-dashboard-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.log"
    )
    ACTIVE_LOG_HANDLER = RotatingFileHandler(
        filename, maxBytes=config["max_bytes"], backupCount=config["backup_count"], encoding="utf-8"
    )
    ACTIVE_LOG_HANDLER.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        "%Y-%m-%dT%H:%M:%S%z",
    ))
    LOGGER.addHandler(ACTIVE_LOG_HANDLER)
    LOGGER.setLevel(getattr(logging, config["level"]))
    LOGGER.propagate = False
    LOG_CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    files = sorted(LOG_DIR.glob("gscrape-dashboard-*.log"), key=lambda path: path.stat().st_mtime, reverse=True)
    for old_file in files[config["backup_count"] + 1:]:
        try:
            old_file.unlink()
        except OSError:
            LOGGER.warning("could not remove old log file")
    return config


def logging_snapshot():
    config = dict(DEFAULT_LOG_CONFIG)
    if LOG_CONFIG_PATH.exists():
        try:
            config.update(json.loads(LOG_CONFIG_PATH.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    files = []
    for path in sorted(LOG_DIR.glob("gscrape-dashboard-*.log"), key=lambda item: item.stat().st_mtime, reverse=True):
        files.append({"name": path.name, "bytes": path.stat().st_size})
    tail = []
    if files:
        try:
            tail = (LOG_DIR / files[0]["name"]).read_text(encoding="utf-8", errors="replace").splitlines()[-100:]
        except OSError:
            tail = []
    return {"config": config, "files": files, "tail": tail}


configure_logging()


def debug_snapshot():
    checks = []
    try:
        connection = db()
        connection.execute("SELECT 1").fetchone()
        connection.close()
        checks.append({"name": "database", "ok": True, "detail": DB_PATH.name})
    except Exception as error:
        checks.append({"name": "database", "ok": False, "detail": str(error)})
    checks.append({"name": "dashboard_http", "ok": True, "detail": f"http://{HOST}:{PORT} (current service)"})
    checks.append({"name": "scanner", "ok": SCANNER.is_file(), "detail": SCANNER.name})
    checks.append({"name": "python", "ok": PYTHON.exists(), "detail": str(PYTHON if PYTHON.exists() else sys.executable)})
    checks.append({"name": "logs_directory", "ok": LOG_DIR.is_dir() and os.access(LOG_DIR, os.W_OK), "detail": str(LOG_DIR)})
    checks.append({"name": "sessions_directory", "ok": SESSION_DIR.is_dir() and os.access(SESSION_DIR, os.W_OK), "detail": str(SESSION_DIR)})
    try:
        result = subprocess.run(
            [str(PYTHON if PYTHON.exists() else sys.executable), str(SCANNER), "--help"],
            cwd=ROOT, text=True, capture_output=True, timeout=20,
        )
        checks.append({"name": "scanner_cli", "ok": result.returncode == 0, "detail": "gscrape --help"})
    except Exception as error:
        checks.append({"name": "scanner_cli", "ok": False, "detail": str(error)})
    return {
        "service": "GScrape",
        "timestamp": now(),
        "overall": all(check["ok"] for check in checks),
        "checks": checks,
        "logging": logging_snapshot(),
    }


def now():
    return datetime.now(timezone.utc).isoformat()


def db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """CREATE TABLE IF NOT EXISTS profiles (
             id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
             query TEXT NOT NULL, regex TEXT, after_date TEXT, before_date TEXT,
             max_messages INTEGER NOT NULL, created_at TEXT NOT NULL,
             updated_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS accounts (
             id TEXT PRIMARY KEY, label TEXT NOT NULL, email TEXT NOT NULL,
             session_file TEXT NOT NULL, status TEXT NOT NULL,
             created_at TEXT NOT NULL, last_used_at TEXT,
             display_name TEXT NOT NULL DEFAULT '', avatar TEXT NOT NULL DEFAULT ''
        )"""
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(accounts)")}
    if "display_name" not in columns:
        connection.execute("ALTER TABLE accounts ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
    if "avatar" not in columns:
        connection.execute("ALTER TABLE accounts ADD COLUMN avatar TEXT NOT NULL DEFAULT ''")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS runs (
             id TEXT PRIMARY KEY, profile_id TEXT, status TEXT NOT NULL,
             query TEXT NOT NULL, category TEXT NOT NULL, started_at TEXT NOT NULL,
             finished_at TEXT, pages INTEGER DEFAULT 0, scanned INTEGER DEFAULT 0,
             listed INTEGER DEFAULT 0, matches INTEGER DEFAULT 0, error TEXT, results_dir TEXT,
             FOREIGN KEY(profile_id) REFERENCES profiles(id)
        )"""
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
    if "results_dir" not in columns:
        connection.execute("ALTER TABLE runs ADD COLUMN results_dir TEXT")
    if "listed" not in columns:
        connection.execute("ALTER TABLE runs ADD COLUMN listed INTEGER DEFAULT 0")
    if "pid" not in columns:
        connection.execute("ALTER TABLE runs ADD COLUMN pid INTEGER")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS run_sequence (
             id INTEGER PRIMARY KEY CHECK (id = 1),
             next_number INTEGER NOT NULL
        )"""
    )
    connection.execute("INSERT OR IGNORE INTO run_sequence(id, next_number) VALUES(1, 1)")
    connection.commit()
    return connection


def core_job_snapshot(job_id=None):
    """Read provider-neutral jobs without exposing configuration snapshots."""
    with Database(DB_PATH) as database:
        if job_id:
            row = database.get_job(job_id)
            rows = [row] if row else []
        else:
            rows = database.connection.execute(
                "SELECT id, account_id, status, created_at, started_at, finished_at, error_code "
                "FROM jobs_v2 ORDER BY created_at DESC LIMIT 100"
            ).fetchall()
        safe_fields = {
            "id", "account_id", "status", "created_at", "started_at", "finished_at", "error_code"
        }
        jobs = [{key: row[key] for key in safe_fields if key in row.keys()} for row in rows]
        events = {}
        for job in jobs:
            events[job["id"]] = [
                dict(event) for event in database.connection.execute(
                    "SELECT event_type, payload_json, created_at FROM job_events "
                    "WHERE job_id=? ORDER BY id DESC LIMIT 100", (job["id"],)
                )
            ]
    return {"jobs": jobs, "events": events}

def cleanup_result_artifacts(max_age_days=30):
    """Remove only old generated result directories."""
    cutoff = datetime.now(timezone.utc).timestamp() - max_age_days * 86400
    removed = 0
    for path in RESULTS_DIR.iterdir() if RESULTS_DIR.is_dir() else []:
        if path.is_dir() and path.stat().st_mtime < cutoff:
            import shutil
            shutil.rmtree(path)
            removed += 1
    return removed


def account_health_snapshot():
    """Return non-secret account connection health for operator review."""
    with Database(DB_PATH) as database:
        rows = database.connection.execute(
            "SELECT id, provider_id, email, label, status, last_verified_at FROM accounts_v2 ORDER BY created_at DESC"
        ).fetchall()
    return [{key: row[key] for key in ("id", "provider_id", "email", "label", "status", "last_verified_at")} for row in rows]


def core_account_snapshot():
    """Return shared control-plane accounts with no credential or token fields."""
    with Database(DB_PATH) as database:
        database.ensure_provider("gmail", "Gmail")
        return [dict(row) for row in database.list_accounts()]


def core_account_id(value):
    """Validate dashboard account identifiers before using them in storage paths."""
    value = str(value or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise ValueError("invalid account id")
    return value


def inspect_legacy_session(path):
    """Inspect a legacy dotenv session without reading or returning secret values."""
    session_path = Path(path).expanduser().resolve()
    try:
        session_path.relative_to(SESSION_DIR.resolve())
    except ValueError:
        return {"present": False, "reason": "outside_session_directory"}
    if not session_path.is_file():
        return {"present": False, "reason": "missing"}
    names = set()
    try:
        for line in session_path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                names.add(stripped.split("=", 1)[0].strip())
    except OSError:
        return {"present": False, "reason": "unreadable"}
    return {
        "present": True,
        "has_refresh_token": "GMAIL_REFRESH_TOKEN" in names,
        "has_access_token": "GMAIL_ACCESS_TOKEN" in names,
        "variables": sorted(name for name in names if name in {"GMAIL_REFRESH_TOKEN", "GMAIL_ACCESS_TOKEN"}),
    }


def gmail_authorization_url(session_id, state, email, code_challenge=None):
    """Build a redirection URL containing OAuth state but never credentials."""
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    redirect_uri = OAUTH_REDIRECT_URI
    if not client_id:
        raise RuntimeError("GOOGLE_CLIENT_ID is required before connecting a Gmail account")
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/gmail.readonly",
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    if email:
        params["login_hint"] = email
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)


class OAuthExchangeError(RuntimeError):
    """Non-secret OAuth exchange failure with a stable machine code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def classify_oauth_http_error(error: urllib.error.HTTPError) -> OAuthExchangeError:
    """Map Google token-endpoint failures to operator-safe codes/messages."""
    detail = gscrape.oauth_error_detail(error)
    body = detail.lower()
    if "redirect_uri_mismatch" in body:
        return OAuthExchangeError(
            "redirect_uri_mismatch",
            f"OAuth redirect URI mismatch. Dashboard expects {OAUTH_REDIRECT_URI}.",
        )
    if "invalid_grant" in body:
        return OAuthExchangeError(
            "invalid_grant",
            "Authorization code was invalid, expired, or already used. Start Connect again.",
        )
    if "invalid_client" in body:
        return OAuthExchangeError(
            "invalid_client",
            "OAuth client ID/secret was rejected. Check GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.",
        )
    if "unauthorized_client" in body:
        return OAuthExchangeError(
            "unauthorized_client",
            "OAuth client is not allowed for this grant. Check the Google Cloud OAuth client type.",
        )
    if "invalid_request" in body and "code_verifier" in body:
        return OAuthExchangeError(
            "pkce_failed",
            "OAuth PKCE verification failed. Start Connect again from Settings.",
        )
    return OAuthExchangeError("token_exchange_failed", f"Google OAuth code exchange failed{detail}")


def exchange_gmail_code(code, client_id, client_secret, redirect_uri, code_verifier):
    if not code:
        raise OAuthExchangeError("missing_code", "Google did not return an authorization code.")
    if not client_id or not client_secret:
        raise OAuthExchangeError(
            "missing_client",
            "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are required for token exchange.",
        )
    if not redirect_uri:
        raise OAuthExchangeError("missing_redirect", "OAuth redirect URI is not configured.")
    if not code_verifier:
        raise OAuthExchangeError("pkce_failed", "OAuth PKCE verifier was missing for this Connect attempt.")
    payload = urlencode({
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": code_verifier,
    }).encode("ascii")
    request = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            tokens = json.load(response)
    except urllib.error.HTTPError as error:
        raise classify_oauth_http_error(error) from error
    except urllib.error.URLError as error:
        raise OAuthExchangeError(
            "network_error",
            "Could not reach Google token endpoint. Check local network connectivity.",
        ) from error
    except ValueError as error:
        raise OAuthExchangeError(
            "invalid_token_response",
            "Google token endpoint returned an unreadable response.",
        ) from error
    if not isinstance(tokens, dict):
        raise OAuthExchangeError("invalid_token_response", "Google token endpoint returned an invalid payload.")
    if not tokens.get("access_token"):
        raise OAuthExchangeError("missing_access_token", "Google OAuth response did not contain an access token.")
    if not tokens.get("refresh_token"):
        raise OAuthExchangeError(
            "missing_refresh_token",
            "Google did not return a refresh token. Revoke GScrape access at "
            "https://myaccount.google.com/permissions then Connect again with consent.",
        )
    return tokens


def write_session_env(path: Path, tokens: dict, email: str = "") -> None:
    """Persist non-ambient OAuth values into a scanner session env file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    values = {
        "GOOGLE_CLIENT_ID": os.environ.get("GOOGLE_CLIENT_ID", "").strip(),
        "GOOGLE_CLIENT_SECRET": os.environ.get("GOOGLE_CLIENT_SECRET", "").strip(),
        "GOOGLE_REDIRECT_URI": OAUTH_REDIRECT_URI,
        "GMAIL_REFRESH_TOKEN": str(tokens.get("refresh_token") or ""),
    }
    if tokens.get("access_token"):
        values["GMAIL_ACCESS_TOKEN"] = str(tokens["access_token"])
    if email:
        values["GMAIL_ACCOUNT_EMAIL"] = email
    lines = [f'{key}="{value}"' for key, value in values.items() if value]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def persist_oauth_tokens(account_v2_id: str, email: str, tokens: dict) -> list[str]:
    """Store tokens in the secret store and sync every matching scanner session file."""
    synced = []
    ref = SECRET_STORE.put_json({
        "refresh_token": tokens.get("refresh_token"),
        "access_token": tokens.get("access_token"),
        "scope": tokens.get("scope", ""),
        "token_type": tokens.get("token_type", ""),
        "expires_in": tokens.get("expires_in"),
    })
    with Database(DB_PATH) as database:
        database.upsert_credential(account_v2_id, ref.id, str(tokens.get("scope") or "").split())
        database.update_account_health(account_v2_id, "connected")
    core_session = SESSION_DIR / f"{account_v2_id}.env"
    write_session_env(core_session, tokens, email)
    synced.append(str(core_session))
    connection = db()
    legacy_rows = connection.execute(
        "SELECT id, session_file FROM accounts WHERE lower(email)=lower(?)",
        (email,),
    ).fetchall()
    for row in legacy_rows:
        session_path = Path(row["session_file"])
        # Keep scanner sessions inside this workspace even if a row has a stale path.
        if SESSION_DIR.resolve() not in session_path.resolve().parents:
            session_path = SESSION_DIR / f"{row['id']}.env"
            connection.execute(
                "UPDATE accounts SET session_file=?, status=?, last_used_at=? WHERE id=?",
                (str(session_path), "connected", now(), row["id"]),
            )
        else:
            connection.execute(
                "UPDATE accounts SET status=?, last_used_at=? WHERE id=?",
                ("connected", now(), row["id"]),
            )
        write_session_env(session_path, tokens, email)
        synced.append(str(session_path))
    connection.commit()
    connection.close()
    return synced


def allocate_run_id(connection):
    """Atomically allocate a human-friendly, never-reused six-digit run ID."""
    connection.execute("BEGIN IMMEDIATE")
    row = connection.execute("SELECT next_number FROM run_sequence WHERE id=1").fetchone()
    next_number = int(row[0]) if row else 1
    numeric_ids = [
        int(item[0]) for item in connection.execute(
            "SELECT id FROM runs WHERE id GLOB '[0-9]*' AND length(id) <= 18"
        )
        if str(item[0]).isdigit()
    ]
    if next_number <= max(numeric_ids, default=0):
        next_number = max(numeric_ids) + 1
    connection.execute("UPDATE run_sequence SET next_number=? WHERE id=1", (next_number + 1,))
    return f"{next_number:06d}"


def reserve_run_directory(run_id, attempts=5):
    """Reserve a result directory, advancing past stale filesystem collisions."""
    candidate = int(run_id)
    for _ in range(attempts):
        path = RESULTS_DIR / f"{candidate:06d}"
        try:
            path.mkdir(parents=True, exist_ok=False)
            return f"{candidate:06d}", path
        except FileExistsError:
            candidate += 1
    raise RuntimeError("could not reserve a unique result directory")


def write_run_artifacts(run_id, run_row, records, status, error=None):
    """Write a redacted, self-contained result bundle for one run."""
    run_dir = RESULTS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    summary = gscrape.summarize_matches(records)
    public_records = [gscrape.public_match_record(record) for record in records]
    with Database(DB_PATH) as database:
        from core.search import result_from_match
        for record in public_records:
            if record.get("message_id") or record.get("id"):
                database.persist_normalized_result(result_from_match(record).__dict__)
    phone_records = [record for record in public_records if "phone" in record.get("categories", [])]
    crypto_categories = {"gourl", "wallet", "wallet-file", "seed", "crypto-address"}
    crypto_records = [
        record for record in public_records
        if crypto_categories.intersection(record.get("categories", []))
    ]
    safe_error = "The scanner did not complete successfully." if error else ""

    lines = [
        f"# GScrape Run {run_id}", "",
        f"- Run ID: `{run_id}`",
        f"- Status: **{status}**",
        f"- Started: {run_row['started_at']}",
        f"- Finished: {run_row['finished_at'] or 'in progress'}",
        f"- Category: `{run_row['category']}`",
        f"- Query: {run_row['query'] or 'preset discovery passes'}",
        f"- Messages scanned: {run_row['scanned'] or 0}",
        f"- Messages returned by Gmail: {run_row['listed'] or 0}",
        f"- Pages scanned: {run_row['pages'] or 0}",
        f"- Redacted findings: {summary['total_matches']}",
        f"- Unique message IDs: {summary['unique_message_ids']}",
        f"- Detailed scan log: `SCAN.log`",
        "",
        "## Category breakdown", "",
    ]
    lines.extend(f"- `{category}`: {count}" for category, count in summary["category_counts"].items())
    if safe_error:
        lines.extend(["", "## Completion note", "", safe_error])
    lines.extend([
        "", "This bundle contains redacted metadata only. Message bodies, credentials, tokens, "
        "wallet secrets, and private keys are never written here.",
    ])
    (run_dir / "SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    phone_lines = [f"# Phone Numbers — Run {run_id}", "",
                   "Phone values are intentionally not written to result files.",
                   "The entries below identify redacted metadata findings for review.", ""]
    for record in phone_records:
        phone_lines.append(
            f"- {record.get('message_date', '')} — {record.get('sender', '')} — "
            f"{record.get('subject', '')} — {record.get('message_location', '')}"
        )
    if not phone_records:
        phone_lines.append("No phone-number category findings were recorded.")
    (run_dir / "PHONE_NUMBERS.md").write_text("\n".join(phone_lines) + "\n", encoding="utf-8")
    (run_dir / "CRYPTO.json").write_text(
        json.dumps(crypto_records, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    gscrape.export_matches_csv(records, run_dir / "MATCHES.csv")
    return run_dir


def json_response(handler, payload, status=200):
    raw = json.dumps(payload, ensure_ascii=False).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def html_response(handler, body, status=200):
    raw = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def oauth_result_page(*, ok, title, message, account_id="", error_code=""):
    """Browser-facing OAuth result that returns control to the dashboard."""
    params = {"view": "settings", "oauth": "connected" if ok else "failed"}
    if account_id:
        params["account_id"] = account_id
    if not ok and error_code:
        params["oauth_error"] = error_code
    if not ok and message:
        # Keep operator-safe guidance in the hash so the UI can show it without
        # stuffing long text into a logged query string.
        params["oauth_message"] = message[:240]
    destination = "/?" + urlencode(params)
    status_class = "ok" if ok else "err"
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{escape(title)}</title>
<meta http-equiv="refresh" content="1;url={destination}">
<style>
body{{font-family:system-ui,sans-serif;background:#111;color:#eee;display:grid;place-items:center;min-height:100vh;margin:0}}
main{{max-width:28rem;padding:1.5rem;border:1px solid #333;border-radius:12px;background:#1a1a1a}}
.{status_class}{{color:{"#8f8" if ok else "#f88"}}}
a{{color:#9cf}}
</style></head><body><main>
<h1 class="{status_class}">{escape(title)}</h1>
<p>{escape(message)}</p>
<p><a href="{destination}">Return to GScrape settings</a></p>
<script>setTimeout(function(){{ location.replace({json.dumps(destination)}); }}, 600);</script>
</main></body></html>"""


def read_json(handler):
    try:
        length = min(int(handler.headers.get("Content-Length", "0")), 100_000)
        return json.loads(handler.rfile.read(length) or b"{}")
    except (ValueError, json.JSONDecodeError):
        raise ValueError("request body must be valid JSON")


def validate_config(data):
    category = str(data.get("category", "all"))
    # The dashboard's canonical control is the Attachments discovery section.
    # Keep the explicit field for API/legacy clients, but derive the effective
    # value from either spelling so older clients remain compatible.
    attachment_discovery = bool(data.get("attachment_discovery", False))
    raw_domains = data.get("domains", [])
    if isinstance(raw_domains, str):
        raw_domains = raw_domains.split(",")
    domains = gscrape.normalize_search_domains(raw_domains)
    allowed = {"all", *gscrape.CATEGORY_PATTERNS}
    if category not in allowed:
        raise ValueError("unsupported category")
    raw_categories = data.get("categories")
    if raw_categories is None:
        categories = [] if attachment_discovery else (list(gscrape.CATEGORY_PATTERNS) if category == "all" else [category])
    elif isinstance(raw_categories, list):
        categories = [str(item).strip() for item in raw_categories if str(item).strip()]
    else:
        categories = [item.strip() for item in str(raw_categories).split(",") if item.strip()]
    if "attachments" in categories:
        categories = [item for item in categories if item != "attachments"]
        attachment_discovery = True
    if not categories and not attachment_discovery:
        raise ValueError("select at least one discovery category")
    if any(item not in gscrape.CATEGORY_PATTERNS for item in categories):
        raise ValueError("unsupported discovery category")
    regex_overrides = data.get("category_regex") or {}
    if not isinstance(regex_overrides, dict):
        raise ValueError("category_regex must be an object")
    for key, expression in regex_overrides.items():
        if key not in gscrape.CATEGORY_PATTERNS:
            raise ValueError(f"unsupported category regex: {key}")
        if expression:
            try:
                re.compile(str(expression))
            except re.error as error:
                raise ValueError(f"invalid regex for {key}: {error}") from error
    query = str(data.get("query", "")).strip()
    if len(query) > MAX_QUERY:
        raise ValueError("query is too long")
    regex = str(data.get("regex", "")).strip()
    if regex:
        try:
            re.compile(regex)
        except re.error as error:
            raise ValueError(f"invalid regex: {error}") from error
    raw_extensions = data.get("attachment_extensions", [])
    if isinstance(raw_extensions, str):
        raw_extensions = raw_extensions.split(",")
    try:
        attachment_extensions = gscrape.normalize_attachment_extensions(raw_extensions)
    except ValueError as error:
        raise ValueError(str(error)) from error
    attachment_filename_regex = str(data.get("attachment_filename_regex", "")).strip()
    contact_email_regex = str(data.get("contact_email_regex", "")).strip()
    contact_email_domain = str(data.get("contact_email_domain", "")).strip()
    if contact_email_regex:
        try:
            re.compile(contact_email_regex, re.I)
        except re.error as error:
            raise ValueError(f"invalid contact email regex: {error}") from error
    if contact_email_domain and not re.fullmatch(r"[A-Za-z0-9.-]+", contact_email_domain):
        raise ValueError("invalid contact email domain")
    if attachment_discovery and not categories:
        categories = []
    if not categories and not attachment_discovery:
        raise ValueError("select at least one discovery category or enable attachment discovery")
    if not attachment_discovery:
        attachment_extensions = []
        attachment_filename_regex = ""
    if attachment_filename_regex:
        try:
            re.compile(attachment_filename_regex, re.I)
        except re.error as error:
            raise ValueError(f"invalid attachment filename regex: {error}") from error
    # Discovery and acquisition are separate: bytes require an explicit opt-in.
    acquire_attachments = bool(data.get("acquire_attachments", False))
    if acquire_attachments and not attachment_discovery:
        raise ValueError("attachment acquisition requires attachment discovery")
    # Blank / missing max_messages means "no practical limit" (ceiling), not int("").
    raw_max_messages = data.get("max_messages", None)
    if isinstance(raw_max_messages, str):
        raw_max_messages = raw_max_messages.strip()
    if raw_max_messages in (None, "", 0, "0"):
        max_messages = 10_000
    else:
        try:
            max_messages = int(raw_max_messages)
        except (TypeError, ValueError) as error:
            raise ValueError("max_messages must be an integer between 1 and 10000") from error
    if not 1 <= max_messages <= 10_000:
        raise ValueError("max_messages must be between 1 and 10000")
    try:
        raw_rps = data.get("requests_per_second", 1.0)
        requests_per_second = 1.0 if raw_rps in (None, "") else float(raw_rps)
    except (TypeError, ValueError) as error:
        raise ValueError("requests_per_second must be between 0 and 10") from error
    if not 0 <= requests_per_second <= 10:
        raise ValueError("requests_per_second must be between 0 and 10")
    try:
        raw_cooldown = data.get("rate_limit_max_delay", 60.0)
        rate_limit_max_delay = 60.0 if raw_cooldown in (None, "") else float(raw_cooldown)
    except (TypeError, ValueError) as error:
        raise ValueError("rate_limit_max_delay must be between 1 and 300") from error
    if not 1 <= rate_limit_max_delay <= 300:
        raise ValueError("rate_limit_max_delay must be between 1 and 300")
    # Date bounds apply only when the operator sets them. Empty fields mean
    # unrestricted search — do not silently inject the historical window.
    after_date = str(data.get("after_date") or "").strip()
    before_date = str(data.get("before_date") or "").strip()
    if after_date:
        gscrape.normalize_gmail_date(after_date)
    if before_date:
        gscrape.normalize_gmail_date(before_date, inclusive_end=True)
    return {
        "account_id": str(data.get("account_id", "")).strip(),
        "category": category,
        "categories": categories,
        "category_regex": {str(k): str(v) for k, v in regex_overrides.items() if str(v).strip()},
        "query": query,
        "regex": regex,
        "attachment_extensions": attachment_extensions,
        "attachment_filename_regex": attachment_filename_regex,
        "contact_email_regex": contact_email_regex,
        "contact_email_domain": contact_email_domain,
        "attachment_discovery": attachment_discovery,
        "acquire_attachments": acquire_attachments,
        "domains": domains or gscrape.domains_for_categories(categories, attachment_discovery),
        "after_date": after_date,
        "before_date": before_date,
        "max_messages": max_messages,
        "requests_per_second": requests_per_second,
        "rate_limit_max_delay": rate_limit_max_delay,
    }


def account_initials(name):
    parts = re.findall(r"[A-Za-z0-9]+", name.strip())
    if not parts:
        return "?"
    return "".join(part[0] for part in parts[:2]).upper()


def scanner_env():
    """Child env for gscrape that cannot inherit ambient mailbox tokens."""
    env = os.environ.copy()
    for key in (
        "GMAIL_REFRESH_TOKEN",
        "GMAIL_ACCESS_TOKEN",
        "GMAIL_ACCOUNT_EMAIL",
        "GMAIL_USER",
    ):
        env.pop(key, None)
    return env


def account_is_connected(row):
    session = Path(row["session_file"])
    if not session.exists() or "GMAIL_REFRESH_TOKEN" not in session.read_text(errors="ignore"):
        return False
    try:
        result = subprocess.run(
            [str(PYTHON if PYTHON.exists() else sys.executable), str(SCANNER),
             "--profile-check", "--env-file", str(session)],
            cwd=ROOT, text=True, capture_output=True, timeout=120, env=scanner_env(),
        )
        if result.returncode:
            return False
        profile = json.loads(result.stdout)
        return str(profile.get("emailAddress", "")).strip().casefold() == str(row["email"] or "").strip().casefold()
    except (OSError, subprocess.SubprocessError, TypeError, ValueError, json.JSONDecodeError):
        return False


def list_rows(table, limit=100):
    connection = db()
    rows = [dict(row) for row in connection.execute(
        f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT ?", (limit,)
    )]
    connection.close()
    return rows


def progress_path(run_id):
    return RESULTS_DIR / run_id / "PROGRESS.jsonl"


def load_progress_records(run_id):
    """Load the latest redacted progress snapshot for each matched message."""
    path = progress_path(run_id)
    latest = {}
    if not path.exists():
        return []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if payload.get("event") != "gmail-crypto-progress-match":
            continue
        record = payload.get("record")
        if not isinstance(record, dict):
            continue
        key = record.get("message_location") or json.dumps(record, sort_keys=True)
        latest[key] = record
    return list(latest.values())


def load_run_records(run_id):
    """Merge durable audit records with live progress snapshots."""
    records = [
        record for record in gscrape.load_match_records(LOG_PATH)
        if record.get("run_id") == run_id
    ]
    merged = {}
    for record in records + load_progress_records(run_id):
        key = record.get("message_location") or json.dumps(record, sort_keys=True)
        merged[key] = record
    return list(merged.values())


def acquired_run_dir(run_id: str) -> Path:
    # Dashboard scans explicitly store acquisitions in the run's result bundle.
    # Keep the legacy directory as a fallback for older CLI runs only.
    result_dir = RESULTS_DIR / run_id / "attachments"
    return result_dir if result_dir.is_dir() else ACQUIRED_DIR / run_id


def resolve_acquired_attachment(run_id: str, stored_name: str) -> Path | None:
    """Resolve a stored attachment path under the run's acquire directory only."""
    name = Path(unquote(stored_name or "")).name
    if not name or name in {".", ".."} or "/" in stored_name or "\\" in stored_name:
        return None
    root = acquired_run_dir(run_id).resolve()
    path = (root / name).resolve()
    if not path.is_file():
        # A content-hash deduplication hit may refer to an artifact in another
        # run. Search only result attachment directories, never arbitrary paths.
        for candidate_root in RESULTS_DIR.glob("*/attachments"):
            candidate = (candidate_root / name).resolve()
            try:
                candidate.relative_to(candidate_root.resolve())
            except ValueError:
                continue
            if candidate.is_file():
                path, root = candidate, candidate_root.resolve()
                break
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path if path.is_file() else None


def enrich_match_for_dashboard(run_id: str, record: dict) -> dict:
    """Attach download/thumbnail URLs for acquired files without exposing bytes."""
    payload = gscrape.public_match_record(record)
    acquired = []
    for item in payload.get("acquired_attachments") or []:
        stored = str(item.get("stored_name") or "")
        entry = dict(item)
        if stored and resolve_acquired_attachment(run_id, stored):
            entry["download_url"] = f"/api/runs/{run_id}/attachments/{stored}"
            suffix = Path(stored).suffix.lower()
            if suffix in IMAGE_SUFFIXES or Path(str(item.get("filename") or "")).suffix.lower() in IMAGE_SUFFIXES:
                entry["thumbnail_url"] = f"/api/runs/{run_id}/attachments/{stored}?inline=1"
                entry["is_image"] = True
        acquired.append(entry)
    if acquired:
        payload["acquired_attachments"] = acquired
    return payload


def load_all_run_records():
    """Load durable and in-flight redacted records across the workspace."""
    merged = {}
    for record in gscrape.load_match_records(LOG_PATH):
        key = (record.get("run_id"), record.get("message_location"))
        merged[key] = record
    for row in list_rows("runs", 1000):
        for record in load_progress_records(row["id"]):
            key = (row["id"], record.get("message_location"))
            merged[key] = record
    return list(merged.values())


def persist_progress_records(run_id, records):
    """Promote progress-only redacted records into the append-only audit log."""
    existing = [
        record for record in gscrape.load_match_records(LOG_PATH)
        if record.get("run_id") == run_id
    ]
    existing_keys = {record.get("message_location") for record in existing}
    for record in records:
        key = record.get("message_location")
        if key and key in existing_keys:
            continue
        payload = dict(record)
        payload.update({"event": "gmail-crypto-discovery-match", "run_id": run_id})
        gscrape.append_record(LOG_PATH, payload)
        if key:
            existing_keys.add(key)


PROGRESS_RE = re.compile(
    r"\[search\].*?page=(?P<page>\d+).*?returned=(?P<returned>\d+)"
    r".*?fetched=(?P<fetched>\d+).*?matches=(?P<matches>\d+)"
)


def update_run_progress(connection, run_id, line):
    """Update dashboard counters from the scanner's redacted progress line."""
    match = PROGRESS_RE.search(line)
    if not match:
        return
    values = {name: int(value) for name, value in match.groupdict().items()}
    connection.execute(
        """UPDATE runs SET pages=?, listed=?, scanned=?, matches=?
           WHERE id=? AND status='running'""",
        (values["page"], values["returned"], values["fetched"], values["matches"], run_id),
    )
    connection.commit()


def reconcile_scan_counter(payload, current, key):
    """Keep the largest trustworthy counter seen during a partial scan.

    A scanner may emit a category-level audit payload after earlier progress
    lines.  Those payloads can legitimately contain zero for a category even
    though the dashboard already observed work in previous pages.  Never
    replace durable progress with such a regression.
    """
    current_key = {
        "pages_scanned": "pages",
        "messages_scanned": "scanned",
        "messages_returned": "listed",
        "matches": "matches",
    }.get(key, key)
    try:
        reported = int(payload.get(key, 0) or 0)
    except (TypeError, ValueError):
        reported = 0
    try:
        observed = int(current[current_key] or 0)
    except (TypeError, ValueError, KeyError):
        observed = 0
    return max(reported, observed)


def recover_interrupted_runs(message: str = "scan interrupted by dashboard restart") -> int:
    """Mark queued/running rows failed after a process restart (daemon threads die)."""
    connection = db()
    cursor = connection.execute(
        """UPDATE runs
           SET status='failed', finished_at=?, error=?
           WHERE status IN ('queued', 'running')""",
        (now(), message[:1000]),
    )
    connection.commit()
    count = cursor.rowcount
    connection.close()
    if count:
        LOGGER.warning("recovered %d interrupted run(s)", count)
    return count


def run_scan(run_id, config):
    LOGGER.info("scan started: %s", run_id)
    connection = db()
    connection.execute("UPDATE runs SET status=? WHERE id=?", ("running", run_id))
    connection.commit()
    args = [
        str(PYTHON if PYTHON.exists() else sys.executable), str(SCANNER),
        "--category", config["category"],
        "--categories", ",".join(config["categories"]),
        "--max-messages", str(config["max_messages"]),
        "--run-id", run_id,
        "--log-file", str(LOG_PATH),
        "--progress-file", str(progress_path(run_id)),
        "--verbose",
        "--requests-per-second", str(config["requests_per_second"]),
        "--rate-limit-max-delay", str(config["rate_limit_max_delay"]),
    ]
    if config.get("attachment_discovery"):
        args.append("--attachment-discovery")
    # Acquisition is explicit and isolated to this run's result directory.
    if config.get("acquire_attachments"):
        args.extend(["--acquire-attachments", "--capture-mode", "acquired",
                     "--attachment-output-dir", str(RESULTS_DIR / run_id / "attachments")])
    for domain in config.get("domains", []):
        args.extend(["--domain", domain])
    if config.get("attachment_extensions"):
        args.extend(["--attachment-extensions", ",".join(config["attachment_extensions"])])
    if config.get("attachment_filename_regex"):
        args.extend(["--attachment-filename-regex", config["attachment_filename_regex"]])
    if config.get("category_regex"):
        args.extend(["--category-regex", json.dumps(config["category_regex"])])
    if config.get("after_date"):
        args.extend(["--after", config["after_date"]])
    if config.get("before_date"):
        args.extend(["--before", config["before_date"]])
    if config.get("account_id"):
        account_connection = db()
        account = account_connection.execute(
            "SELECT * FROM accounts WHERE id=?", (config["account_id"],)
        ).fetchone()
        account_connection.close()
        if account:
            profile_result = subprocess.run(
                [str(PYTHON if PYTHON.exists() else sys.executable), str(SCANNER),
                 "--profile-check", "--env-file", account["session_file"]],
                cwd=ROOT, text=True, capture_output=True, timeout=120, env=scanner_env(),
            )
            if profile_result.returncode:
                raise RuntimeError("Gmail session profile verification failed")
            try:
                profile = json.loads(profile_result.stdout)
            except (TypeError, ValueError) as error:
                raise RuntimeError("Gmail session profile verification returned invalid data") from error
            actual = str(profile.get("emailAddress", "")).strip().casefold()
            expected = str(account["email"] or "").strip().casefold()
            if not actual or actual != expected:
                raise RuntimeError("Gmail session identity does not match the selected account")
            args.extend(["--env-file", account["session_file"]])
    if config["query"]:
        args.extend(["--query", config["query"]])
    scan_log_path = RESULTS_DIR / run_id / "SCAN.log"
    scan_log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with scan_log_path.open("w", encoding="utf-8") as scan_log:
            scan_log.write(f"run_id={run_id}\n")
            scan_log.write(f"command_category={config['category']}\n")
            scan_log.write(f"selected_categories={','.join(config['categories'])}\n")
            if config.get("attachment_discovery"):
                scan_log.write(
                    "attachment_discovery_query="
                    f"{gscrape.attachment_discovery_query(config.get('attachment_extensions') or [])}\n"
                )
            # Redacted argv for audit: omit env-file path contents, keep flags.
            safe_args = []
            skip_next = False
            for index, part in enumerate(args):
                if skip_next:
                    skip_next = False
                    safe_args.append("<session>")
                    continue
                if part == "--env-file":
                    safe_args.append(part)
                    skip_next = True
                    continue
                safe_args.append(part)
            scan_log.write("argv=" + " ".join(safe_args) + "\n")
            scan_log.flush()
            process = subprocess.Popen(
                args,
                cwd=ROOT,
                text=True,
                bufsize=1,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=scanner_env(),
            )
            connection.execute("UPDATE runs SET pid=? WHERE id=?", (process.pid, run_id))
            connection.commit()
            # The scanner emits one redacted progress line per Gmail page.
            # Stream it into the log and database so the UI reflects work
            # while the child process is still running.
            progress_selector = selectors.DefaultSelector()
            if process.stdout:
                progress_selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + 3600
            while process.poll() is None or progress_selector.get_map():
                if time.monotonic() >= deadline:
                    process.kill()
                    raise TimeoutError("scan exceeded the one-hour runtime limit")
                ready = progress_selector.select(timeout=1)
                if not ready and process.poll() is not None:
                    break
                for key, _ in ready:
                    line = key.fileobj.readline()
                    if not line:
                        progress_selector.unregister(key.fileobj)
                        continue
                    scan_log.write(line)
                    scan_log.flush()
                    update_run_progress(connection, run_id, line)
            result_code = process.wait()
            progress_selector.close()
        payload = {}
        scan_output = scan_log_path.read_text(encoding="utf-8", errors="replace")
        for line in reversed(scan_output.splitlines()):
            try:
                payload = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
        current = connection.execute(
            "SELECT pages, scanned, listed, matches FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        current_status = connection.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()[0]
        status = "cancelled" if current_status == "cancelled" else ("completed" if result_code == 0 else "failed")
        scanner_error = next(
            (
                line.strip() for line in reversed(scan_output.splitlines())
                if "Gmail API request failed" in line
                or "re-authorize the account" in line
                or "Gmail API access" in line
            ),
            "",
        )
        error = None if result_code == 0 else (
            scanner_error[:1000] if scanner_error
            else "scanner failed; inspect results/{}/SCAN.log".format(run_id)
        )
        connection.execute(
            "UPDATE runs SET status=?, finished_at=?, pages=?, scanned=?, listed=?, matches=?, error=? WHERE id=?",
            (status, now(), reconcile_scan_counter(payload, current, "pages_scanned"),
             reconcile_scan_counter(payload, current, "messages_scanned"),
             reconcile_scan_counter(payload, current, "messages_returned"),
             reconcile_scan_counter(payload, current, "matches"), error, run_id),
        )
    except Exception as error:
        LOGGER.exception("scan failed: %s", run_id)
        connection.execute(
            "UPDATE runs SET status=?, finished_at=?, error=? WHERE id=?",
            ("cancelled" if connection.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()[0] == "cancelled" else "failed", now(), str(error)[:1000], run_id),
        )
    connection.commit()
    row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    records = load_run_records(run_id)
    persist_progress_records(run_id, records)
    write_run_artifacts(run_id, row, records, row["status"], row["error"])
    if row["status"] == "completed":
        generate_configured_llm_report(run_id, records)
    connection.close()
    LOGGER.info("scan finished: %s (%s)", run_id, status if "status" in locals() else "failed")


def generate_configured_llm_report(run_id, records):
    """Generate a local report from redacted metadata when explicitly enabled."""
    if os.environ.get("GSCRAPE_LLM_ENABLED", "").lower() not in {"1", "true", "yes", "on"}:
        return None
    endpoint = os.environ.get("LLM_API_BASE", "https://api.openai.com/v1")
    model = os.environ.get("LLM_MODEL", "gpt-5.6-luna")
    api_key = os.environ.get(os.environ.get("LLM_API_KEY_ENV", "OPENAI_API_KEY"), "")
    if not api_key:
        LOGGER.warning("LLM report skipped for %s: configured API key environment variable is empty", run_id)
        return None
    payload = gscrape.build_llm_analysis_payload(records, "all")
    try:
        analysis = gscrape.analyze_with_llm(payload, endpoint, api_key, model)
        output_dir = ROOT / os.environ.get("LLM_OUTPUT_DIR", "llm-reports")
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{run_id}.md"
        output_path.write_text(
            f"# LLM analysis for run {run_id}\n\n{analysis.strip()}\n",
            encoding="utf-8",
        )
        LOGGER.info("LLM report written for %s", run_id)
        return output_path
    except (OSError, RuntimeError, ValueError) as error:
        LOGGER.warning("LLM report failed for %s: %s", run_id, error)
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "GScrape/1.4"

    def log_message(self, *_):
        return

    def send_file(self, path, content_type):
        if not path.is_file():
            self.send_error(404)
            return
        raw = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        parsed = urlparse(self.path)
        parsed = parsed._replace(path=parsed.path.rstrip("/") or "/")
        if parsed.path == "/":
            return self.send_file(STATIC / "index.html", "text/html; charset=utf-8")
        run_page = re.fullmatch(r"/run/([A-Za-z0-9_-]{1,100})", parsed.path)
        if run_page:
            return self.send_file(STATIC / "index.html", "text/html; charset=utf-8")
        if parsed.path.startswith("/assets/"):
            suffix = Path(parsed.path).suffix
            types = {".js": "text/javascript", ".css": "text/css"}
            return self.send_file(STATIC / parsed.path.removeprefix("/"), types.get(suffix, "text/plain"))
        attachment_match = re.fullmatch(r"/api/runs/([A-Za-z0-9_-]{1,100})/attachments/([A-Za-z0-9._-]{1,220})", parsed.path)
        if attachment_match:
            run_id, stored_name = attachment_match.groups()
            attachment_path = resolve_acquired_attachment(run_id, stored_name)
            if attachment_path is None:
                return self.send_error(404)
            suffix = attachment_path.suffix.lower()
            content_types = {
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
                ".svg": "image/svg+xml", ".pdf": "application/pdf",
            }
            return self.send_file(attachment_path, content_types.get(suffix, "application/octet-stream"))
        try:
            if parsed.path == "/api/status":
                return json_response(self, {
                    "ok": True,
                    "service": "GScrape",
                    "scanner": SCANNER.name,
                    "local_only": True,
                    "database": str(DB_PATH.name),
                    "version": "1.2.0",
                })
            if parsed.path == "/api/profiles":
                return json_response(self, {"profiles": list_rows("profiles")})
            if parsed.path == "/api/accounts":
                connection = db()
                accounts = []
                for row in connection.execute("SELECT * FROM accounts ORDER BY created_at DESC"):
                    item = dict(row)
                    if account_is_connected(item):
                        item["status"] = "connected"
                    item["display_name"] = item.get("display_name") or item["label"]
                    item["avatar"] = item.get("avatar") or account_initials(item["display_name"])
                    item.pop("session_file", None)
                    accounts.append(item)
                connection.close()
                return json_response(self, {"accounts": accounts})
            if parsed.path == "/api/core/accounts":
                accounts = core_account_snapshot()
                for account in accounts:
                    account["legacy_session"] = inspect_legacy_session(SESSION_DIR / f"{account['id']}.env")
                return json_response(self, {"accounts": accounts})
            if parsed.path.startswith("/api/core/accounts/") and parsed.path.endswith("/connect"):
                account_id = core_account_id(parsed.path.split("/")[4])
                with Database(DB_PATH) as database:
                    row = database.connection.execute("SELECT email FROM accounts_v2 WHERE id=?", (account_id,)).fetchone()
                    if row is None:
                        return json_response(self, {"error": "account not found"}, 404)
                    raw_state = secrets.token_urlsafe(32)
                    state_hash = hashlib.sha256(raw_state.encode("utf-8")).hexdigest()
                    verifier = secrets.token_urlsafe(64)
                    challenge = base64.urlsafe_b64encode(
                        hashlib.sha256(verifier.encode("utf-8")).digest()
                    ).rstrip(b"=").decode("ascii")
                    verifier_ref = SECRET_STORE.put_json({"pkce_verifier": verifier})
                    session_id = database.create_oauth_session(
                        "gmail",
                        state_hash,
                        OAUTH_REDIRECT_URI,
                        datetime.fromtimestamp(time.time() + 600, timezone.utc).isoformat(),
                        row["email"],
                        hashlib.sha256(verifier.encode("utf-8")).hexdigest(),
                        verifier_ref.id,
                    )
                    database.update_account_health(account_id, "authorizing")
                return json_response(self, {
                    "session_id": session_id,
                    "authorization_url": gmail_authorization_url(session_id, raw_state, row["email"], challenge),
                    "status": "authorizing",
                    "redirect_uri": OAUTH_REDIRECT_URI,
                })
            if parsed.path == "/api/core/oauth/callback":
                query = parse_qs(parsed.query)
                wants_html = "text/html" in (self.headers.get("Accept") or "")
                state = query.get("state", [""])[0]
                if query.get("error"):
                    denied = str(query["error"][0])
                    LOGGER.warning("oauth denied code=%s", denied)
                    if wants_html:
                        return html_response(
                            self,
                            oauth_result_page(
                                ok=False,
                                title="Authorization denied",
                                message="Google did not grant access. Return to Settings and try Connect again.",
                            ),
                            400,
                        )
                    return json_response(self, {"error": "OAuth authorization denied", "code": denied}, 400)
                row = None
                with Database(DB_PATH) as database:
                    row = database.consume_oauth_state(hashlib.sha256(state.encode()).hexdigest()) if state else None
                    if row is None:
                        if wants_html:
                            return html_response(
                                self,
                                oauth_result_page(
                                    ok=False,
                                    title="Authorization expired",
                                    message="This OAuth session is invalid or expired. Return to Settings and click Connect again.",
                                ),
                                400,
                            )
                        return json_response(self, {"error": "invalid or expired OAuth state"}, 400)
                    ref_id = row["pkce_verifier_ref"]
                try:
                    if not ref_id:
                        raise OAuthExchangeError("pkce_failed", "OAuth PKCE verifier was not stored for this Connect attempt.")
                    verifier_payload = SECRET_STORE.get_json(SecretRef(ref_id, "local-file"))
                    verifier = str(verifier_payload.get("pkce_verifier") or "")
                    if not verifier or hashlib.sha256(verifier.encode()).hexdigest() != row["pkce_verifier_hash"]:
                        raise OAuthExchangeError("pkce_failed", "OAuth PKCE verifier validation failed.")
                    # Prefer the redirect captured when Connect started so exchange
                    # matches the authorization request even if process env drifted.
                    redirect_uri = str(row["redirect_uri"] or OAUTH_REDIRECT_URI).strip()
                    tokens = exchange_gmail_code(
                        query.get("code", [""])[0],
                        os.environ.get("GOOGLE_CLIENT_ID", "").strip(),
                        os.environ.get("GOOGLE_CLIENT_SECRET", "").strip(),
                        redirect_uri,
                        verifier,
                    )
                    with Database(DB_PATH) as database:
                        account = database.connection.execute(
                            "SELECT id FROM accounts_v2 WHERE lower(email)=lower(?) AND provider_id=?",
                            (row["account_hint"], "gmail"),
                        ).fetchone()
                        if account is None:
                            raise OAuthExchangeError(
                                "account_not_found",
                                "OAuth completed, but the Gmail account record was not found in Settings.",
                            )
                        account_id = account["id"]
                    persist_oauth_tokens(account_id, row["account_hint"], tokens)
                    with Database(DB_PATH) as database:
                        database.mark_oauth_session(row["id"], "completed")
                    SECRET_STORE.delete(SecretRef(ref_id, "local-file"))
                    LOGGER.info("oauth connected account=%s", row["account_hint"])
                    if wants_html:
                        return html_response(
                            self,
                            oauth_result_page(
                                ok=True,
                                title="Account connected",
                                message="Authorization complete. Returning you to GScrape.",
                                account_id=account_id,
                            ),
                        )
                    return json_response(self, {"status": "connected", "account_id": account_id})
                except Exception as error:
                    if isinstance(error, OAuthExchangeError):
                        error_code = error.code
                        message = error.message
                    else:
                        error_code = "token_exchange_failed"
                        message = "Google consent succeeded, but the local token exchange failed. Return to Settings and try Connect again."
                    LOGGER.warning("oauth exchange failed code=%s", error_code)
                    with Database(DB_PATH) as database:
                        database.mark_oauth_session(row["id"], "failed", error_code)
                    if wants_html:
                        return html_response(
                            self,
                            oauth_result_page(
                                ok=False,
                                title="Token exchange failed",
                                message=message,
                                error_code=error_code,
                            ),
                            502,
                        )
                    return json_response(self, {"error": message, "code": error_code}, 502)
            if parsed.path.startswith("/api/runs/") and parsed.path != "/api/runs":
                parts = [part for part in parsed.path.split("/") if part]
                # /api/runs/<run_id>/attachments/<stored_name>
                if len(parts) >= 5 and parts[0] == "api" and parts[1] == "runs" and parts[3] == "attachments":
                    run_id = parts[2]
                    stored_name = "/".join(parts[4:])
                    path = resolve_acquired_attachment(run_id, stored_name)
                    if path is None:
                        return json_response(self, {"error": "attachment not found"}, 404)
                    query = parse_qs(parsed.query)
                    inline = query.get("inline", ["0"])[0] in {"1", "true", "yes"}
                    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                    raw = path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(raw)))
                    disposition = "inline" if inline else "attachment"
                    self.send_header(
                        "Content-Disposition",
                        f'{disposition}; filename="{path.name}"',
                    )
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                run_id = parts[2] if len(parts) >= 3 else ""
                connection = db()
                row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
                connection.close()
                if not row:
                    return json_response(self, {"error": "run not found"}, 404)
                records = load_run_records(run_id)
                audits = [
                    audit for audit in gscrape.load_audit_records(LOG_PATH)
                    if audit.get("run_id") == run_id
                ]
                return json_response(self, {
                    "run": dict(row),
                    "summary": gscrape.summarize_matches(records),
                    "matches": [enrich_match_for_dashboard(run_id, record) for record in records],
                    "audit": audits[-1] if audits else None,
                    "files": {
                        "audit_log": str(LOG_PATH),
                        "dashboard_logs": str(LOG_DIR),
                        "results_dir": str(row["results_dir"] or RESULTS_DIR / run_id),
                        "scan_log": str(RESULTS_DIR / run_id / "SCAN.log"),
                        "progress_log": str(progress_path(run_id)),
                        "acquired_dir": str(acquired_run_dir(run_id)),
                    },
                    "note": "Reports contain redacted metadata only; secrets and message bodies are never exported.",
                })
            if parsed.path == "/api/runs":
                return json_response(self, {"runs": list_rows("runs")})
            if parsed.path == "/api/core/jobs":
                return json_response(self, core_job_snapshot())
            if parsed.path == "/api/core/health":
                return json_response(self, {"ok": True, "provider": "gmail", "live_scan": "operator_authorization_required", "accounts": account_health_snapshot()})
            if parsed.path == "/api/core/metrics":
                return json_response(self, {"metrics": dict(METRICS), "retention_days": RETENTION_DAYS})
            if parsed.path.startswith("/api/core/jobs/") and parsed.path.count("/") == 4:
                job_id = parsed.path.rsplit("/", 1)[-1]
                snapshot = core_job_snapshot(job_id)
                if not snapshot["jobs"]:
                    return json_response(self, {"error": "job not found"}, 404)
                return json_response(self, snapshot)
            if parsed.path.startswith("/api/core/jobs/") and parsed.path.endswith("/events"):
                job_id = parsed.path.split("/")[4]
                query = parse_qs(parsed.query)
                try:
                    limit = int(query.get("limit", [100])[0])
                    before_id = query.get("before_id", [None])[0]
                    before_id = int(before_id) if before_id else None
                    with Database(DB_PATH) as database:
                        events = database.list_job_events(job_id, limit, before_id)
                    payload = [dict(event) for event in events]
                    return json_response(self, {"events": payload, "next_before_id": payload[-1]["id"] if payload else None})
                except (TypeError, ValueError):
                    return json_response(self, {"error": "invalid event pagination"}, 400)
            if parsed.path == "/api/analysis":
                records = load_all_run_records()
                return json_response(self, {
                    "summary": gscrape.summarize_matches(records),
                    "audit_runs": gscrape.load_audit_records(LOG_PATH)[-20:],
                })
            if parsed.path == "/api/suggestions":
                category = parse_qs(parsed.query).get("category", ["all"])[0]
                if category == "all":
                    suggestions = {
                        name: gscrape.suggest_refined_queries(name)
                        for name in sorted(gscrape.CATEGORY_PATTERNS)
                    }
                else:
                    suggestions = {category: gscrape.suggest_refined_queries(category)}
                return json_response(self, {"suggestions": suggestions})
            if parsed.path == "/api/logging":
                return json_response(self, logging_snapshot())
            if parsed.path == "/api/debug":
                return json_response(self, debug_snapshot())
            if parsed.path == "/api/export":
                return self.export(parse_qs(parsed.query))
            if parsed.path == "/api/log":
                return json_response(self, {"lines": LOG_PATH.read_text(errors="replace").splitlines()[-200:] if LOG_PATH.exists() else []})
            self.send_error(404)
        except Exception as error:
            LOGGER.exception("GET request failed for %s", parsed.path)
            json_response(self, {"error": str(error)}, 500)

    def export(self, query):
        fmt = query.get("format", ["markdown"])[0]
        run_id = query.get("run_id", [""])[0]
        selected_ids = []
        for value in query.get("run_ids", []):
            selected_ids.extend(item for item in value.split(",") if item)
        selected_ids = list(dict.fromkeys(selected_ids))
        if selected_ids:
            if len(selected_ids) > 1000:
                return json_response(self, {"error": "too many runs selected"}, 400)
            connection = db()
            placeholders = ",".join("?" for _ in selected_ids)
            rows = connection.execute(
                f"SELECT * FROM runs WHERE id IN ({placeholders}) ORDER BY started_at DESC",
                selected_ids,
            ).fetchall()
            connection.close()
            records = []
            for selected_id in selected_ids:
                records.extend(load_run_records(selected_id))
            if fmt in {"markdown", "selected-markdown", "report"}:
                lines = [
                    "# GScrape Combined Run Report", "",
                    f"Generated: {now()}",
                    f"Selected runs: {len(rows)}", "",
                    "This report contains redacted metadata only. Message bodies, credentials, "
                    "tokens, wallet secrets, and private keys are never exported.", "",
                    "## Runs", "",
                    "| Run ID | Status | Category | Scanned | Matches |",
                    "|---|---|---|---:|---:|",
                ]
                lines.extend(
                    f"| `{escape(row['id'])}` | {escape(row['status'])} | "
                    f"{escape(row['category'])} | {row['scanned'] or 0} | {row['matches'] or 0} |"
                    for row in rows
                )
                lines.extend(["", "## Findings", ""])
                for record in records:
                    lines.append(
                        f"- `{escape(str(record.get('run_id', '')) )}` — "
                        f"{escape(str(record.get('message_date', '')))} — "
                        f"{escape(str(record.get('subject', '')))} — "
                        f"{escape(', '.join(record.get('categories') or []))} — "
                        f"{escape(str(record.get('message_location', '')))}"
                    )
                body, content_type, filename = (
                    "\n".join(lines) + "\n",
                    "text/markdown",
                    "gscrape-selected-runs.md",
                )
            elif fmt in {"csv", "selected-csv"}:
                output = io.StringIO(newline="")
                writer = csv.DictWriter(output, fieldnames=[
                    "run_id", "message_date", "sender", "subject", "categories",
                    "score", "attachment_names", "message_location", "matched_search_passes",
                ])
                writer.writeheader()
                for record in records:
                    public = gscrape.public_match_record(record)
                    writer.writerow({
                        "run_id": record.get("run_id", ""),
                        "message_date": public.get("message_date", ""),
                        "sender": public.get("sender", ""),
                        "subject": public.get("subject", ""),
                        "categories": "|".join(public.get("categories") or []),
                        "score": gscrape.score_match_relevance(public.get("categories") or []),
                        "attachment_names": "|".join(public.get("attachment_names") or []),
                        "message_location": public.get("message_location", ""),
                        "matched_search_passes": "|".join(public.get("matched_search_passes") or []),
                    })
                body, content_type, filename = output.getvalue(), "text/csv", "gscrape-selected-runs.csv"
            elif fmt in {"json", "selected-json"}:
                safe_runs = [{
                    "id": row["id"],
                    "status": row["status"],
                    "category": row["category"],
                    "query": row["query"],
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "pages": row["pages"] or 0,
                    "scanned": row["scanned"] or 0,
                    "listed": row["listed"] or 0,
                    "matches": row["matches"] or 0,
                } for row in rows]
                body = json.dumps({
                    "runs": safe_runs,
                    "matches": [gscrape.public_match_record(record) for record in records],
                }, indent=2, ensure_ascii=False, sort_keys=True)
                content_type, filename = "application/json", "gscrape-selected-runs.json"
            else:
                return json_response(self, {"error": "unsupported selected export format"}, 400)
            raw = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if fmt == "run-report" and run_id:
            connection = db()
            row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            connection.close()
            if not row:
                return json_response(self, {"error": "run not found"}, 404)
            records = load_run_records(run_id)
            summary = gscrape.summarize_matches(records)
            lines = [
                "# GScrape Run Synopsis", "",
                f"- Run ID: `{run_id}`",
                f"- Status: {row['status']}",
                f"- Category: {row['category']}",
                f"- Query: {row['query'] or 'preset'}",
                f"- Messages scanned: {row['scanned'] or 0}",
                f"- Pages scanned: {row['pages'] or 0}",
                f"- Redacted findings: {summary['total_matches']}",
                f"- Audit log: `{LOG_PATH}`",
                f"- Dashboard logs: `{LOG_DIR}`", "",
                "## Findings", "",
                "This report contains redacted metadata only. Message bodies, credentials, tokens, wallet secrets, and private keys are never exported.",
            ]
            for record in records:
                lines.append(
                    f"- {record.get('message_date', '')} — {record.get('subject', '')} — "
                    f"{', '.join(record.get('categories') or [])} — {record.get('message_location', '')}"
                )
            raw = ("\n".join(lines) + "\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="gscrape-run-{run_id}.md"')
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if fmt in {"matches-csv", "matches-json", "report"}:
            records = gscrape.load_match_records(LOG_PATH)
            if fmt == "matches-csv":
                output = io.StringIO(newline="")
                writer = csv.DictWriter(output, fieldnames=[
                    "message_date", "sender", "subject", "categories", "score",
                    "attachment_names", "message_location", "matched_search_passes",
                ])
                writer.writeheader()
                for record in records:
                    categories = record.get("categories") or []
                    writer.writerow({
                        "message_date": record.get("message_date", ""),
                        "sender": record.get("sender", ""),
                        "subject": record.get("subject", ""),
                        "categories": "|".join(categories),
                        "score": gscrape.score_match_relevance(categories),
                        "attachment_names": "|".join(record.get("attachment_names") or []),
                        "message_location": record.get("message_location", ""),
                        "matched_search_passes": "|".join(record.get("matched_search_passes") or []),
                    })
                body, content_type, filename = output.getvalue(), "text/csv", "gscrape-matches.csv"
            elif fmt == "matches-json":
                body = json.dumps(
                    [gscrape.public_match_record(record) for record in records],
                    indent=2,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                content_type, filename = "application/json", "gscrape-matches.json"
            else:
                summary = gscrape.summarize_matches(records)
                lines = [
                    "# GScrape Match Report", "",
                    f"Generated: {now()}", "",
                    f"- Total matches: {summary['total_matches']}",
                    f"- Unique message IDs: {summary['unique_message_ids']}",
                    f"- High-score matches: {summary['high_score_count']}",
                    "",
                    "## Categories", "",
                    "| Category | Count |", "|---|---:|",
                ]
                lines.extend(
                    f"| {escape(str(category))} | {count} |"
                    for category, count in summary["category_counts"].items()
                )
                body, content_type, filename = (
                    "\n".join(lines) + "\n",
                    "text/markdown",
                    "gscrape-match-report.md",
                )
            raw = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        rows = list_rows("runs", 1000)
        if fmt == "json":
            body, content_type, suffix = json.dumps(rows, indent=2), "application/json", "json"
        elif fmt == "csv":
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=["id", "status", "category", "query", "started_at", "finished_at", "scanned", "matches", "error"])
            writer.writeheader()
            writer.writerows(rows)
            body, content_type, suffix = output.getvalue(), "text/csv", "csv"
        else:
            lines = ["# GScrape Run History", "", f"Generated: {now()}", "", "| Status | Category | Query | Scanned | Matches |", "|---|---|---|---:|---:|"]
            lines.extend(f"| {r['status']} | {r['category']} | {r['query'] or 'preset'} | {r['scanned']} | {r['matches']} |" for r in rows)
            body, content_type, suffix = "\n".join(lines), "text/markdown", "md"
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="gscrape-history.{suffix}"')
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        parsed = urlparse(self.path)
        parsed = parsed._replace(path=parsed.path.rstrip("/") or "/")
        try:
            data = read_json(self)
            if parsed.path == "/api/logging":
                config = configure_logging(
                    level=data.get("level"),
                    max_bytes=int(data.get("max_bytes", DEFAULT_LOG_CONFIG["max_bytes"])),
                    backup_count=int(data.get("backup_count", DEFAULT_LOG_CONFIG["backup_count"])),
                )
                LOGGER.info("logging configuration updated")
                return json_response(self, {"config": config})
            if parsed.path == "/api/core/accounts":
                if not isinstance(data, dict):
                    raise ValueError("account request must be an object")
                provider_id = str(data.get("provider_id", "gmail")).strip().lower()
                if provider_id != "gmail":
                    raise ValueError("unsupported provider")
                email = str(data.get("email", "")).strip()
                label = str(data.get("label", "")).strip() or email
                if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email) > 254:
                    raise ValueError("valid email is required")
                account_id = core_account_id(data.get("id") or secrets.token_hex(12))
                with Database(DB_PATH) as database:
                    database.ensure_provider("gmail", "Gmail")
                    database.upsert_account(account_id, "gmail", email, label)
                return json_response(self, {"account": next(a for a in core_account_snapshot() if a["id"] == account_id)}, 201)
            if parsed.path.startswith("/api/core/accounts/") and parsed.path.endswith("/connect"):
                account_id = core_account_id(parsed.path.split("/")[4])
                with Database(DB_PATH) as database:
                    row = database.connection.execute(
                        "SELECT email FROM accounts_v2 WHERE id=? AND provider_id='gmail'",
                        (account_id,),
                    ).fetchone()
                    if row is None:
                        return json_response(self, {"error": "account not found"}, 404)
                    raw_state = secrets.token_urlsafe(32)
                    state_hash = hashlib.sha256(raw_state.encode("utf-8")).hexdigest()
                    verifier = secrets.token_urlsafe(64)
                    challenge = base64.urlsafe_b64encode(
                        hashlib.sha256(verifier.encode("utf-8")).digest()
                    ).rstrip(b"=").decode("ascii")
                    verifier_ref = SECRET_STORE.put_json({"pkce_verifier": verifier})
                    session_id = database.create_oauth_session(
                        "gmail",
                        state_hash,
                        OAUTH_REDIRECT_URI,
                        datetime.fromtimestamp(time.time() + 600, timezone.utc).isoformat(),
                        row["email"],
                        hashlib.sha256(verifier.encode("utf-8")).hexdigest(),
                        verifier_ref.id,
                    )
                    database.update_account_health(account_id, "authorizing")
                return json_response(self, {
                    "session_id": session_id,
                    "authorization_url": gmail_authorization_url(
                        session_id, raw_state, row["email"], challenge,
                    ),
                    "status": "authorizing",
                    "redirect_uri": OAUTH_REDIRECT_URI,
                })
            if parsed.path.startswith("/api/core/accounts/") and parsed.path.endswith("/oauth"):
                account_id = core_account_id(parsed.path.split("/")[4])
                with Database(DB_PATH) as database:
                    row = database.connection.execute("SELECT * FROM accounts_v2 WHERE id=?", (account_id,)).fetchone()
                    if row is None:
                        return json_response(self, {"error": "account not found"}, 404)
                    database.update_account_health(account_id, "authorizing")
                    state_hash = secrets.token_hex(32)
                    session_id = database.create_oauth_session(
                        "gmail", state_hash, OAUTH_REDIRECT_URI,
                        datetime.fromtimestamp(time.time() + 600, timezone.utc).isoformat(), row["email"],
                    )
                return json_response(self, {"session_id": session_id, "status": "authorizing", "authorization_required": True})
            if parsed.path == "/api/core/jobs":
                if not isinstance(data, dict):
                    raise ValueError("job request must be an object")
                provider = str(data.get("provider", "demo")).strip().lower()
                adapter = CORE_PROVIDERS.get(provider)
                if adapter is None:
                    raise ValueError("unsupported provider")
                account_id = str(data.get("account_id", "demo-account")).strip()
                if not account_id or len(account_id) > 128:
                    raise ValueError("a valid account_id is required")
                if provider == DEMO_PROVIDER.provider_id:
                    operation = str(data.get("operation", "noop")).strip()
                    if operation not in {"noop", "fail"}:
                        raise ValueError("unsupported demo operation")
                    job_config = {"operation": operation, "provider": provider}
                    account_email = f"{account_id}@example.invalid"
                    account_label = account_id[:100]
                else:
                    with Database(DB_PATH) as database:
                        account_row = database.connection.execute(
                            "SELECT email, label, status FROM accounts_v2 WHERE id=? AND status IN ('connected', 'authorizing')",
                            (account_id,),
                        ).fetchone()
                    if account_row is None:
                        raise ValueError("connected Gmail account was not found")
                    session_file = SESSION_DIR / f"{account_id}.env"
                    credential = None
                    with Database(DB_PATH) as database:
                        credential = database.credential_for_account(account_id)
                    if credential is not None:
                        token_payload = SECRET_STORE.get_json(SecretRef(credential["secret_ref"], "local-file"))
                        session_file = SESSION_DIR / f"{account_id}.env"
                        write_session_env(session_file, token_payload, account_row["email"])
                    elif not inspect_legacy_session(session_file).get("has_refresh_token"):
                        raise ValueError("Gmail account needs authorization before starting a scan")
                    job_config = {
                        "provider": provider,
                        "account_id": account_id,
                        "expected_email": account_row["email"],
                        "env_file": str(session_file),
                        "categories": data.get("categories"),
                        "query": data.get("query", ""),
                        "after": data.get("after", ""),
                        "before": data.get("before", ""),
                        "max_messages": data.get("max_messages", 10000),
                        "run_id": data.get("run_id", ""),
                        "log_file": data.get("log_file", str(LOG_PATH)),
                        "progress_file": data.get("progress_file", ""),
                        "attachment_extensions": data.get("attachment_extensions", []),
                        "attachment_filename_regex": data.get("attachment_filename_regex", ""),
                        "attachment_discovery": data.get("attachment_discovery", False),
                    }
                    adapter._validate(job_config)
                    account_email = account_row["email"]
                    account_label = account_row["label"]
                with Database(DB_PATH) as database:
                    database.ensure_provider(provider, "Deterministic Demo Provider" if provider == "demo" else "Gmail")
                    account = database.connection.execute(
                        "SELECT id FROM accounts_v2 WHERE id=?", (account_id,)
                    ).fetchone()
                    if account is None:
                        database.connection.execute(
                            "INSERT INTO accounts_v2(id, provider_id, email, label, status, created_at) VALUES (?, ?, ?, ?, 'connected', ?)",
                            (account_id, provider, account_email, account_label, now()),
                        )
                        database.connection.commit()
                    job_id = database.create_job(account_id, job_config)
                METRICS["jobs_created"] += 1
                CORE_JOB_RUNNER.submit(job_id, adapter.execute)
                return json_response(self, {"id": job_id, "status": "queued", "provider": provider}, 202)
            if parsed.path.startswith("/api/core/jobs/") and parsed.path.endswith("/cancel"):
                job_id = parsed.path.split("/")[4]
                if not CORE_JOB_RUNNER.cancel(job_id):
                    return json_response(self, {"error": "job is not queued or was not found"}, 409)
                METRICS["jobs_cancelled"] += 1
                return json_response(self, {"id": job_id, "status": "cancelled"})
            if parsed.path.startswith("/api/core/jobs/") and parsed.path.endswith("/retry"):
                job_id = parsed.path.split("/")[4]
                with Database(DB_PATH) as database:
                    new_id = database.retry_job(job_id)
                if not new_id:
                    return json_response(self, {"error": "only failed or cancelled jobs can be retried"}, 409)
                return json_response(self, {"id": new_id, "status": "queued"}, 202)
            if parsed.path == "/api/core/cleanup":
                return json_response(self, {"removed": cleanup_result_artifacts(RETENTION_DAYS)})
            if parsed.path == "/api/profiles":
                config = validate_config(data)
                profile_id = uuid.uuid4().hex
                connection = db()
                connection.execute(
                    "INSERT INTO profiles VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (profile_id, data.get("name", "Untitled search")[:100], config["category"], config["query"],
                     config["regex"], config["after_date"], config["before_date"], config["max_messages"], now(), now()),
                )
                connection.commit()
                connection.close()
                return json_response(self, {"id": profile_id}, 201)
            if parsed.path == "/api/accounts":
                display_name = str(data.get("name") or data.get("label") or "").strip()[:100]
                email = str(data.get("email", "")).strip()[:254]
                avatar = re.sub(r"[^A-Za-z0-9]", "", str(data.get("avatar", "")))[:3].upper()
                if not display_name or "@" not in email or "." not in email.rsplit("@", 1)[-1]:
                    raise ValueError("name and a valid email address are required")
                avatar = avatar or account_initials(display_name)
                account_id = uuid.uuid4().hex
                SESSION_DIR.mkdir(exist_ok=True)
                session_file = SESSION_DIR / f"{account_id}.env"
                base = {}
                for line in (ROOT / ".env").read_text(errors="ignore").splitlines():
                    if "=" in line:
                        key, value = line.split("=", 1)
                        if key.strip() in {"GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI"}:
                            base[key.strip()] = value.strip()
                session_file.write_text("\n".join(f"{k}={v}" for k, v in base.items()) + "\n")
                session_file.chmod(0o600)
                connection = db()
                connection.execute(
                    """INSERT INTO accounts
                       (id,label,email,session_file,status,created_at,last_used_at,display_name,avatar)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (account_id, display_name, email, str(session_file), "needs_authorization",
                     now(), None, display_name, avatar),
                )
                connection.commit()
                connection.close()
                LOGGER.info("account profile created: %s", account_id)
                return json_response(self, {"id": account_id, "session_file": str(session_file)}, 201)
            if parsed.path.startswith("/api/accounts/") and parsed.path.endswith("/remove"):
                account_id = parsed.path.split("/")[3]
                connection = db()
                row = connection.execute("SELECT session_file, email FROM accounts WHERE id=?", (account_id,)).fetchone()
                connection.execute("DELETE FROM accounts WHERE id=?", (account_id,))
                connection.commit()
                connection.close()
                if row:
                    Path(row["session_file"]).unlink(missing_ok=True)
                return json_response(self, {"removed": bool(row)})
            if parsed.path.startswith("/api/accounts/") and parsed.path.endswith("/authorize"):
                account_id = parsed.path.split("/")[3]
                connection = db()
                row = connection.execute("SELECT session_file, email FROM accounts WHERE id=?", (account_id,)).fetchone()
                connection.close()
                if not row:
                    return json_response(self, {"error": "account not found"}, 404)
                subprocess.Popen([str(PYTHON if PYTHON.exists() else sys.executable), str(SCANNER),
                                  "--authorize", "--account-email", row["email"], "--env-file", row["session_file"]],
                                 cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                connection = db()
                connection.execute("UPDATE accounts SET status=? WHERE id=?", ("authorizing", account_id))
                connection.commit()
                connection.close()
                return json_response(self, {"started": True})
            if parsed.path.startswith("/api/accounts/") and parsed.path.endswith("/profile"):
                return self.account_diagnostic(parsed.path.split("/")[3], "profile")
            if parsed.path.startswith("/api/accounts/") and parsed.path.endswith("/labels"):
                return self.account_diagnostic(parsed.path.split("/")[3], "labels")
            if parsed.path == "/api/runs":
                config = validate_config(data)
                if not config["account_id"]:
                    raise ValueError("select exactly one email account before starting a search")
                connection = db()
                run_id = allocate_run_id(connection)
                account = connection.execute(
                    "SELECT * FROM accounts WHERE id=?", (config["account_id"],)
                ).fetchone()
                if not account:
                    connection.close()
                    raise ValueError("selected email account was not found")
                if not account_is_connected(account):
                    connection.close()
                    raise ValueError("selected email account is not connected; authorize it first")
                run_id, run_dir = reserve_run_directory(run_id)
                connection.execute(
                    """INSERT INTO runs
                       (id,profile_id,status,query,category,started_at,results_dir)
                       VALUES(?,?,?,?,?,?,?)""",
                    (run_id, data.get("profile_id"), "queued", config["query"],
                     ",".join(config["categories"]), now(), str(run_dir)),
                )
                connection.commit()
                connection.close()
                threading.Thread(target=run_scan, args=(run_id, config), daemon=True).start()
                return json_response(self, {"id": run_id, "status": "queued"}, 202)
            if parsed.path == "/api/runs/bulk-delete":
                ids = data.get("ids") or []
                if not isinstance(ids, list) or not ids:
                    raise ValueError("select at least one run")
                ids = [str(run_id) for run_id in ids if str(run_id).strip()]
                if not ids or len(ids) > 1000:
                    raise ValueError("invalid run selection")
                connection = db()
                placeholders = ",".join("?" for _ in ids)
                cursor = connection.execute(
                    f"DELETE FROM runs WHERE id IN ({placeholders})", ids
                )
                connection.commit()
                connection.close()
                LOGGER.info("bulk removed %d dashboard runs", cursor.rowcount)
                return json_response(self, {"deleted": cursor.rowcount})
            if parsed.path.startswith("/api/runs/") and parsed.path.endswith("/delete"):
                run_id = parsed.path.split("/")[3]
                connection = db()
                cursor = connection.execute("DELETE FROM runs WHERE id=?", (run_id,))
                connection.commit()
                connection.close()
                return json_response(self, {"deleted": cursor.rowcount == 1})
            if parsed.path.startswith("/api/runs/") and parsed.path.endswith("/stop"):
                run_id = parsed.path.split("/")[3]
                connection = db()
                row = connection.execute("SELECT status, pid FROM runs WHERE id=?", (run_id,)).fetchone()
                if not row:
                    connection.close()
                    return json_response(self, {"error": "run not found"}, 404)
                if row["status"] not in {"queued", "running"}:
                    connection.close()
                    return json_response(self, {"error": "only queued or running runs can be stopped"}, 409)
                connection.execute("UPDATE runs SET status=?, finished_at=?, error=? WHERE id=?", ("cancelled", now(), "stopped by operator", run_id))
                connection.commit()
                connection.close()
                if row["pid"]:
                    try:
                        os.kill(int(row["pid"]), 15)
                    except (OSError, ValueError):
                        pass
                return json_response(self, {"id": run_id, "status": "cancelled"})
            if parsed.path.startswith("/api/runs/") and parsed.path.endswith("/retry"):
                run_id = parsed.path.split("/")[3]
                connection = db()
                row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
                if not row:
                    connection.close()
                    return json_response(self, {"error": "run not found"}, 404)
                if row["status"] not in {"failed", "cancelled"}:
                    connection.close()
                    return json_response(self, {"error": "only failed or stopped runs can be retried"}, 409)
                config = json.loads(row["request_json"] or "{}")
                connection.execute("UPDATE runs SET status=?, started_at=?, finished_at=NULL, error=NULL, pid=NULL WHERE id=?", ("queued", now(), run_id))
                connection.commit()
                connection.close()
                threading.Thread(target=run_scan, args=(run_id, config), daemon=True).start()
                return json_response(self, {"id": run_id, "status": "queued"})
            if parsed.path.startswith("/api/runs/") and parsed.path.endswith("/resume"):
                run_id = parsed.path.split("/")[3]
                connection = db()
                row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
                if not row:
                    connection.close()
                    return json_response(self, {"error": "run not found"}, 404)
                if row["status"] != "cancelled":
                    connection.close()
                    return json_response(self, {"error": "only stopped runs can be resumed"}, 409)
                config = json.loads(row["request_json"] or "{}")
                connection.execute("UPDATE runs SET status=?, started_at=?, finished_at=NULL, error=NULL, pid=NULL WHERE id=?", ("queued", now(), run_id))
                connection.commit()
                connection.close()
                threading.Thread(target=run_scan, args=(run_id, config), daemon=True).start()
                return json_response(self, {"id": run_id, "status": "queued"})
            self.send_error(404)
        except ValueError as error:
            json_response(self, {"error": str(error)}, 400)
        except Exception as error:
            LOGGER.exception("POST request failed for %s", parsed.path)
            json_response(self, {"error": str(error)}, 500)

    def account_diagnostic(self, account_id, operation):
        connection = db()
        row = connection.execute(
            "SELECT session_file FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
        connection.close()
        if not row:
            return json_response(self, {"error": "account not found"}, 404)
        flag = "--profile-check" if operation == "profile" else "--list-labels"
        result = subprocess.run(
            [
                str(PYTHON if PYTHON.exists() else sys.executable),
                str(SCANNER),
                flag,
                "--env-file",
                row["session_file"],
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=120,
        )
        if result.returncode:
            detail = (result.stderr or result.stdout or "diagnostic failed")[-1000:]
            return json_response(self, {"error": detail}, 400)
        if operation == "profile":
            try:
                return json_response(self, {"profile": json.loads(result.stdout)})
            except json.JSONDecodeError:
                return json_response(self, {"error": "scanner returned invalid profile data"}, 500)
        labels = [
            {"id": line[:40].strip(), "name": line[40:].strip()}
            for line in result.stdout.splitlines()
            if line.strip()
        ]
        return json_response(self, {"labels": labels})

class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    configure_logging()
    db().close()
    recovered = recover_interrupted_runs()
    print(f"GScrape dashboard: http://{HOST}:{PORT}/")
    print(f"OAuth redirect URI: {OAUTH_REDIRECT_URI}")
    if recovered:
        print(f"Recovered {recovered} interrupted run(s) from a prior dashboard process")
    server = ReusableThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        CORE_JOB_RUNNER.shutdown(wait=True)


if __name__ == "__main__":
    main()
