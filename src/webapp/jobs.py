# src/webapp/jobs.py
"""
Single-worker job execution for the local web/MCP layer.

Why a single worker:
- The engine's FX rate caches (ECB/ČNB JSON files) and the classification
  cache have no locking — concurrent runs could corrupt them.
- ``decimal.getcontext()`` is thread-local; the pool's ``initializer`` sets
  the engine's precision/rounding once for the worker thread's lifetime.

A ``ThreadPoolExecutor(max_workers=1)`` serializes all engine work by
construction, with no per-callsite lock bookkeeping.

``engine_file_lock`` additionally guards against a SECOND PROCESS running
the engine concurrently (e.g. the web server and the MCP server): both wrap
pipeline runs in the same ``flock``-based lock file.
"""
import fcntl
import logging
import threading
import traceback
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Optional

import src.config as config
from src.utils.decimal_context import setup_decimal_context

logger = logging.getLogger(__name__)

DEFAULT_LOCK_FILE = Path(config.ECB_RATES_CACHE_FILE_PATH).parent / "engine.lock"
LOG_TAIL_MAX_LINES = 80


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class JobState:
    job_id: str
    description: str
    status: JobStatus = JobStatus.QUEUED
    log_tail: Deque[str] = field(default_factory=lambda: deque(maxlen=LOG_TAIL_MAX_LINES))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    result: Any = None
    error: Optional[str] = None


class _JobLogHandler(logging.Handler):
    """Captures engine log records into the job's log tail while it runs."""

    def __init__(self, state: JobState):
        super().__init__(level=logging.INFO)
        self._state = state

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._state.log_tail.append(self.format(record))
        except Exception:  # never let log capture break the job
            pass


class JobRunner:
    """Runs engine work on a single worker thread with a job registry.

    The worker thread gets the engine's decimal context via ``initializer``;
    submitting more work while a job runs simply queues it.
    """

    def __init__(self):
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="engine-job",
            initializer=setup_decimal_context,
        )
        self._jobs: Dict[str, JobState] = {}
        self._registry_lock = threading.Lock()

    def submit(self, description: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
        job_id = uuid.uuid4().hex[:12]
        state = JobState(job_id=job_id, description=description)
        with self._registry_lock:
            self._jobs[job_id] = state

        def _run() -> None:
            state.status = JobStatus.RUNNING
            state.started_at = datetime.now(timezone.utc)
            handler = _JobLogHandler(state)
            handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
            root = logging.getLogger()
            root.addHandler(handler)
            final_status = JobStatus.DONE
            try:
                state.result = fn(*args, **kwargs)
            except Exception as exc:
                final_status = JobStatus.FAILED
                state.error = "".join(
                    traceback.format_exception_only(type(exc), exc)
                ).strip()
                logger.exception(f"Job {job_id} ({description}) failed.")
            finally:
                root.removeHandler(handler)
                state.finished_at = datetime.now(timezone.utc)
                # Status transitions LAST — pollers treat a terminal status as
                # "all other fields are final".
                state.status = final_status

        self._executor.submit(_run)
        return job_id

    def get(self, job_id: str) -> Optional[JobState]:
        with self._registry_lock:
            return self._jobs.get(job_id)

    def run_sync(self, fn: Callable[..., Any], *args: Any, timeout: Optional[float] = None, **kwargs: Any) -> Any:
        """Execute *fn* on the worker thread and wait for the result.

        Used for short interactive work (quote-backed valuation, sale
        simulation) that must still be serialized with engine runs — it
        touches the same unlocked FX caches and needs the worker's decimal
        context. Raises whatever *fn* raises.
        """
        return self._executor.submit(fn, *args, **kwargs).result(timeout=timeout)

    def shutdown(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait)


@contextmanager
def engine_file_lock(lock_file: Optional[Path] = None, blocking: bool = True):
    """Cross-process exclusive lock around engine runs.

    Protects the unlocked FX/classification cache files when more than one
    process (web server, MCP server, CLI) could run the pipeline at once.
    Raises ``BlockingIOError`` immediately when ``blocking=False`` and the
    lock is already held by another process.
    """
    path = Path(lock_file) if lock_file else DEFAULT_LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
    with open(path, "w") as fh:
        fcntl.flock(fh, flags)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
