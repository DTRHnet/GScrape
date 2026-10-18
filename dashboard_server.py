#!/usr/bin/env python3
"""Local-only GScrape dashboard for generic, redacted email discovery workflows."""

from __future__ import annotations

import csv
import io
import json
import logging
from logging.handlers import RotatingFileHandler
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
from urllib.parse import parse_qs, urlparse

import gscrape
from core.storage import Database
from core.async_jobs import AsyncJobRunner
from core.providers import DemoProvider, GmailProvider


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "web"
DB_PATH = ROOT / "dashboard.db"
LOG_PATH = ROOT / "email-wallet-search.log"
SCANNER = ROOT / "gscrape.py"
PYTHON = ROOT / ".venv" / "bin" / "python"
SESSION_DIR = ROOT / "sessions"
HOST = "127.0.0.1"
PORT = 8766
MAX_QUERY = 2000
LOG_DIR = ROOT / "logs"
LOG_CONFIG_PATH = LOG_DIR / "logging.json"
RESULTS_DIR = ROOT / "results"
DEFAULT_AFTER_DATE = "2013/01/01"
DEFAULT_BEFORE_DATE = "2018/12/31"
DEFAULT_LOG_CONFIG = {"level": "INFO", "max_bytes": 5 * 1024 * 1024, "backup_count": 5}
RETENTION_DAYS = int(os.environ.get("GSCRAPE_RETENTION_DAYS", "30"))
METRICS = {"jobs_created": 0, "jobs_completed": 0, "jobs_failed": 0, "jobs_cancelled": 0}
LOGGER = logging.getLogger("gscrape.dashboard")
ACTIVE_LOG_HANDLER = None
CORE_JOB_RUNNER = AsyncJobRunner(str(DB_PATH))
DEMO_PROVIDER = DemoProvider()
GMAIL_PROVIDER = GmailProvider(SCANNER, ROOT, str(PYTHON if PYTHON.exists() else sys.executable), SESSION_DIR)
CORE_PROVIDERS = {DEMO_PROVIDER.provider_id: DEMO_PROVIDER, GMAIL_PROVIDER.provider_id: GMAIL_PROVIDER}


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


def read_json(handler):
    try:
        length = min(int(handler.headers.get("Content-Length", "0")), 100_000)
        return json.loads(handler.rfile.read(length) or b"{}")
    except (ValueError, json.JSONDecodeError):
        raise ValueError("request body must be valid JSON")


def validate_config(data):
    category = str(data.get("category", "all"))
    allowed = {"all", *gscrape.CATEGORY_PATTERNS}
    if category not in allowed:
        raise ValueError("unsupported category")
    raw_categories = data.get("categories")
    if raw_categories is None:
        categories = list(gscrape.CATEGORY_PATTERNS) if category == "all" else [category]
    elif isinstance(raw_categories, list):
        categories = [str(item).strip() for item in raw_categories if str(item).strip()]
    else:
        categories = [item.strip() for item in str(raw_categories).split(",") if item.strip()]
    if not categories:
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
    attachment_discovery = bool(data.get("attachment_discovery", False))
    if not attachment_discovery:
        attachment_extensions = []
        attachment_filename_regex = ""
    if attachment_filename_regex:
        try:
            re.compile(attachment_filename_regex, re.I)
        except re.error as error:
            raise ValueError(f"invalid attachment filename regex: {error}") from error
    max_messages = int(data.get("max_messages", 1000))
    if not 1 <= max_messages <= 10_000:
        raise ValueError("max_messages must be between 1 and 10000")
    requests_per_second = float(data.get("requests_per_second", 1.0))
    if not 0 <= requests_per_second <= 10:
        raise ValueError("requests_per_second must be between 0 and 10")
    rate_limit_max_delay = float(data.get("rate_limit_max_delay", 60.0))
    if not 1 <= rate_limit_max_delay <= 300:
        raise ValueError("rate_limit_max_delay must be between 1 and 300")
    # Discovery is historical by default; callers may explicitly override the
    # window for a separately authorized search.
    after_date = str(data.get("after_date") or DEFAULT_AFTER_DATE).strip()
    before_date = str(data.get("before_date") or DEFAULT_BEFORE_DATE).strip()
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
        "attachment_discovery": attachment_discovery,
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


