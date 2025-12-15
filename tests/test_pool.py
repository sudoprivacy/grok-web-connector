"""Tests for pool module (job, worker, persistence, worker_pool)."""

import asyncio
import json
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from grok_web.pool.job import Job, JobResult, JobStatus
from grok_web.pool.persistence import PoolState, load_state, save_state
from grok_web.pool.worker import Worker, WorkerStats, WorkerStatus

# =============================================================================
# Job Tests
# =============================================================================


class TestJobStatus:
    """Tests for JobStatus enum."""

    def test_status_values(self):
        """All expected status values exist."""
        assert JobStatus.PENDING.value == "pending"
        assert JobStatus.IN_PROGRESS.value == "in_progress"
        assert JobStatus.COMPLETED.value == "completed"
        assert JobStatus.FAILED.value == "failed"


class TestJob:
    """Tests for Job dataclass."""

    def test_job_defaults(self):
        """Job has sensible defaults."""
        job = Job(task_type="test_task")
        assert job.task_type == "test_task"
        assert job.args == ()
        assert job.kwargs == {}
        assert job.retries == 0
        assert job.max_retries == -1
        assert job.status == JobStatus.PENDING
        assert job.job_id is not None
        assert isinstance(job.created_at, datetime)

    def test_job_with_args_kwargs(self):
        """Job stores args and kwargs."""
        job = Job(
            task_type="create_video",
            args=("post123",),
            kwargs={"adjustment_prompt": "Orbit"},
        )
        assert job.args == ("post123",)
        assert job.kwargs == {"adjustment_prompt": "Orbit"}

    def test_job_to_dict(self):
        """Job serializes to dict."""
        job = Job(
            task_type="create_video",
            args=("arg1", "arg2"),
            kwargs={"key": "value"},
            max_retries=3,
        )
        data = job.to_dict()
        assert data["task_type"] == "create_video"
        assert data["args"] == ["arg1", "arg2"]
        assert data["kwargs"] == {"key": "value"}
        assert data["max_retries"] == 3
        assert data["status"] == "pending"
        assert "job_id" in data
        assert "created_at" in data

    def test_job_from_dict(self):
        """Job deserializes from dict."""
        data = {
            "job_id": "test-uuid",
            "task_type": "list_posts",
            "args": ["arg1"],
            "kwargs": {"limit": 10},
            "retries": 2,
            "max_retries": 5,
            "created_at": "2025-01-01T12:00:00",
            "status": "in_progress",
        }
        job = Job.from_dict(data)
        assert job.job_id == "test-uuid"
        assert job.task_type == "list_posts"
        assert job.args == ("arg1",)
        assert job.kwargs == {"limit": 10}
        assert job.retries == 2
        assert job.max_retries == 5
        assert job.status == JobStatus.IN_PROGRESS

    def test_job_roundtrip(self):
        """Job survives serialization roundtrip."""
        original = Job(
            task_type="get_details",
            args=("post_id",),
            kwargs={"include_meta": True},
            max_retries=10,
        )
        data = original.to_dict()
        restored = Job.from_dict(data)
        assert restored.job_id == original.job_id
        assert restored.task_type == original.task_type
        assert restored.args == original.args
        assert restored.kwargs == original.kwargs


class TestJobResult:
    """Tests for JobResult dataclass."""

    def test_success_result(self):
        """Success result stores data."""
        result = JobResult(
            job_id="job123",
            success=True,
            data={"video_id": "vid456"},
            worker_id=0,
        )
        assert result.job_id == "job123"
        assert result.success is True
        assert result.data == {"video_id": "vid456"}
        assert result.error is None
        assert result.worker_id == 0

    def test_failure_result(self):
        """Failure result stores error."""
        result = JobResult(
            job_id="job123",
            success=False,
            error="Connection timeout",
            worker_id=1,
        )
        assert result.success is False
        assert result.error == "Connection timeout"
        assert result.data is None

    def test_result_to_dict(self):
        """JobResult serializes to dict."""
        result = JobResult(
            job_id="job123",
            success=True,
            data={"key": "value"},
            worker_id=2,
        )
        data = result.to_dict()
        assert data["job_id"] == "job123"
        assert data["success"] is True
        assert data["data"] == {"key": "value"}
        assert data["worker_id"] == 2
        assert "completed_at" in data

    def test_result_from_dict(self):
        """JobResult deserializes from dict."""
        data = {
            "job_id": "job789",
            "success": False,
            "data": None,
            "error": "Failed",
            "completed_at": "2025-01-01T12:00:00",
            "worker_id": 3,
        }
        result = JobResult.from_dict(data)
        assert result.job_id == "job789"
        assert result.success is False
        assert result.error == "Failed"
        assert result.worker_id == 3


