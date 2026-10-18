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
        return future

    def _execute(self, job_id: str, operation: JobCallable) -> Dict[str, Any]:
        try:
            with Database(self.database_path) as database:
                row = database.get_job(job_id)
                if row is None:
                    raise KeyError(job_id)
                import json
                config = json.loads(row["config_snapshot_json"])
            result = operation(config)
            with Database(self.database_path) as database:
                database.finish_job(job_id, "completed")
                database.add_job_event(job_id, "completed", {"result": result})
            return result
        except Exception as error:
            with Database(self.database_path) as database:
                database.finish_job(job_id, "failed", error_code=type(error).__name__)
                database.add_job_event(job_id, "failed", {"error": type(error).__name__})
            raise

    def shutdown(self, wait: bool = True) -> None:
        self.executor.shutdown(wait=wait)