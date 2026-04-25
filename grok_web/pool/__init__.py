"""
BrowserWorkerPool - Concurrent browser workers with job queuing.

Example:
    from grok_web.pool import BrowserWorkerPool

    async with BrowserWorkerPool(num_workers=3, state_file="progress.json") as pool:
        # Submit jobs
        for command in ["Orbit", "Pan Left", "Static Shot"]:
            await pool.submit("create_video", {
                "images": ["post:abc123"], "prompt": command,
            })

        # Dynamically add worker
        await pool.add_worker()

        # Wait for all jobs
        results = await pool.wait()
        for job_id, result in results.items():
            if result.success:
                print(f"Video: {result.data['video_id']}")
"""

from ai_dev_browser.pool import (
    Job,
    JobResult,
    JobStatus,
    PoolState,
    Worker,
    WorkerStats,
    WorkerStatus,
    load_state,
    save_state,
)

from .worker_pool import BrowserWorkerPool, matches_exception

__all__ = [
    # Main class
    "BrowserWorkerPool",
    # Job models (re-exported from ai-dev-browser)
    "Job",
    "JobResult",
    "JobStatus",
    # Worker models (re-exported from ai-dev-browser)
    "Worker",
    "WorkerStats",
    "WorkerStatus",
    # Persistence (re-exported from ai-dev-browser)
    "PoolState",
    "load_state",
    "save_state",
    # Exception-type helper (reads native JobResult.error_type /
    # error_bases populated by ai-dev-browser 0.9.3+; honors MRO so
    # catching a parent class matches subclasses).
    "matches_exception",
]
