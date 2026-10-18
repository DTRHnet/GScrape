"""Small asynchronous job runner for the demo control plane.

The runner owns worker threads, while SQLite remains the source of truth for
job state. Callers provide a safe, local callable; credentials and mailbox
contents are intentionally outside this orchestration layer.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Dict, Any

from .storage import Database


JobCallable = Callable[[Dict[str, Any]], Dict[str, Any]]


class AsyncJobRunner:
    """Run queued jobs in background workers with durable status transitions."""

    def __init__(self, database_path: str, max_workers: int = 2):
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self.database_path = database_path
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self._futures: Dict[str, Future] = {}
        self.metrics = {"submitted": 0, "completed": 0, "failed": 0}

    def submit(self, job_id: str, operation: JobCallable) -> Future:
        """Claim and execute a queued job, or raise if it was already claimed."""
        with Database(self.database_path) as database:
            row = database.get_job(job_id)
            if row is None:
                raise KeyError(job_id)
            if not database.claim_job(job_id):
                raise RuntimeError(f"job {job_id} is not queued")
            database.add_job_event(job_id, "started", {"status": "running"})

        future = self.executor.submit(self._execute, job_id, operation)
        self._futures[job_id] = future
        self.metrics["submitted"] += 1
        return future

    def cancel(self, job_id: str) -> bool:
        """Cancel a queued job; running subprocess cancellation remains provider-specific."""
        with Database(self.database_path) as database:
            return database.cancel_job(job_id)

    def _execute(self, job_id: str, operation: JobCallable) -> Dict[str, Any]:
        try:
            with Database(self.database_path) as database:
                row = database.get_job(job_id)
                if row is None:
                    raise KeyError(job_id)
                import json
                config = json.loads(row["config_snapshot_json"])
            result = operation(config)
            if not isinstance(result, dict):
                result = {"status": "ok"}
            public_result = {key: value for key, value in result.items()
                             if key in {"provider", "status", "operation", "run_id", "matches", "error_code", "exit_code", "stderr_lines"}}
            with Database(self.database_path) as database:
                if public_result.get("status") == "failed":
                    self.metrics["failed"] += 1
                    database.finish_job(job_id, "failed", error_code=public_result.get("error_code", "provider_failed"))
                    database.add_job_event(job_id, "failed", public_result)
                else:
                    self.metrics["completed"] += 1
                    database.finish_job(job_id, "completed")
                    database.add_job_event(job_id, "completed", public_result)
                database.prune_job_events()
            return public_result
        except Exception as error:
            with Database(self.database_path) as database:
                database.finish_job(job_id, "failed", error_code=type(error).__name__)
                database.add_job_event(job_id, "failed", {"error": type(error).__name__})
            raise

    def shutdown(self, wait: bool = True) -> None:
        self.executor.shutdown(wait=wait)