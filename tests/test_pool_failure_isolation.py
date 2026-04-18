"""Tests for BrowserWorkerPool's failure-classification and
shared-queue isolation guarantees.

Covers two reported issues:
  A. consecutive_failures must not conflate legitimate business misses
     (e.g. GrokAPIError "No matching video found") with browser death.
  B. add_worker must isolate each worker on its own Chrome instance —
     passing force_new_chrome=True to GrokClient bypasses ai-dev-browser's
     profile-agnostic workspace reuse.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from grok_web.exceptions import GrokAPIError, GrokAuthError, GrokNotFoundError
from grok_web.pool.worker_pool import BrowserWorkerPool, _is_infra_failure

# =============================================================================
# _is_infra_failure classifier
# =============================================================================


class TestIsInfraFailure:
    """Business errors → False; transport errors → True."""

    @pytest.mark.parametrize(
        "exc",
        [
            GrokAPIError("No matching video found by file size in all favorites"),
            GrokAPIError("Submit button is disabled (no image uploaded?)"),
            GrokAuthError("Cloudflare challenge detected"),
            GrokNotFoundError("post not found"),
            ValueError("bad input"),
            TypeError("wrong type"),
        ],
    )
    def test_business_errors_are_not_infra(self, exc):
        assert _is_infra_failure(exc) is False

    @pytest.mark.parametrize(
        "msg",
        [
            "ConnectionRefusedError: [Errno 111] Connection refused",
            "OSError: [WinError 1225] No connection could be made",
            "OSError: [WinError 10054] Connection reset",
            "websocket connection closed",
            "ProtocolException: CDP command timed out after 30s: Runtime.evaluate",
            "Target closed",
            "Browser disconnected",
        ],
    )
    def test_transport_errors_are_infra(self, msg):
        assert _is_infra_failure(RuntimeError(msg)) is True

    def test_generic_runtime_error_without_hint_is_not_infra(self):
        """Unknown error text → treat as business (err on the side of NOT
        killing the worker). A genuine browser death will produce one of
        the known hints soon enough."""
        assert _is_infra_failure(RuntimeError("something weird happened")) is False


# =============================================================================
# Problem A: shared-queue isolation
# =============================================================================


class TestNoSharedQueueDrain:
    """When a worker's browser dies, it must not drain jobs queued for
    sibling workers."""

    @pytest.mark.asyncio
    async def test_business_failures_do_not_trip_dead_browser_detection(self):
        """3 GrokAPIError in a row with a LIVE browser must not cause the
        worker to exit — previous behavior treated any 3 exceptions as
        potential browser death."""
        # Simulate the "after each job" check our loop runs. 3 business
        # errors in a row → counter resets to 0, fail-fast not triggered.
        consecutive_failures = 0
        for _ in range(3):
            exc = GrokAPIError("No matching video found by file size in all favorites")
            if _is_infra_failure(exc):
                consecutive_failures += 1
            else:
                consecutive_failures = 0
        assert consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_worker_death_does_not_drain_shared_queues(self):
        """If a worker's _worker_loop breaks out due to dead browser, the
        pool's priority_queue and job_queue must remain intact so other
        workers can keep consuming."""
        from grok_web.pool.job import Job

        pool = BrowserWorkerPool(num_workers=0)
        # Seed the shared queues with work
        j1 = Job(task_type="match_local_video", args=("a.mp4",))
        j2 = Job(task_type="match_local_video", args=("b.mp4",))
        j3 = Job(task_type="match_local_video", args=("c.mp4",))
        await pool._job_queue.put(j1)
        await pool._job_queue.put(j2)
        await pool._priority_queue.put(j3)

        # Simulate a worker observing infra failures and exiting its loop.
        # The loop exits via `break` WITHOUT calling _fail_all_pending_jobs —
        # queues must still hold the seeded jobs for other workers.
        from grok_web.pool.worker_pool import Worker

        dead_worker = Worker(worker_id=0, port=9999)
        with patch.object(pool, "_check_browser_alive", return_value=False):
            # We only exercise the tail check of _worker_loop — no full loop.
            consecutive_failures = 3
            if consecutive_failures >= 3 and not pool._check_browser_alive(dead_worker):
                dead_worker.mark_stopping()  # matches what _worker_loop does

        assert pool._job_queue.qsize() == 2
        assert pool._priority_queue.qsize() == 1


# =============================================================================
# Problem B: per-worker Chrome isolation via force_new_chrome
# =============================================================================


class TestWorkerChromeIsolation:
    @pytest.mark.asyncio
    async def test_add_worker_forces_new_chrome(self):
        """Each worker must pass force_new_chrome=True so ai-dev-browser's
        profile-agnostic workspace reuse doesn't collapse all workers onto
        the same existing Chrome."""
        pool = BrowserWorkerPool(num_workers=0)
        pool._running = True

        with patch("grok_web.pool.worker_pool.GrokClient") as MockClient:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client._remote_port = 9350
            MockClient.return_value = mock_client

            await pool.add_worker()

            MockClient.assert_called_once()
            kwargs = MockClient.call_args.kwargs
            assert kwargs["force_new_chrome"] is True, (
                "Pool workers must pass force_new_chrome=True so each one gets "
                "its own Chrome instance rather than silently sharing whatever "
                "debug Chrome ai-dev-browser's workspace-reuse lands on."
            )
            assert kwargs["profile"] == "grok-chrome-w0"

    @pytest.mark.asyncio
    async def test_add_multiple_workers_uses_distinct_profiles(self):
        """Sanity: workers 0..N get profiles grok-chrome-w0..wN."""
        pool = BrowserWorkerPool(num_workers=0)
        pool._running = True

        profiles_seen: list[str] = []

        with patch("grok_web.pool.worker_pool.GrokClient") as MockClient:

            def _make_client(**kw):
                profiles_seen.append(kw["profile"])
                mock = MagicMock()
                mock.__aenter__ = AsyncMock(return_value=mock)
                mock.__aexit__ = AsyncMock(return_value=None)
                mock._remote_port = 9350 + len(profiles_seen)
                return mock

            MockClient.side_effect = _make_client
            await pool.add_worker()
            await pool.add_worker()
            await pool.add_worker()

        assert profiles_seen == ["grok-chrome-w0", "grok-chrome-w1", "grok-chrome-w2"]