# =============================================================================
# Worker Tests
# =============================================================================


class TestWorkerStatus:
    """Tests for WorkerStatus enum."""

    def test_status_values(self):
        """All expected status values exist."""
        assert WorkerStatus.IDLE.value == "idle"
        assert WorkerStatus.BUSY.value == "busy"
        assert WorkerStatus.STOPPING.value == "stopping"
        assert WorkerStatus.STOPPED.value == "stopped"
        assert WorkerStatus.ERROR.value == "error"


class TestWorkerStats:
    """Tests for WorkerStats dataclass."""

    def test_stats_defaults(self):
        """Stats have zero defaults."""
        stats = WorkerStats()
        assert stats.success == 0
        assert stats.fail == 0
        assert stats.total_time == 0.0

    def test_stats_total(self):
        """Total is sum of success and fail."""
        stats = WorkerStats(success=5, fail=3)
        assert stats.total == 8

    def test_stats_success_rate_zero_total(self):
        """Success rate is 0.0 when no jobs."""
        stats = WorkerStats()
        assert stats.success_rate == 0.0

    def test_stats_success_rate(self):
        """Success rate calculated correctly."""
        stats = WorkerStats(success=8, fail=2)
        assert stats.success_rate == 0.8


class TestWorker:
    """Tests for Worker dataclass."""

    def test_worker_defaults(self):
        """Worker has sensible defaults."""
        worker = Worker(worker_id=0, port=9223)
        assert worker.worker_id == 0
        assert worker.port == 9223
        assert worker.client is None
        assert worker.status == WorkerStatus.IDLE
        assert worker.current_job is None
        assert isinstance(worker.stats, WorkerStats)

    def test_worker_mark_busy(self):
        """mark_busy sets status and job."""
        worker = Worker(worker_id=0, port=9223)
        job = Job(task_type="test")
        worker.mark_busy(job)
        assert worker.status == WorkerStatus.BUSY
        assert worker.current_job == job

    def test_worker_mark_idle(self):
        """mark_idle clears status and job."""
        worker = Worker(worker_id=0, port=9223)
        job = Job(task_type="test")
        worker.mark_busy(job)
        worker.mark_idle()
        assert worker.status == WorkerStatus.IDLE
        assert worker.current_job is None

    def test_worker_mark_stopping(self):
        """mark_stopping sets stopping status."""
        worker = Worker(worker_id=0, port=9223)
        worker.mark_stopping()
        assert worker.status == WorkerStatus.STOPPING

    def test_worker_mark_stopped(self):
        """mark_stopped sets stopped status."""
        worker = Worker(worker_id=0, port=9223)
        worker.mark_stopped()
        assert worker.status == WorkerStatus.STOPPED

    def test_worker_to_dict(self):
        """Worker serializes to dict."""
        worker = Worker(worker_id=1, port=9224)
        worker.stats.success = 5
        worker.stats.fail = 1
        data = worker.to_dict()
        assert data["worker_id"] == 1
        assert data["port"] == 9224
        assert data["status"] == "idle"
        assert data["current_job_id"] is None
        assert data["stats"]["success"] == 5
        assert data["stats"]["fail"] == 1
        assert data["stats"]["total"] == 6

    def test_worker_to_dict_with_job(self):
        """Worker to_dict includes current job id."""
        worker = Worker(worker_id=0, port=9223)
        job = Job(task_type="test")
        worker.mark_busy(job)
        data = worker.to_dict()
        assert data["current_job_id"] == job.job_id

    @pytest.mark.asyncio
    async def test_worker_wait_current_task_no_task(self):
        """wait_current_task returns immediately when no task."""
        worker = Worker(worker_id=0, port=9223)
        result = await worker.wait_current_task(timeout=0.1)
        assert result is True

    @pytest.mark.asyncio
    async def test_worker_wait_current_task_completes(self):
        """wait_current_task waits for task completion."""
        worker = Worker(worker_id=0, port=9223)
        job = Job(task_type="test")
        worker.mark_busy(job)

        async def complete_task():
            await asyncio.sleep(0.05)
            worker.mark_idle()

        asyncio.create_task(complete_task())
        result = await worker.wait_current_task(timeout=1.0)
        assert result is True

    @pytest.mark.asyncio
    async def test_worker_wait_current_task_timeout(self):
        """wait_current_task returns False on timeout."""
        worker = Worker(worker_id=0, port=9223)
        job = Job(task_type="test")
        worker.mark_busy(job)
        result = await worker.wait_current_task(timeout=0.01)
        assert result is False


