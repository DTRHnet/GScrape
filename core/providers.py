"""Provider adapter contracts for the modular demo."""

from __future__ import annotations

import os
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Protocol, Sequence


class ProviderAdapter(Protocol):
    """Minimal provider contract used by asynchronous jobs."""

    provider_id: str

    def execute(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Run a provider operation and return non-secret summary metadata."""


class DemoProvider:
    """Deterministic adapter for local demos and control-plane validation."""

    provider_id = "demo"

    def execute(self, config: Dict[str, Any]) -> Dict[str, Any]:
        operation = str(config.get("operation", "noop"))
        if operation == "fail":
            raise RuntimeError("demo provider failure")
        return {"provider": self.provider_id, "operation": operation, "status": "ok"}


class GmailProvider:
    """Bounded adapter for the legacy scanner's non-interactive search path.

    OAuth setup remains an explicit user action through the legacy CLI. This
    adapter only consumes an existing session env file and returns metadata.
    """

    provider_id = "gmail"
    allowed_categories = {"gourl", "wallet", "seed", "wallet-file", "crypto-address", "identity", "phone"}

    def __init__(self, scanner_path: Path, workdir: Path, python_executable: str | None = None, allowed_root: Path | None = None):
        self.scanner_path = Path(scanner_path)
        self.workdir = Path(workdir)
        self.python_executable = python_executable or os.environ.get("GSCRAPE_PYTHON", "python3")
        self.allowed_root = Path(allowed_root or workdir).resolve()

    @staticmethod
    def _normalize_email(value: Any) -> str:
        return str(value or "").strip().casefold()

    def validate_session_identity(self, expected_email: str, profile: Mapping[str, Any]) -> None:
        """Reject credentials that resolve to a different Gmail account."""
        if self._normalize_email(profile.get("emailAddress")) != self._normalize_email(expected_email):
            raise ValueError("Gmail session identity does not match the selected account")

    def _verify_session_identity(self, env_file: Path, expected_email: str) -> None:
        result = subprocess.run(
            [self.python_executable, str(self.scanner_path), "--profile-check", "--env-file", str(env_file)],
            cwd=self.workdir, text=True, capture_output=True, timeout=120,
        )
        if result.returncode:
            raise ValueError("Gmail session profile verification failed")
        try:
            profile = json.loads(result.stdout)
        except (TypeError, ValueError) as error:
            raise ValueError("Gmail session profile verification returned invalid data") from error
        self.validate_session_identity(expected_email, profile)

    def _validate(self, config: Mapping[str, Any]) -> Dict[str, Any]:
        allowed = {"account_id", "expected_email", "env_file", "categories", "query", "after", "before", "max_messages", "run_id", "log_file", "progress_file", "attachment_extensions", "attachment_filename_regex", "attachment_discovery"}
        unknown = set(config) - allowed
        if unknown:
            raise ValueError("unsupported Gmail job fields")
        env_file = Path(str(config.get("env_file", ""))).expanduser()
        try:
            env_file = env_file.resolve()
        except OSError as error:
            raise ValueError("invalid Gmail session file") from error
        if not env_file.is_file() or self.allowed_root not in env_file.parents:
            raise ValueError("Gmail session file is not available")
        categories = config.get("categories", ["gourl", "wallet", "seed", "wallet-file", "crypto-address", "identity", "phone"])
        if isinstance(categories, str):
            categories = [item.strip() for item in categories.split(",") if item.strip()]
        if not isinstance(categories, Sequence) or isinstance(categories, (bytes, str)):
            raise ValueError("categories must be a list")
        categories = [str(item) for item in categories]
        attachment_discovery = bool(config.get("attachment_discovery", False))
        if not categories and not attachment_discovery:
            raise ValueError("unsupported Gmail category")
        if set(categories) - self.allowed_categories:
            raise ValueError("unsupported Gmail category")
        raw_max = config.get("max_messages", None)
        if isinstance(raw_max, str):
            raw_max = raw_max.strip()
        if raw_max in (None, "", 0, "0"):
            max_messages = 10000
        else:
            try:
                max_messages = int(raw_max)
            except (TypeError, ValueError) as error:
                raise ValueError("max_messages must be between 1 and 10000") from error
        if not 1 <= max_messages <= 10000:
            raise ValueError("max_messages must be between 1 and 10000")
        query = str(config.get("query", "")).strip()
        if len(query) > 2000 or re.search(r"(?:token|secret|password|private.?key|seed.?phrase)", query, re.I):
            raise ValueError("query contains unsupported sensitive content")
        after = str(config.get("after") or "").strip()
        before = str(config.get("before") or "").strip()
        if after and not re.fullmatch(r"\d{4}/\d{2}/\d{2}", after):
            raise ValueError("Gmail dates must use YYYY/MM/DD")
        if before and not re.fullmatch(r"\d{4}/\d{2}/\d{2}", before):
            raise ValueError("Gmail dates must use YYYY/MM/DD")
        extensions = config.get("attachment_extensions", [])
        if isinstance(extensions, str):
            extensions = extensions.split(",")
        attachment_filename_regex = str(config.get("attachment_filename_regex", "")).strip()
        if attachment_filename_regex:
            re.compile(attachment_filename_regex, re.I)
        return {"env_file": env_file, "expected_email": str(config.get("expected_email", "")), "categories": categories, "query": query,
                "after": after, "before": before,
                "max_messages": max_messages, "run_id": str(config.get("run_id", "")),
                "log_file": Path(str(config.get("log_file", self.workdir / "email-wallet-search.log"))),
                "progress_file": Path(str(config.get("progress_file", ""))),
                "attachment_extensions": [str(item) for item in extensions],
                "attachment_discovery": attachment_discovery,
                "attachment_filename_regex": attachment_filename_regex}

    def execute(self, config: Dict[str, Any]) -> Dict[str, Any]:
        validated = self._validate(config)
        if not validated["expected_email"]:
            raise ValueError("selected Gmail account email is required")
        self._verify_session_identity(validated["env_file"], validated["expected_email"])
        command = [self.python_executable, str(self.scanner_path), "--categories", ",".join(validated["categories"]),
                   "--max-messages", str(validated["max_messages"]), "--env-file", str(validated["env_file"]),
                   "--log-file", str(validated["log_file"])]
        if validated["after"]:
            command.extend(["--after", validated["after"]])
        if validated["before"]:
            command.extend(["--before", validated["before"]])
        if validated["query"]:
            command.extend(["--query", validated["query"]])
        if validated["run_id"]:
            command.extend(["--run-id", validated["run_id"]])
        if validated["progress_file"]:
            command.extend(["--progress-file", str(validated["progress_file"])])
        if validated["attachment_extensions"]:
            command.extend(["--attachment-extensions", ",".join(validated["attachment_extensions"])])
        if validated["attachment_filename_regex"]:
            command.extend(["--attachment-filename-regex", validated["attachment_filename_regex"]])
        if validated["attachment_discovery"]:
            command.append("--attachment-discovery")
        try:
            result = self._run_bounded(command)
        except subprocess.TimeoutExpired:
            return {"provider": self.provider_id, "status": "failed", "exit_code": None,
                    "error_code": "scanner_timeout", "run_id": validated["run_id"],
                    "stderr_lines": 0, "matches": None}
        except OSError:
            return {"provider": self.provider_id, "status": "failed", "exit_code": None,
                    "error_code": "scanner_start_failed", "run_id": validated["run_id"],
                    "stderr_lines": 0, "matches": None}
        status = "ok" if result.returncode == 0 else "failed"
        return {"provider": self.provider_id, "status": "ok" if result.returncode == 0 else "failed",
                "exit_code": result.returncode, "run_id": validated["run_id"],
                "stderr_lines": len(result.stderr.splitlines()),
                "error_code": None if result.returncode == 0 else "scanner_exit_nonzero",
                "matches": self._read_match_count(validated["log_file"]) if status == "ok" else None}

    def _run_bounded(self, command: list[str], attempts: int = 2):
        """Run the scanner with bounded retry for transient process failures."""
        last = None
        for attempt in range(attempts):
            last = subprocess.run(command, cwd=self.workdir, text=True, capture_output=True, timeout=3600)
            if last.returncode == 0 or attempt + 1 == attempts:
                return last
            time.sleep(0.25 * (attempt + 1))
        return last

    @staticmethod
    def _read_match_count(log_file: Path) -> int:
        """Count redacted JSONL match records without returning their contents."""
        if not log_file.is_file():
            return 0
        count = 0
        for line in log_file.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                record = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(record, dict) and record.get("event") == "gmail-crypto-match":
                count += 1
        return count