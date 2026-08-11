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


@dataclass(frozen=True)
class JobProgress:
    """A coarse "where are we" snapshot, published by the running job.

    Frozen and replaced wholesale rather than field-by-field: the worker
    thread writes it while HTTP threads poll, and a single attribute rebind
    cannot be observed half-applied (a step counter from the new phase next
    to the old phase's total).
    """
    label: str = ""          # what is happening: "Stahuji výpisy z IBKR"
    detail: str = ""         # the current item: "pozice · rok 2026"
    step: int = 0            # steps FINISHED, so 0/5 means "starting the first"
    total: int = 0           # 0 = unknown length, render as a plain spinner

    @property
    def percent(self) -> Optional[int]:
        if self.total <= 0:
            return None
        return min(100, int(round(100 * self.step / self.total)))


@dataclass
class JobState:
    job_id: str
    description: str
    status: JobStatus = JobStatus.QUEUED
    kind: str = ""            # groups jobs so an in-flight one can be found
    meta: Dict[str, Any] = field(default_factory=dict)
    progress: JobProgress = field(default_factory=JobProgress)
    log_tail: Deque[str] = field(default_factory=lambda: deque(maxlen=LOG_TAIL_MAX_LINES))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    result: Any = None
    error: Optional[str] = None

    def report(self, label: Optional[str] = None, detail: Optional[str] = None,
               step: Optional[int] = None, total: Optional[int] = None) -> None:
        """Publish progress from the worker thread; omitted fields are kept.

        Passing only ``detail`` is the common case — an IBKR download reports
        its poll attempts without restating which slot it is on.
        """
        cur = self.progress
        self.progress = JobProgress(
            label=cur.label if label is None else label,
            detail=cur.detail if detail is None else detail,
            step=cur.step if step is None else step,
            total=cur.total if total is None else total,
        )

    def elapsed_seconds(self) -> Optional[int]:
        """Wall-clock since the job started running; None while queued."""
        if self.started_at is None:
            return None
        end = self.finished_at or datetime.now(timezone.utc)
        return int((end - self.started_at).total_seconds())


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

    def submit(self, description: str, fn: Callable[..., Any], *args: Any,
               with_progress: bool = False, kind: str = "",
               meta: Optional[Dict[str, Any]] = None, **kwargs: Any) -> str:
        """Queue *fn* on the worker thread and return its job id.

        ``with_progress=True`` passes the job's ``report`` callable to *fn* as
        the keyword ``report``, so long jobs can say where they are instead of
        leaving the poller to guess from the log tail. ``kind`` lets callers
        find an already-queued job of the same sort via ``find_active``.
        """
        job_id = uuid.uuid4().hex[:12]
        state = JobState(job_id=job_id, description=description, kind=kind,
                         meta=dict(meta or {}))
        with self._registry_lock:
            self._jobs[job_id] = state
        if with_progress:
            kwargs["report"] = state.report

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

    def find_active(self, kind: str) -> Optional[JobState]:
        """The queued-or-running job of this ``kind``, newest first.

        Lets a caller attach to work already in flight instead of queueing a
        duplicate. With one worker a second submission would not race the
        first, but it would still fire its own IBKR requests once the first
        finished — earning a rate limit and overwriting the .bak copy of the
        statement the first download replaced.
        """
        with self._registry_lock:
            for state in reversed(list(self._jobs.values())):
                if state.kind == kind and state.status in (
                        JobStatus.QUEUED, JobStatus.RUNNING):
                    return state
        return None

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