def account_is_connected(row):
    session = Path(row["session_file"])
    return session.exists() and "GMAIL_REFRESH_TOKEN" in session.read_text(errors="ignore")


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
            scan_log.flush()
            process = subprocess.Popen(
                args,
                cwd=ROOT,
                text=True,
                bufsize=1,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
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
        status = "completed" if result_code == 0 else "failed"
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
            ("failed", now(), str(error)[:1000], run_id),
        )
    connection.commit()
    row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    records = load_run_records(run_id)
    persist_progress_records(run_id, records)
    write_run_artifacts(run_id, row, records, row["status"], row["error"])
    connection.close()
    LOGGER.info("scan finished: %s (%s)", run_id, status if "status" in locals() else "failed")


class Handler(BaseHTTPRequestHandler):
    server_version = "GScrape/1.3"

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
        if parsed.path.startswith("/assets/"):
            suffix = Path(parsed.path).suffix
            types = {".js": "text/javascript", ".css": "text/css"}
            return self.send_file(STATIC / parsed.path.removeprefix("/"), types.get(suffix, "text/plain"))
        try:
            if parsed.path == "/api/status":
                return json_response(self, {
                    "ok": True,
                    "service": "GScrape",
                    "scanner": SCANNER.name,
                    "local_only": True,
                    "database": str(DB_PATH.name),
                    "version": "0.6.0",
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
            if parsed.path.startswith("/api/runs/") and parsed.path != "/api/runs":
                run_id = parsed.path.split("/")[3]
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
                    "matches": [gscrape.public_match_record(record) for record in records],
                    "audit": audits[-1] if audits else None,
                    "files": {
                        "audit_log": str(LOG_PATH),
                        "dashboard_logs": str(LOG_DIR),
                        "results_dir": str(row["results_dir"] or RESULTS_DIR / run_id),
                        "scan_log": str(RESULTS_DIR / run_id / "SCAN.log"),
                        "progress_log": str(progress_path(run_id)),
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
                            "SELECT email, label FROM accounts WHERE id=? AND status IN ('connected', 'authorizing')",
                            (account_id,),
                        ).fetchone()
                    if account_row is None:
                        raise ValueError("connected Gmail account was not found")
                    session_file = SESSION_DIR / f"{account_id}.env"
                    job_config = {
                        "provider": provider,
                        "account_id": account_id,
                        "env_file": str(session_file),
                        "categories": data.get("categories"),
                        "query": data.get("query", ""),
                        "after": data.get("after", DEFAULT_AFTER_DATE),
                        "before": data.get("before", DEFAULT_BEFORE_DATE),
                        "max_messages": data.get("max_messages", 100),
                        "run_id": data.get("run_id", ""),
                        "log_file": data.get("log_file", str(LOG_PATH)),
                        "progress_file": data.get("progress_file", ""),
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
                row = connection.execute("SELECT session_file FROM accounts WHERE id=?", (account_id,)).fetchone()
                connection.execute("DELETE FROM accounts WHERE id=?", (account_id,))
                connection.commit()
                connection.close()
                if row:
                    Path(row["session_file"]).unlink(missing_ok=True)
                return json_response(self, {"removed": bool(row)})
            if parsed.path.startswith("/api/accounts/") and parsed.path.endswith("/authorize"):
                account_id = parsed.path.split("/")[3]
                connection = db()
                row = connection.execute("SELECT session_file FROM accounts WHERE id=?", (account_id,)).fetchone()
                connection.close()
                if not row:
                    return json_response(self, {"error": "account not found"}, 404)
                subprocess.Popen([str(PYTHON if PYTHON.exists() else sys.executable), str(SCANNER),
                                  "--authorize", "--env-file", row["session_file"]],
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
    print(f"GScrape dashboard: http://{HOST}:{PORT}/")
    server = ReusableThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        CORE_JOB_RUNNER.shutdown(wait=True)


if __name__ == "__main__":
    main()