# =============================================================================
# Persistence Tests
# =============================================================================


class TestPoolState:
    """Tests for PoolState dataclass."""

    def test_state_defaults(self):
        """State has sensible defaults."""
        state = PoolState()
        assert state.version == 1
        assert state.completed == {}
        assert state.pending == []
        assert state.in_progress == []

    def test_state_to_dict(self):
        """State serializes to dict."""
        job = Job(task_type="test")
        result = JobResult(job_id="r1", success=True)
        state = PoolState(
            completed={"r1": result},
            pending=[job],
            in_progress=[],
        )
        data = state.to_dict()
        assert data["version"] == 1
        assert "r1" in data["completed"]
        assert len(data["pending"]) == 1
        assert data["pending"][0]["task_type"] == "test"

    def test_state_from_dict(self):
        """State deserializes from dict."""
        data = {
            "version": 1,
            "last_updated": "2025-01-01T12:00:00",
            "completed": {
                "job1": {
                    "job_id": "job1",
                    "success": True,
                    "data": {"key": "value"},
                    "error": None,
                    "completed_at": "2025-01-01T12:00:00",
                    "worker_id": 0,
                }
            },
            "pending": [
                {
                    "job_id": "job2",
                    "task_type": "create_video",
                    "args": [],
                    "kwargs": {},
                    "retries": 0,
                    "max_retries": -1,
                    "created_at": "2025-01-01T11:00:00",
                    "status": "pending",
                }
            ],
            "in_progress": [],
        }
        state = PoolState.from_dict(data)
        assert "job1" in state.completed
        assert state.completed["job1"].success is True
        assert len(state.pending) == 1
        assert state.pending[0].task_type == "create_video"


