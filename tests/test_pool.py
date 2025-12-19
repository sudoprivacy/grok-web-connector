"""Tests for pool module (job, worker, persistence, worker_pool)."""

import asyncio
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

    def test_job_serialization_roundtrip(self):
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

    def test_success_and_failure_results(self):
        """JobResult stores success/failure data correctly."""
        # Success case
        success = JobResult(job_id="job1", success=True, data={"video_id": "v1"}, worker_id=0)
        assert success.success is True
        assert success.data == {"video_id": "v1"}
        assert success.error is None

        # Failure case
        failure = JobResult(job_id="job2", success=False, error="Timeout", worker_id=1)
        assert failure.success is False
        assert failure.error == "Timeout"

    def test_result_serialization_roundtrip(self):
        """JobResult survives serialization roundtrip."""
        original = JobResult(job_id="job1", success=True, data={"key": "value"}, worker_id=0)
        restored = JobResult.from_dict(original.to_dict())
        assert restored.job_id == original.job_id
        assert restored.success == original.success
        assert restored.data == original.data


# =============================================================================
# Worker Tests
# =============================================================================


class TestWorkerStats:
    """Tests for WorkerStats dataclass."""

    def test_stats_calculations(self):
        """Stats calculates total and success_rate correctly."""
        # Empty stats
        empty = WorkerStats()
        assert empty.total == 0
        assert empty.success_rate == 0.0

        # With data
        stats = WorkerStats(success=8, fail=2)
        assert stats.total == 10
        assert stats.success_rate == 0.8


class TestWorker:
    """Tests for Worker dataclass."""

    def test_worker_status_transitions(self):
        """Worker status transitions work correctly."""
        worker = Worker(worker_id=0, port=9223)
        assert worker.status == WorkerStatus.IDLE

        job = Job(task_type="test")
        worker.mark_busy(job)
        assert worker.status == WorkerStatus.BUSY
        assert worker.current_job == job

        worker.mark_idle()
        assert worker.status == WorkerStatus.IDLE
        assert worker.current_job is None

        worker.mark_stopping()
        assert worker.status == WorkerStatus.STOPPING

        worker.mark_stopped()
        assert worker.status == WorkerStatus.STOPPED

    def test_worker_to_dict(self):
        """Worker serializes to dict with stats and job info."""
        worker = Worker(worker_id=1, port=9224)
        worker.stats.success = 5
        job = Job(task_type="test")
        worker.mark_busy(job)

        data = worker.to_dict()
        assert data["worker_id"] == 1
        assert data["status"] == "busy"
        assert data["current_job_id"] == job.job_id
        assert data["stats"]["success"] == 5

    @pytest.mark.asyncio
    async def test_worker_wait_current_task(self):
        """wait_current_task handles completion and timeout."""
        worker = Worker(worker_id=0, port=9223)

        # No task - returns immediately
        assert await worker.wait_current_task(timeout=0.1) is True

        # Task completes
        job = Job(task_type="test")
        worker.mark_busy(job)

        async def complete_task():
            await asyncio.sleep(0.02)
            worker.mark_idle()

        asyncio.create_task(complete_task())
        assert await worker.wait_current_task(timeout=1.0) is True

        # Timeout
        worker.mark_busy(Job(task_type="test"))
        assert await worker.wait_current_task(timeout=0.01) is False


# =============================================================================
# Persistence Tests
# =============================================================================


class TestPoolState:
    """Tests for PoolState dataclass."""

    def test_state_serialization_roundtrip(self):
        """PoolState survives serialization roundtrip."""
        job = Job(task_type="test", kwargs={"key": "value"})
        result = JobResult(job_id="r1", success=True, data={"result": 42})
        original = PoolState(completed={"r1": result}, pending=[job])

        restored = PoolState.from_dict(original.to_dict())
        assert "r1" in restored.completed
        assert restored.completed["r1"].success is True
        assert len(restored.pending) == 1
        assert restored.pending[0].task_type == "test"


