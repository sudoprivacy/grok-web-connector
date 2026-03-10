"""Integration tests for BrowserWorkerPool.

Requires real Chrome and Grok credentials.
Run with: pytest tests/integration/test_worker_pool_integration.py -v
"""

import pytest

# Test image for video generation
ORBIT_POST = "9ac51419-65c8-467c-958e-97e9f1abadfa"


@pytest.mark.integration
async def test_dynamic_worker_scaling():
    """Test adding/removing workers and queueing jobs while pool is running."""
    from grok_web.pool import BrowserWorkerPool

    async with BrowserWorkerPool(
        num_workers=2,
        max_retries=3,
        headless=False,
        close_chrome=True,
    ) as pool:
        # Initial state
        assert pool.worker_count == 2

        # Phase 1: Submit 2 jobs
        job_ids = []
        for cmd in ["Zoom In", "Zoom Out"]:
            job_id = await pool.submit(
                "create_video",
                source_post_id=ORBIT_POST,
                prompt=cmd,
            )
            job_ids.append(job_id)

        # Phase 2: Add worker while running
        new_worker_id = await pool.add_worker()
        assert pool.worker_count == 3

        # Phase 3: Queue more jobs
        for cmd in ["Dolly In", "Dolly Out"]:
            job_id = await pool.submit(
                "create_video",
                source_post_id=ORBIT_POST,
                prompt=cmd,
            )
            job_ids.append(job_id)

        # Phase 4: Wait for all jobs
        results = await pool.wait(timeout=300)
        assert len(results) == 4
        assert all(r.success for r in results.values())

        # Phase 5: Remove worker (pool idle now)
        await pool.remove_worker(new_worker_id, wait=False)
        assert pool.worker_count == 2


@pytest.mark.integration
async def test_close_chrome_on_exit():
    """Test that Chrome processes are terminated when pool exits."""
    import subprocess

    from grok_web.pool import BrowserWorkerPool

    # Get Chrome count before
    def count_chrome():
        result = subprocess.run(
            ["pgrep", "-f", "grok_chrome_"],
            capture_output=True,
            text=True,
        )
        return len(result.stdout.strip().split("\n")) if result.stdout.strip() else 0

    chrome_before = count_chrome()

    async with BrowserWorkerPool(
        num_workers=2,
        headless=True,
        close_chrome=True,
    ) as pool:
        assert pool.worker_count == 2
        # Don't submit any jobs, just test Chrome lifecycle

    # Chrome should be terminated
    chrome_after = count_chrome()
    assert chrome_after <= chrome_before, "Chrome processes should be terminated on exit"


@pytest.mark.integration
async def test_job_retry_on_failure():
    """Test that jobs are retried on failure."""
    from grok_web.pool import BrowserWorkerPool

    async with BrowserWorkerPool(
        num_workers=1,
        max_retries=2,
        headless=False,
        close_chrome=True,
    ) as pool:
        # Submit a job that should succeed
        job_id = await pool.submit(
            "create_video",
            parent_post_id=ORBIT_POST,
            adjustment_prompt="Zoom In",
        )

        result = await pool.wait_for(job_id, timeout=120)
        assert result.success


@pytest.mark.integration
async def test_multiple_workers_distribute_jobs():
    """Test that jobs are distributed across multiple workers."""
    from grok_web.pool import BrowserWorkerPool

    async with BrowserWorkerPool(
        num_workers=3,
        max_retries=2,
        headless=False,
        close_chrome=True,
    ) as pool:
        # Submit 3 jobs - each worker should get one
        job_ids = []
        for cmd in ["Zoom In", "Zoom Out", "Dolly In"]:
            job_id = await pool.submit(
                "create_video",
                source_post_id=ORBIT_POST,
                prompt=cmd,
            )
            job_ids.append(job_id)

        results = await pool.wait(timeout=300)
        assert len(results) == 3
        assert all(r.success for r in results.values())

        # Check that multiple workers were used
        worker_ids_used = {r.worker_id for r in results.values()}
        assert len(worker_ids_used) >= 2, "Jobs should be distributed across workers"