class TestPersistenceFunctions:
    """Tests for save_state and load_state functions."""

    def test_save_state_creates_file(self):
        """save_state creates state file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            state = PoolState()
            save_state(state, path)
            assert path.exists()

    def test_save_state_valid_json(self):
        """save_state writes valid JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            state = PoolState()
            save_state(state, path)
            with open(path) as f:
                data = json.load(f)
            assert "version" in data
            assert "last_updated" in data

    def test_load_state_nonexistent(self):
        """load_state returns None for nonexistent file."""
        result = load_state("/nonexistent/path.json")
        assert result is None

    def test_load_state_roundtrip(self):
        """State survives save/load roundtrip."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            job = Job(task_type="test_task", kwargs={"key": "value"})
            result = JobResult(job_id="r1", success=True, data={"result": 42})
            original = PoolState(
                completed={"r1": result},
                pending=[job],
            )
            save_state(original, path)
            loaded = load_state(path)
            assert loaded is not None
            assert "r1" in loaded.completed
            assert loaded.completed["r1"].data == {"result": 42}
            assert len(loaded.pending) == 1
            assert loaded.pending[0].task_type == "test_task"

    def test_load_state_invalid_json(self):
        """load_state returns None for invalid JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            path.write_text("not valid json {{{")
            result = load_state(path)
            assert result is None

    def test_save_state_atomic_write(self):
        """save_state uses atomic write (temp file + rename)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            state = PoolState()
            # Write twice to verify no corruption
            save_state(state, path)
            save_state(state, path)
            loaded = load_state(path)
            assert loaded is not None


# =============================================================================
# BrowserWorkerPool Tests (unit tests with mocks)
# =============================================================================


class TestBrowserWorkerPoolInit:
    """Tests for BrowserWorkerPool initialization."""

    def test_pool_init_defaults(self):
        """Pool initializes with defaults."""
        from grok_web.pool import BrowserWorkerPool

        pool = BrowserWorkerPool()
        assert pool._num_workers == 3
        assert pool._state_file is None
        assert pool._max_retries == -1
        assert pool._running is False
        assert pool._used_ports == set()
        assert pool._available_nodriver_ports == []

    def test_pool_init_custom(self):
        """Pool accepts custom parameters."""
        from grok_web.pool import BrowserWorkerPool

        pool = BrowserWorkerPool(
            num_workers=5,
            state_file="custom.json",
            max_retries=10,
            headless=True,
        )
        assert pool._num_workers == 5
        assert pool._state_file == Path("custom.json")
        assert pool._max_retries == 10
        assert pool._headless is True


class TestBrowserWorkerPoolProperties:
    """Tests for BrowserWorkerPool properties."""

    def test_pool_pending_count(self):
        """pending_count reflects queue state."""
        from grok_web.pool import BrowserWorkerPool

        pool = BrowserWorkerPool()
        job = Job(task_type="test")
        pool._pending_jobs[job.job_id] = job
        assert pool.pending_count == 1

    def test_pool_completed_count(self):
        """completed_count reflects results."""
        from grok_web.pool import BrowserWorkerPool

        pool = BrowserWorkerPool()
        result = JobResult(job_id="r1", success=True)
        pool._results["r1"] = result
        assert pool.completed_count == 1

    def test_pool_worker_count(self):
        """worker_count reflects workers."""
        from grok_web.pool import BrowserWorkerPool

        pool = BrowserWorkerPool()
        worker = Worker(worker_id=0, port=9223)
        pool._workers[0] = worker
        assert pool.worker_count == 1

    def test_pool_get_status(self):
        """get_status returns status dict."""
        from grok_web.pool import BrowserWorkerPool

        pool = BrowserWorkerPool()
        pool._running = True
        worker = Worker(worker_id=0, port=9223)
        pool._workers[0] = worker
        result = JobResult(job_id="r1", success=True)
        pool._results["r1"] = result
        status = pool.get_status()
        assert status["running"] is True
        assert 0 in status["workers"]
        assert status["completed_jobs"] == 1
        assert status["success_count"] == 1


class TestBrowserWorkerPoolJobManagement:
    """Tests for BrowserWorkerPool job management."""

    @pytest.mark.asyncio
    async def test_pool_submit_creates_job(self):
        """submit creates job and returns id."""
        from grok_web.pool import BrowserWorkerPool

        pool = BrowserWorkerPool()
        job_id = await pool.submit("create_video", post_id="abc", adjustment_prompt="Orbit")
        assert job_id is not None
        assert job_id in pool._pending_jobs
        job = pool._pending_jobs[job_id]
        assert job.task_type == "create_video"
        assert job.kwargs == {"post_id": "abc", "adjustment_prompt": "Orbit"}

    @pytest.mark.asyncio
    async def test_pool_submit_batch(self):
        """submit_batch creates multiple jobs."""
        from grok_web.pool import BrowserWorkerPool

        pool = BrowserWorkerPool()
        jobs = [
            ("create_video", (), {"post_id": "1"}),
            ("create_video", (), {"post_id": "2"}),
            ("list_posts", (), {"limit": 10}),
        ]
        job_ids = await pool.submit_batch(jobs)
        assert len(job_ids) == 3
        assert all(jid in pool._pending_jobs for jid in job_ids)

    def test_pool_get_result_not_found(self):
        """get_result returns None for unknown job."""
        from grok_web.pool import BrowserWorkerPool

        pool = BrowserWorkerPool()
        assert pool.get_result("nonexistent") is None

    def test_pool_get_result_found(self):
        """get_result returns result for completed job."""
        from grok_web.pool import BrowserWorkerPool

        pool = BrowserWorkerPool()
        result = JobResult(job_id="job1", success=True, data={"video_id": "v1"})
        pool._results["job1"] = result
        assert pool.get_result("job1") == result

    @pytest.mark.asyncio
    async def test_pool_wait_for_not_found(self):
        """wait_for raises KeyError for unknown job."""
        from grok_web.pool import BrowserWorkerPool

        pool = BrowserWorkerPool()
        with pytest.raises(KeyError):
            await pool.wait_for("nonexistent")

    @pytest.mark.asyncio
    async def test_pool_wait_for_already_complete(self):
        """wait_for returns immediately for completed job."""
        from grok_web.pool import BrowserWorkerPool

        pool = BrowserWorkerPool()
        result = JobResult(job_id="job1", success=True)
        pool._results["job1"] = result
        returned = await pool.wait_for("job1")
        assert returned == result


class TestBrowserWorkerPoolStatePersistence:
    """Tests for BrowserWorkerPool state persistence."""

    def test_pool_save_state_no_file(self):
        """save_state does nothing without state_file."""
        from grok_web.pool import BrowserWorkerPool

        pool = BrowserWorkerPool(state_file=None)
        pool.save_state()  # Should not raise

    def test_pool_save_state_with_file(self):
        """save_state writes to state_file."""
        from grok_web.pool import BrowserWorkerPool

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            pool = BrowserWorkerPool(state_file=path)
            result = JobResult(job_id="r1", success=True)
            pool._results["r1"] = result
            pool.save_state()
            assert path.exists()
            loaded = load_state(path)
            assert "r1" in loaded.completed


class TestBrowserWorkerPoolExecuteJob:
    """Tests for BrowserWorkerPool._execute_job method."""

    @pytest.fixture
    def pool(self):
        """Create pool instance."""
        from grok_web.pool import BrowserWorkerPool

        return BrowserWorkerPool()

    @pytest.fixture
    def mock_worker(self) -> Worker:
        """Create worker with mock client."""
        worker = Worker(worker_id=0, port=9222)
        worker.client = MagicMock()
        return worker

    @pytest.mark.asyncio
    async def test_execute_delete_video(self, pool, mock_worker):
        """_execute_job handles delete_video task."""
        mock_worker.client.delete_video = AsyncMock(return_value=True)
        job = Job(task_type="delete_video", args=("video-123",))

        result = await pool._execute_job(mock_worker, job)

        assert result is True
        mock_worker.client.delete_video.assert_called_once_with("video-123")

    @pytest.mark.asyncio
    async def test_execute_favorite_post(self, pool, mock_worker):
        """_execute_job handles favorite_post task."""
        mock_worker.client.favorite_post = AsyncMock(return_value=True)
        job = Job(task_type="favorite_post", args=("post-123",))

        result = await pool._execute_job(mock_worker, job)

        assert result is True
        mock_worker.client.favorite_post.assert_called_once_with("post-123")

    @pytest.mark.asyncio
    async def test_execute_unfavorite_post(self, pool, mock_worker):
        """_execute_job handles unfavorite_post task."""
        mock_worker.client.unfavorite_post = AsyncMock(return_value=True)
        job = Job(task_type="unfavorite_post", args=("post-123",))

        result = await pool._execute_job(mock_worker, job)

        assert result is True
        mock_worker.client.unfavorite_post.assert_called_once_with("post-123")

    @pytest.mark.asyncio
    async def test_execute_like_post(self, pool, mock_worker):
        """_execute_job handles like_post task (thumbs up)."""
        mock_worker.client.like_post = AsyncMock(return_value=True)
        job = Job(task_type="like_post", args=("post-123",))

        result = await pool._execute_job(mock_worker, job)

        assert result is True
        mock_worker.client.like_post.assert_called_once_with("post-123")

    @pytest.mark.asyncio
    async def test_execute_dislike_post(self, pool, mock_worker):
        """_execute_job handles dislike_post task (thumbs down)."""
        mock_worker.client.dislike_post = AsyncMock(return_value=True)
        job = Job(task_type="dislike_post", args=("post-123",))

        result = await pool._execute_job(mock_worker, job)

        assert result is True
        mock_worker.client.dislike_post.assert_called_once_with("post-123")

    @pytest.mark.asyncio
    async def test_execute_upgrade_video(self, pool, mock_worker):
        """_execute_job handles upgrade_video task."""
        mock_worker.client.upgrade_video = AsyncMock(return_value=True)
        job = Job(task_type="upgrade_video", args=("video-123",))

        result = await pool._execute_job(mock_worker, job)

        assert result is True
        mock_worker.client.upgrade_video.assert_called_once_with("video-123")

    @pytest.mark.asyncio
    async def test_execute_unknown_task_raises(self, pool, mock_worker):
        """_execute_job raises ValueError for unknown task type."""
        job = Job(task_type="unknown_task")

        with pytest.raises(ValueError, match="Unknown task type"):
            await pool._execute_job(mock_worker, job)

    @pytest.mark.asyncio
    async def test_execute_with_ui_delay(self, pool, mock_worker):
        """_execute_job applies ui_delay and restores it after."""
        mock_worker.client._ui_delay = 1.0
        mock_worker.client.delete_video = AsyncMock(return_value=True)
        job = Job(task_type="delete_video", args=("video-123",), kwargs={"ui_delay": 2.0})

        await pool._execute_job(mock_worker, job)

        # ui_delay should be restored to original
        assert mock_worker.client._ui_delay == 1.0
