"""Isolated, unredacted Gmail mailbox export storage and orchestration."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional


ATTACHMENT_TYPE_EXTENSIONS = {
    "image": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff", ".bmp", ".svg"},
    "document": {".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".md"},
    "spreadsheet": {".xls", ".xlsx", ".ods", ".csv", ".tsv"},
    "archive": {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz"},
    "audio": {".mp3", ".wav", ".m4a", ".ogg", ".flac"},
    "video": {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_filename(value: str, fallback: str = "attachment") -> str:
    name = Path(str(value or "")).name
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    return (name or fallback)[:240]


def attachment_parts(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(part: Mapping[str, Any]) -> None:
        filename = str(part.get("filename") or "")
        body = part.get("body") or {}
        if filename:
            found.append({
                "filename": filename,
                "mime_type": str(part.get("mimeType") or "application/octet-stream"),
                "attachment_id": str(body.get("attachmentId") or ""),
                "size_bytes": int(body.get("size") or 0),
            })
        for child in part.get("parts") or []:
            if isinstance(child, Mapping):
                walk(child)

    walk(payload)
    return found


class RawMailboxDatabase:
    """Dedicated SQLite store. No dashboard database connection is used."""

    def __init__(self, path: Path | str):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.connection = sqlite3.connect(self.path, timeout=30.0)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = NORMAL")
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS raw_exports (
            id TEXT PRIMARY KEY, account TEXT NOT NULL, query TEXT NOT NULL,
            include_attachments INTEGER NOT NULL, attachment_filter TEXT NOT NULL,
            status TEXT NOT NULL, message_count INTEGER NOT NULL DEFAULT 0,
            attachment_count INTEGER NOT NULL DEFAULT 0, failure_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS raw_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, export_id TEXT NOT NULL,
            provider_message_id TEXT NOT NULL, thread_id TEXT, internal_date TEXT,
            raw_api_json TEXT NOT NULL, raw_rfc822 BLOB, created_at TEXT NOT NULL,
            UNIQUE(export_id, provider_message_id),
            FOREIGN KEY(export_id) REFERENCES raw_exports(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS raw_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, raw_message_id INTEGER NOT NULL,
            filename TEXT NOT NULL, mime_type TEXT, attachment_id TEXT, size_bytes INTEGER,
            sha256 TEXT, blob_path TEXT, content BLOB, download_status TEXT NOT NULL,
            error TEXT, UNIQUE(raw_message_id, filename),
            FOREIGN KEY(raw_message_id) REFERENCES raw_messages(id) ON DELETE CASCADE
        );
        """)
        migrations = {
            "processed_messages": "INTEGER NOT NULL DEFAULT 0",
            "total_messages": "INTEGER",
            "pages_scanned": "INTEGER NOT NULL DEFAULT 0",
            "last_error": "TEXT",
        }
        existing = {row[1] for row in self.connection.execute("PRAGMA table_info(raw_exports)")}
        for column, definition in migrations.items():
            if column not in existing:
                self.connection.execute(f"ALTER TABLE raw_exports ADD COLUMN {column} {definition}")
        self.connection.commit()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "RawMailboxDatabase":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def create_or_resume(self, export_id: Optional[str], account: str, query: str,
                         include_attachments: bool, attachment_filter: str) -> str:
        export_id = export_id or uuid.uuid4().hex
        row = self.connection.execute("SELECT id FROM raw_exports WHERE id=?", (export_id,)).fetchone()
        if row is None:
            self.connection.execute(
                "INSERT INTO raw_exports(id,account,query,include_attachments,attachment_filter,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (export_id, account, query, int(include_attachments), attachment_filter, "running", utc_now(), utc_now()),
            )
        else:
            self.connection.execute("UPDATE raw_exports SET status='running', updated_at=? WHERE id=?", (utc_now(), export_id))
        self.connection.commit()
        return export_id

    def has_message(self, export_id: str, message_id: str) -> bool:
        return self.connection.execute("SELECT 1 FROM raw_messages WHERE export_id=? AND provider_message_id=?", (export_id, message_id)).fetchone() is not None

    def list_messages(self, export_id: str, *, limit: int = 50, offset: int = 0,
                      subject: str = "", sender: str = "", attachment_type: str = "") -> tuple[list[sqlite3.Row], int]:
        """Return filtered lightweight rows and the matching total for the browser."""
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        rows = self.connection.execute(
            """SELECT m.id, m.provider_message_id, m.thread_id, m.internal_date,
                      m.raw_api_json, m.raw_rfc822, m.created_at, length(m.raw_rfc822) AS raw_bytes,
                      COUNT(a.id) AS attachment_count
                 FROM raw_messages AS m
                 LEFT JOIN raw_attachments AS a ON a.raw_message_id=m.id
                WHERE m.export_id=?
                GROUP BY m.id
                ORDER BY CAST(m.internal_date AS INTEGER) DESC, m.id DESC""",
            (export_id,),
        ).fetchall()
        subject = str(subject or "").strip().casefold()
        sender = str(sender or "").strip().casefold()
        attachment_type = str(attachment_type or "").strip().casefold()
        if subject or sender or attachment_type:
            from core.header_analysis import analyze_email_headers
            filtered = []
            for row in rows:
                analysis = analyze_email_headers(row["raw_rfc822"])
                fields = analysis.get("fields", {})
                subject_value = str(fields.get("subject") or "").casefold()
                sender_value = " ".join(str(item.get("address") or item.get("name") or "") for item in fields.get("from", [])).casefold()
                if subject and subject not in subject_value:
                    continue
                if sender and sender not in sender_value:
                    continue
                if attachment_type:
                    attachments = self.connection.execute(
                        "SELECT filename, mime_type FROM raw_attachments WHERE raw_message_id=?",
                        (row["id"],),
                    ).fetchall()
                    if not any(attachment_matches_type(item["filename"], item["mime_type"], attachment_type) for item in attachments):
                        continue
                filtered.append(row)
            rows = filtered
        total = len(rows)
        return rows[offset:offset + limit], total

    def message(self, export_id: str, message_row_id: int) -> Optional[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM raw_messages WHERE export_id=? AND id=?",
            (export_id, int(message_row_id)),
        ).fetchone()

    def attachments(self, export_id: str, message_row_id: int) -> list[sqlite3.Row]:
        return self.connection.execute(
            """SELECT a.* FROM raw_attachments AS a
                JOIN raw_messages AS m ON m.id=a.raw_message_id
               WHERE m.export_id=? AND m.id=?
               ORDER BY a.id""",
            (export_id, int(message_row_id)),
        ).fetchall()

    def put_message(self, export_id: str, message: Mapping[str, Any], raw_rfc822: Optional[bytes]) -> int:
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO raw_messages(export_id,provider_message_id,thread_id,internal_date,raw_api_json,raw_rfc822,created_at) VALUES(?,?,?,?,?,?,?)",
            (export_id, str(message.get("id") or ""), str(message.get("threadId") or ""), str(message.get("internalDate") or ""), json.dumps(message, ensure_ascii=False, separators=(",", ":")), raw_rfc822, utc_now()),
        )
        if cursor.rowcount == 0:
            return int(self.connection.execute("SELECT id FROM raw_messages WHERE export_id=? AND provider_message_id=?", (export_id, str(message.get("id") or ""))).fetchone()[0])
        return int(cursor.lastrowid)

    def put_attachment(self, message_row_id: int, item: Mapping[str, Any], *, status: str,
                       sha256: str = "", blob_path: str = "", content: Optional[bytes] = None,
                       error: str = "") -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO raw_attachments(raw_message_id,filename,mime_type,attachment_id,size_bytes,sha256,blob_path,content,download_status,error) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (message_row_id, str(item.get("filename") or ""), str(item.get("mime_type") or ""), str(item.get("attachment_id") or ""), int(item.get("size_bytes") or 0), sha256, blob_path, content, status, error[:1000]),
        )

    def finish(self, export_id: str, status: str, failures: int = 0) -> None:
        messages = self.connection.execute("SELECT COUNT(*) FROM raw_messages WHERE export_id=?", (export_id,)).fetchone()[0]
        attachments = self.connection.execute("SELECT COUNT(*) FROM raw_attachments WHERE raw_message_id IN (SELECT id FROM raw_messages WHERE export_id=?)", (export_id,)).fetchone()[0]
        self.connection.execute("UPDATE raw_exports SET status=?,message_count=?,attachment_count=?,failure_count=?,processed_messages=COALESCE(processed_messages, ?),updated_at=?,completed_at=? WHERE id=?", (status, messages, attachments, failures, messages + failures, utc_now(), utc_now() if status == "completed" else None, export_id))
        self.connection.commit()

    def update_progress(self, export_id: str, processed: int, failures: int, pages: int, total: Optional[int] = None, error: str = "") -> None:
        self.connection.execute(
            "UPDATE raw_exports SET processed_messages=?,failure_count=?,pages_scanned=?,total_messages=COALESCE(?,total_messages),last_error=COALESCE(NULLIF(?,''),last_error),updated_at=? WHERE id=?",
            (processed, failures, pages, total, error[:1000], utc_now(), export_id),
        )
        self.connection.commit()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        handle.write(data)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def matches_attachment_filter(filename: str, extensions: list[str], regex: str) -> bool:
    if extensions and Path(filename).suffix.lower() not in {str(x).lower() if str(x).startswith(".") else f".{str(x).lower()}" for x in extensions}:
        return False
    return not regex or bool(re.search(regex, filename, re.I))


