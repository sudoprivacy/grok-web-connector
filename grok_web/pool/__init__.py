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

from .job import Job, JobResult, JobStatus
from .persistence import PoolState, load_state, save_state
from .worker import Worker, WorkerStats, WorkerStatus
from .worker_pool import BrowserWorkerPool

__all__ = [
    # Main class
    "BrowserWorkerPool",
    # Job models
    "Job",
    "JobResult",
    "JobStatus",
    # Worker models
    "Worker",
    "WorkerStats",
    "WorkerStatus",
    # Persistence
    "PoolState",
    "load_state",
    "save_state",
]