class TestPersistenceFunctions:
    """Tests for save_state and load_state functions."""

    def test_save_load_roundtrip(self):
        """State survives save/load roundtrip to file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            job = Job(task_type="test_task", kwargs={"key": "value"})
            result = JobResult(job_id="r1", success=True, data={"result": 42})
            original = PoolState(completed={"r1": result}, pending=[job])

            save_state(original, path)
            assert path.exists()

            loaded = load_state(path)
            assert loaded is not None
            assert "r1" in loaded.completed
            assert loaded.pending[0].task_type == "test_task"

    def test_load_state_error_cases(self):
        """load_state handles missing and invalid files."""
        # Missing file
        assert load_state("/nonexistent/path.json") is None

        # Invalid JSON
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            path.write_text("not valid json {{{")
            assert load_state(path) is None


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
        from grok_web.client import NodriverClient

        # Use spec to make mock behave like real client (rejects unknown attributes)
        mock_worker.client = MagicMock(spec=NodriverClient)
        job = Job(task_type="unknown_task")

        with pytest.raises(ValueError, match="Unknown task_type"):
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


# =============================================================================
# BrowserWorkerPool Edge Cases Tests
# =============================================================================


class TestBrowserWorkerPoolEdgeCases:
    """Tests for BrowserWorkerPool edge cases and boundary conditions."""

    @pytest.fixture
    def pool(self):
        """Create pool instance."""
        from grok_web.pool import BrowserWorkerPool

        return BrowserWorkerPool()

    @pytest.mark.asyncio
    async def test_remove_worker_invalid_id_raises(self, pool):
        """remove_worker with invalid worker_id raises ValueError."""
        with pytest.raises(ValueError, match="Worker .* not found"):
            await pool.remove_worker(999)

    @pytest.mark.asyncio
    async def test_submit_batch_returns_job_ids(self, pool):
        """submit_batch returns list of job IDs."""
        job_ids = await pool.submit_batch(
            [
                ("create_video", ("post1",), {}),
                ("create_video", ("post2",), {}),
            ]
        )

        assert len(job_ids) == 2
        assert all(isinstance(jid, str) for jid in job_ids)

    @pytest.mark.asyncio
    async def test_submit_batch_empty_list(self, pool):
        """submit_batch with empty list returns empty list."""
        job_ids = await pool.submit_batch([])

        assert job_ids == []

    @pytest.mark.asyncio
    async def test_get_result_nonexistent_job_returns_none(self, pool):
        """get_result for nonexistent job returns None."""
        result = pool.get_result("nonexistent-job-id")

        assert result is None

    @pytest.mark.asyncio
    async def test_wait_for_timeout(self, pool):
        """wait_for with timeout raises TimeoutError."""
        job_id = await pool.submit("create_video", "post-123")

        with pytest.raises(asyncio.TimeoutError):
            await pool.wait_for(job_id, timeout=0.1)

    @pytest.mark.asyncio
    async def test_wait_for_nonexistent_job_raises(self, pool):
        """wait_for with nonexistent job raises KeyError."""
        with pytest.raises(KeyError, match="Job .* not found"):
            await pool.wait_for("nonexistent-job", timeout=0.1)

    @pytest.mark.asyncio
    async def test_wait_all_empty_pool(self, pool):
        """wait_all on empty pool returns empty dict."""
        results = await pool.wait_all(timeout=0.1)

        assert results == {}

    @pytest.mark.asyncio
    async def test_wait_all_timeout(self, pool):
        """wait_all with timeout raises TimeoutError when jobs incomplete."""
        await pool.submit("create_video", "post-123")

        with pytest.raises(asyncio.TimeoutError):
            await pool.wait_all(timeout=0.1)

    def test_pending_count_empty_pool(self, pool):
        """pending_count property returns 0 for empty pool."""
        assert pool.pending_count == 0

    def test_completed_count_empty_pool(self, pool):
        """completed_count property returns 0 for empty pool."""
        assert pool.completed_count == 0

    def test_worker_count_before_start(self, pool):
        """worker_count property returns 0 before pool is started."""
        assert pool.worker_count == 0

    def test_worker_stats_empty_pool(self, pool):
        """worker_stats property returns empty dict for unstarted pool."""
        stats = pool.worker_stats

        assert stats == {}

    def test_get_status_includes_all_fields(self, pool):
        """get_status returns dict with all status fields."""
        status = pool.get_status()

        assert "running" in status
        assert "workers" in status
        assert "pending_jobs" in status
        assert "completed_jobs" in status
        assert status["running"] is False
        assert len(status["workers"]) == 0
        assert status["pending_jobs"] == 0
        assert status["completed_jobs"] == 0

    @pytest.mark.asyncio
    async def test_submit_with_custom_max_retries(self, pool):
        """submit with custom max_retries sets job max_retries."""
        job_id = await pool.submit("create_video", "post-123", max_retries=5)

        # Check that job was created with correct max_retries
        assert job_id in pool._pending_jobs
        assert pool._pending_jobs[job_id].max_retries == 5

    @pytest.mark.asyncio
    async def test_fail_condition_callback(self):
        """fail_condition callback properly identifies failed jobs."""
        from grok_web.pool import BrowserWorkerPool

        # Create pool with fail_condition that fails on moderated=True
        pool = BrowserWorkerPool(fail_condition=lambda r: r.get("moderated", False))

        # Verify fail_condition is set
        assert pool._fail_condition is not None
        assert pool._fail_condition({"moderated": True}) is True
        assert pool._fail_condition({"moderated": False}) is False

    @pytest.mark.asyncio
    async def test_requeue_position_front(self):
        """requeue_position='front' creates pool with priority queue."""
        from grok_web.pool import BrowserWorkerPool

        pool = BrowserWorkerPool(requeue_position="front")

        assert pool._requeue_position == "front"

    @pytest.mark.asyncio
    async def test_requeue_position_back(self):
        """requeue_position='back' creates pool with normal queue."""
        from grok_web.pool import BrowserWorkerPool

        pool = BrowserWorkerPool(requeue_position="back")

        assert pool._requeue_position == "back"

    def test_save_state_without_state_file(self, pool):
        """save_state does nothing when state_file is None."""
        pool.save_state()  # Should not raise

    @pytest.mark.asyncio
    async def test_pending_count_after_submit(self, pool):
        """pending_count property increases after submitting jobs."""
        await pool.submit("create_video", "post-123")
        await pool.submit("create_video", "post-456")

        # pending_count includes both _pending_jobs dict and queue sizes
        # Each job appears in both _pending_jobs and the queue
        assert pool.pending_count >= 2
        assert len(pool._pending_jobs) == 2