def attachment_matches_type(filename: str, mime_type: str, category: str) -> bool:
    """Match a saved attachment against a broad, user-facing file category."""
    category = str(category or "").casefold()
    mime = str(mime_type or "").casefold()
    suffix = Path(str(filename or "")).suffix.casefold()
    if category in ATTACHMENT_TYPE_EXTENSIONS:
        return mime.startswith(f"{category}/") or suffix in ATTACHMENT_TYPE_EXTENSIONS[category]
    if category == "other":
        return not any(attachment_matches_type(filename, mime_type, name) for name in ATTACHMENT_TYPE_EXTENSIONS)
    return False


def export_gmail_mailbox(*, database_path: Path | str, blob_root: Path | str,
                         account: str, query: str, max_messages: int,
                         request_json: Callable[..., dict[str, Any]], access_token: str,
                         api_key: Optional[str] = None, retries: int = 4,
                         rate_limiter: Any = None, include_attachments: bool = False,
                         attachment_extensions: Optional[list[str]] = None,
                         attachment_filename_regex: str = "", export_id: Optional[str] = None,
                         progress: Optional[Callable[[dict[str, Any]], None]] = None,
                         cancel: Optional[Callable[[], bool]] = None) -> dict[str, Any]:
    """Export full Gmail API records into the isolated raw database."""
    blob_root = Path(blob_root).expanduser().resolve()
    blob_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    base = f"https://gmail.googleapis.com/gmail/v1/users/{account}/messages"
    extensions = attachment_extensions or []
    filter_label = ",".join(extensions) + (f"|{attachment_filename_regex}" if attachment_filename_regex else "")
    failures = 0
    pages = 0
    scanned = 0
    with RawMailboxDatabase(database_path) as database:
        export_id = database.create_or_resume(export_id, account, query, include_attachments, filter_label)
        token = None
        seen: set[str] = set()
        try:
            while scanned < max_messages:
                params: dict[str, Any] = {"q": query, "maxResults": min(100, max_messages - scanned)}
                if token:
                    params["pageToken"] = token
                page = request_json(base, api_key, access_token, params, retries=retries, rate_limiter=rate_limiter)
                pages += 1
                estimated_total = page.get("resultSizeEstimate")
                if progress:
                    progress({"export_id": export_id, "pages": pages, "messages": scanned, "total_messages": estimated_total, "failures": failures})
                database.update_progress(export_id, scanned, failures, pages, int(estimated_total) if estimated_total is not None else None)
                for item in page.get("messages", []):
                    message_id = str(item.get("id") or "")
                    if not message_id or message_id in seen or scanned >= max_messages:
                        continue
                    seen.add(message_id)
                    scanned += 1
                    if database.has_message(export_id, message_id):
                        continue
                    try:
                        message = request_json(f"{base}/{message_id}", api_key, access_token, {"format": "full"}, retries=retries, rate_limiter=rate_limiter)
                        raw_payload = request_json(f"{base}/{message_id}", api_key, access_token, {"format": "raw"}, retries=retries, rate_limiter=rate_limiter)
                        raw_rfc822 = base64.urlsafe_b64decode((raw_payload.get("raw") or "") + "===") if raw_payload.get("raw") else None
                        row_id = database.put_message(export_id, message, raw_rfc822)
                        for attachment in attachment_parts(message.get("payload") or {}):
                            selected = matches_attachment_filter(str(attachment["filename"]), extensions, attachment_filename_regex)
                            if not include_attachments or not selected or not attachment.get("attachment_id"):
                                database.put_attachment(row_id, attachment, status="metadata-only" if not include_attachments else "filtered")
                                continue
                            try:
                                content_payload = request_json(f"{base}/{message_id}/attachments/{attachment['attachment_id']}", api_key, access_token, {}, retries=retries, rate_limiter=rate_limiter)
                                content = base64.urlsafe_b64decode((content_payload.get("data") or "") + "===")
                                digest = hashlib.sha256(content).hexdigest()
                                target = blob_root / "attachments" / message_id / f"{digest[:16]}-{safe_filename(str(attachment['filename']))}"
                                if not target.exists():
                                    atomic_write(target, content)
                                database.put_attachment(row_id, attachment, status="downloaded", sha256=digest, blob_path=str(target.relative_to(blob_root)))
                            except Exception as error:
                                failures += 1
                                database.put_attachment(row_id, attachment, status="failed", error=str(error))
                    except Exception:
                        failures += 1
                    database.connection.commit()
                    if progress:
                        progress({"export_id": export_id, "pages": pages, "messages": scanned, "total_messages": estimated_total, "failures": failures})
                    database.update_progress(export_id, scanned, failures, pages, int(estimated_total) if estimated_total is not None else None)
                    if cancel and cancel():
                        database.finish(export_id, "cancelled", failures)
                        return {"export_id": export_id, "status": "cancelled", "pages": pages, "messages": scanned, "failures": failures}
                token = page.get("nextPageToken")
                if not token:
                    break
            database.finish(export_id, "completed", failures)
        except BaseException as error:
            # KeyboardInterrupt/SystemExit can terminate a CLI export while a
            # rate limiter is sleeping. Persist a terminal state before
            # re-raising so the dashboard never presents a stale "running"
            # export after an operator stop or Ctrl-C.
            terminal_status = "cancelled" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "failed"
            database.update_progress(export_id, scanned, failures + 1, pages, error=str(error))
            database.finish(export_id, terminal_status, failures + 1)
            raise
    return {"export_id": export_id, "status": "completed", "pages": pages, "messages": scanned, "failures": failures}