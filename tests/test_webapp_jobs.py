# tests/test_webapp_jobs.py
"""
Phase 0 server-safety primitives.

Pins the two guarantees the web/MCP layer relies on:
1. The job worker thread has the engine's decimal context (thread-local in
   CPython — a naive Thread() would silently compute at default precision 28
   vs. config, or worse, at whatever the ambient thread had).
2. Jobs are serialized (single worker), failures are captured, and the
   cross-process flock actually excludes a second acquirer.
"""
import time
from decimal import getcontext
from pathlib import Path

import pytest

import src.config as config
from src.webapp.jobs import JobRunner, JobStatus, engine_file_lock


def _wait_for(runner, job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = runner.get(job_id)
        if state.status in (JobStatus.DONE, JobStatus.FAILED):
            return state
        time.sleep(0.01)
    raise TimeoutError(f"job {job_id} did not finish")


class TestJobRunner:
    def setup_method(self):
        self.runner = JobRunner()

    def teardown_method(self):
        self.runner.shutdown()

    def test_worker_thread_has_engine_decimal_context(self):
        job_id = self.runner.submit(
            "probe decimal context",
            lambda: (getcontext().prec, getcontext().rounding),
        )
        state = _wait_for(self.runner, job_id)
        assert state.status == JobStatus.DONE
        prec, rounding = state.result
        assert prec == config.INTERNAL_CALCULATION_PRECISION
        assert rounding == config.DECIMAL_ROUNDING_MODE

    def test_jobs_run_serialized_in_submission_order(self):
        order = []

        def make(tag):
            def _fn():
                order.append(f"{tag}-start")
                time.sleep(0.05)
                order.append(f"{tag}-end")
            return _fn

        a = self.runner.submit("job a", make("a"))
        b = self.runner.submit("job b", make("b"))
        _wait_for(self.runner, a)
        _wait_for(self.runner, b)
        assert order == ["a-start", "a-end", "b-start", "b-end"]

    def test_failure_is_captured_not_raised(self):
        def boom():
            raise ValueError("broken input file")

        job_id = self.runner.submit("failing job", boom)
        state = _wait_for(self.runner, job_id)
        assert state.status == JobStatus.FAILED
        assert "broken input file" in state.error
        assert state.finished_at is not None

    def test_unknown_job_id_returns_none(self):
        assert self.runner.get("nonexistent") is None


class TestEngineFileLock:
    def test_lock_excludes_second_acquirer(self, tmp_path):
        lock_file = tmp_path / "engine.lock"
        with engine_file_lock(lock_file):
            # flock is per-(process, file-descriptor) via separate opens, so a
            # second non-blocking acquisition must fail while the first holds it.
            with pytest.raises(BlockingIOError):
                with engine_file_lock(lock_file, blocking=False):
                    pass

        # Released — now it must succeed.
        with engine_file_lock(lock_file, blocking=False):
            pass

    def test_lock_file_created_with_parents(self, tmp_path):
        nested = tmp_path / "a" / "b" / "engine.lock"
        with engine_file_lock(nested):
            assert nested.exists()
