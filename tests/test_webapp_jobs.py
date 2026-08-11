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
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import getcontext
from pathlib import Path

import pytest

import src.config as config
from src.webapp.jobs import (
    JobProgress, JobRunner, JobState, JobStatus, engine_file_lock,
)


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

    def test_progress_is_only_handed_out_when_asked_for(self):
        """Existing jobs take no `report` kwarg — opting in must be explicit."""
        job_id = self.runner.submit("no progress", lambda: "fine")
        assert _wait_for(self.runner, job_id).status == JobStatus.DONE
        assert self.runner.get(job_id).progress.label == ""

    def test_a_progress_job_publishes_where_it_is_while_it_runs(self):
        seen = []
        gate = threading.Event()
        have_id = threading.Event()

        def work(report):
            # The worker can start before submit() returns, so wait for the id.
            assert have_id.wait(5.0)
            report(label="Stahuji", step=0, total=3, detail="obchody")
            seen.append(self.runner.get(job_id).progress)
            report(detail="pozice")          # detail alone keeps label + counts
            seen.append(self.runner.get(job_id).progress)
            report(step=3)
            gate.set()

        job_id = self.runner.submit("progress job", work, with_progress=True)
        have_id.set()
        assert gate.wait(5.0)
        _wait_for(self.runner, job_id)
        assert [(p.label, p.step, p.total, p.detail) for p in seen] == [
            ("Stahuji", 0, 3, "obchody"),
            ("Stahuji", 0, 3, "pozice"),
        ]
        final = self.runner.get(job_id).progress
        assert (final.label, final.step, final.detail) == ("Stahuji", 3, "pozice")
        assert final.percent == 100

    def test_find_active_returns_only_unfinished_jobs_of_that_kind(self):
        gate = threading.Event()
        held = self.runner.submit("slow", lambda: gate.wait(5.0), kind="fetch",
                                  meta={"run_id": "2026-x"})
        other = self.runner.submit("slow other", lambda: gate.wait(5.0),
                                   kind="positions")
        found = self.runner.find_active("fetch")
        assert found is not None and found.job_id == held
        assert found.meta["run_id"] == "2026-x"
        assert self.runner.find_active("nothing-like-this") is None
        gate.set()
        _wait_for(self.runner, held)
        _wait_for(self.runner, other)
        # Finished jobs are not "active" any more.
        assert self.runner.find_active("fetch") is None
        assert self.runner.find_active("positions") is None

    def test_a_failed_job_does_not_block_the_next_one(self):
        job_id = self.runner.submit("boom", lambda: 1 / 0, kind="fetch")
        _wait_for(self.runner, job_id)
        assert self.runner.find_active("fetch") is None

    def test_progress_percent_is_none_when_the_length_is_unknown(self):
        assert JobProgress(label="x").percent is None
        assert JobProgress(step=1, total=3).percent == 33

    def test_elapsed_seconds_is_none_until_the_job_starts(self):
        state = JobState(job_id="x", description="queued forever")
        assert state.elapsed_seconds() is None
        state.started_at = datetime.now(timezone.utc) - timedelta(seconds=90)
        assert state.elapsed_seconds() >= 90
        state.finished_at = state.started_at + timedelta(seconds=12)
        assert state.elapsed_seconds() == 12   # frozen once finished


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
