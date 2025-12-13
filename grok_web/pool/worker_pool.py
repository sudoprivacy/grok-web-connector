"""BrowserWorkerPool - Manage multiple concurrent browser workers."""

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

from ..client import NodriverClient
from ..models import GrokCookies
from .job import Job, JobResult, JobStatus
from .persistence import PoolState, load_state, save_state
from .worker import Worker, WorkerStats, WorkerStatus

logger = logging.getLogger(__name__)

# Default base port - workers get BASE_PORT + worker_id
# We use 9223 as base to avoid 9222 (user's Chrome)
DEFAULT_BASE_PORT = 9223


class BrowserWorkerPool:
    """
    Manage multiple concurrent browser workers with job queuing and persistence.

    Features:
        - Multiple concurrent workers, each with isolated Chrome instance
        - Dynamic scaling: add/remove workers at runtime
        - Job queue for task distribution
        - Progress persistence for resume after restart
        - Graceful shutdown: complete current tasks before exit

    Example:
        async with BrowserWorkerPool(num_workers=3, state_file="progress.json") as pool:
            # Submit jobs
            job_id = await pool.submit("create_video", post_id="abc", adjustment_prompt="Orbit")

            # Dynamically add worker
            await pool.add_worker()

            # Wait for all jobs
            results = await pool.wait_all()
    """

    def __init__(
        self,
        num_workers: int = 3,
        base_port: int = DEFAULT_BASE_PORT,
        state_file: Path | str | None = None,
        max_retries: int = -1,
        config_path: Path | str | None = None,
        cookies: GrokCookies | None = None,
        headless: bool = False,
        close_chrome: bool = True,
    ):
        """
        Initialize BrowserWorkerPool.

        Args:
            num_workers: Initial number of workers to start
            base_port: Base port for Chrome instances. Worker N uses base_port + N.
            state_file: Path to state file for persistence. None = no persistence.
            max_retries: Max retries per job. -1 = unlimited retries.
            config_path: Path to grok config file (default: ~/.grok-config.json)
            cookies: Pre-loaded GrokCookies. If None, loads from config.
            headless: Run Chrome in headless mode.
            close_chrome: Terminate Chrome processes on pool exit. Default True.
        """
        self._num_workers = num_workers
        self._base_port = base_port
        self._state_file = Path(state_file) if state_file else None
        self._max_retries = max_retries
        self._config_path = config_path
        self._cookies = cookies
        self._headless = headless
        self._close_chrome = close_chrome

        # Worker management
        self._workers: dict[int, Worker] = {}
        self._worker_tasks: dict[int, asyncio.Task] = {}
        self._next_worker_id = 0

        # Job management
        self._job_queue: asyncio.Queue[Job] = asyncio.Queue()
        self._results: dict[str, JobResult] = {}
        self._pending_jobs: dict[str, Job] = {}  # job_id -> Job (for tracking)
        self._result_events: dict[str, asyncio.Event] = {}  # For wait_for()

        # Pool state
        self._running = False
        self._state = PoolState()

    async def __aenter__(self) -> "BrowserWorkerPool":
        """Start the pool and all workers."""
        # Load state if state file exists
        if self._state_file:
            loaded = load_state(self._state_file)
            if loaded:
                self._state = loaded
                self._results = loaded.completed.copy()
                # Re-queue pending and in-progress jobs
                for job in loaded.pending + loaded.in_progress:
                    job.status = JobStatus.PENDING
                    self._pending_jobs[job.job_id] = job
                    await self._job_queue.put(job)
                logger.info(
                    f"Loaded state: {len(self._results)} completed, "
                    f"{len(self._pending_jobs)} pending"
                )

        self._running = True

        # Start initial workers
        for _ in range(self._num_workers):
            await self.add_worker()

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Gracefully shutdown all workers."""
        self._running = False

        # Signal all workers to stop (after current task)
        for worker in self._workers.values():
            worker.mark_stopping()

        # Wait for all worker tasks to complete
        if self._worker_tasks:
            await asyncio.gather(*self._worker_tasks.values(), return_exceptions=True)

        # Close all browser clients and optionally terminate Chrome
        for worker in self._workers.values():
            if worker.client:
                try:
                    # Terminate Chrome process if close_chrome is True
                    if self._close_chrome and hasattr(worker.client, "_chrome_process"):
                        chrome_process = worker.client._chrome_process
                        if chrome_process is not None:
                            chrome_process.terminate()
                            logger.info(f"Terminated Chrome for worker {worker.worker_id}")

                    await worker.client.__aexit__(None, None, None)
                except Exception as e:
                    logger.warning(f"Error closing worker {worker.worker_id}: {e}")

        # Final state save
        self.save_state()

    # =========================================================================
    # Worker Management
    # =========================================================================

    async def add_worker(self) -> int:
        """Add a new worker to the pool.

        Returns:
            worker_id of the new worker
        """
        worker_id = self._next_worker_id
        self._next_worker_id += 1

        port = self._base_port + worker_id

        # Create worker
        worker = Worker(worker_id=worker_id, port=port)
        self._workers[worker_id] = worker

        # Initialize browser client
        client = NodriverClient(
            cookies=self._cookies,
            config_path=self._config_path,
            headless=self._headless,
            port=port,
            auto_launch=True,
        )

        try:
            await client.__aenter__()
            worker.client = client
            logger.info(f"Worker {worker_id} started on port {port}")
        except Exception as e:
            logger.error(f"Failed to start worker {worker_id}: {e}")
            worker.status = WorkerStatus.ERROR
            del self._workers[worker_id]
            raise

        # Start worker loop
        task = asyncio.create_task(self._worker_loop(worker))
        self._worker_tasks[worker_id] = task

        return worker_id

    async def remove_worker(self, worker_id: int, wait: bool = True) -> None:
        """Remove a worker from the pool.

        Args:
            worker_id: ID of the worker to remove
            wait: If True, wait for current task to complete before stopping
        """
        if worker_id not in self._workers:
            raise ValueError(f"Worker {worker_id} not found")

        worker = self._workers[worker_id]

        # Mark as stopping
        worker.mark_stopping()

        # Wait for current task if requested
        if wait and worker.status == WorkerStatus.BUSY:
            logger.info(f"Waiting for worker {worker_id} to finish current task...")
            await worker.wait_current_task()

        # Cancel worker task
        if worker_id in self._worker_tasks:
            self._worker_tasks[worker_id].cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_tasks[worker_id]
            del self._worker_tasks[worker_id]

        # Close browser client and optionally terminate Chrome
        if worker.client:
            try:
                # Terminate Chrome process if close_chrome is True
                if self._close_chrome and hasattr(worker.client, "_chrome_process"):
                    chrome_process = worker.client._chrome_process
                    if chrome_process is not None:
                        chrome_process.terminate()
                        logger.info(f"Terminated Chrome for worker {worker_id}")

                await worker.client.__aexit__(None, None, None)
            except Exception as e:
                logger.warning(f"Error closing worker {worker_id}: {e}")

        worker.mark_stopped()
        del self._workers[worker_id]
        logger.info(f"Worker {worker_id} removed")

    # =========================================================================
    # Job Management
    # =========================================================================

    async def submit(
        self,
        task_type: str,
        *args,
        max_retries: int | None = None,
        **kwargs,
    ) -> str:
        """Submit a job to the pool.

        Args:
            task_type: Type of task (e.g., "create_video", "get_details")
            *args: Positional arguments for the task
            max_retries: Max retries for this job. None = use pool default.
            **kwargs: Keyword arguments for the task

        Returns:
            job_id of the submitted job
        """
        job = Job(
            task_type=task_type,
            args=args,
            kwargs=kwargs,
            max_retries=max_retries if max_retries is not None else self._max_retries,
        )

        self._pending_jobs[job.job_id] = job
        self._result_events[job.job_id] = asyncio.Event()
        await self._job_queue.put(job)

        logger.debug(f"Submitted job {job.job_id}: {task_type}")
        return job.job_id

    async def submit_batch(
        self,
        jobs: list[tuple[str, tuple, dict]],
    ) -> list[str]:
        """Submit multiple jobs at once.

        Args:
            jobs: List of (task_type, args, kwargs) tuples

        Returns:
            List of job_ids
        """
        job_ids = []
        for task_type, args, kwargs in jobs:
            job_id = await self.submit(task_type, *args, **kwargs)
            job_ids.append(job_id)
        return job_ids

    def get_result(self, job_id: str) -> JobResult | None:
        """Get result for a completed job.

        Returns:
            JobResult if completed, None if still pending.
        """
        return self._results.get(job_id)

    async def wait_for(self, job_id: str, timeout: float | None = None) -> JobResult:
        """Wait for a specific job to complete.

        Args:
            job_id: ID of the job to wait for
            timeout: Max seconds to wait. None = wait forever.

        Returns:
            JobResult when the job completes

        Raises:
            asyncio.TimeoutError: If timeout exceeded
            KeyError: If job_id not found
        """
        if job_id in self._results:
            return self._results[job_id]

        if job_id not in self._result_events:
            raise KeyError(f"Job {job_id} not found")

        await asyncio.wait_for(self._result_events[job_id].wait(), timeout=timeout)
        return self._results[job_id]

    async def wait_all(self, timeout: float | None = None) -> dict[str, JobResult]:
        """Wait for all pending jobs to complete.

        Args:
            timeout: Max seconds to wait. None = wait forever.

        Returns:
            Dict of job_id -> JobResult for all completed jobs
        """
        # Wait for queue to be empty and all workers to be idle
        start_time = asyncio.get_event_loop().time()

        while True:
            # Check if all jobs are done
            if not self._pending_jobs and self._job_queue.empty():
                all_idle = all(w.status == WorkerStatus.IDLE for w in self._workers.values())
                if all_idle:
                    break

            # Check timeout
            if timeout is not None:
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed >= timeout:
                    raise asyncio.TimeoutError("Timeout waiting for all jobs")

            await asyncio.sleep(0.5)

        return self._results.copy()

    # =========================================================================
    # State Management
    # =========================================================================

    def save_state(self) -> None:
        """Save current state to file."""
        if self._state_file is None:
            return

        # Ensure parent directory exists
        self._state_file.parent.mkdir(parents=True, exist_ok=True)

        # Collect current state
        pending = list(self._pending_jobs.values())
        in_progress = [w.current_job for w in self._workers.values() if w.current_job]

        self._state.completed = self._results.copy()
        self._state.pending = pending
        self._state.in_progress = in_progress

        save_state(self._state, self._state_file)
        logger.debug(f"State saved: {len(self._results)} completed, {len(pending)} pending")

    # =========================================================================
    # Properties
    # =========================================================================

    @property
    def pending_count(self) -> int:
        """Number of pending jobs."""
        return len(self._pending_jobs) + self._job_queue.qsize()

    @property
    def completed_count(self) -> int:
        """Number of completed jobs."""
        return len(self._results)

    @property
    def worker_count(self) -> int:
        """Number of active workers."""
        return len(self._workers)

    @property
    def worker_stats(self) -> dict[int, WorkerStats]:
        """Get statistics for all workers."""
        return {w_id: w.stats for w_id, w in self._workers.items()}

    def get_status(self) -> dict[str, Any]:
        """Get full status of the pool."""
        return {
            "running": self._running,
            "workers": {w_id: w.to_dict() for w_id, w in self._workers.items()},
            "pending_jobs": len(self._pending_jobs),
            "queue_size": self._job_queue.qsize(),
            "completed_jobs": len(self._results),
            "success_count": sum(1 for r in self._results.values() if r.success),
            "fail_count": sum(1 for r in self._results.values() if not r.success),
        }

    # =========================================================================
    # Internal Methods
    # =========================================================================

    async def _worker_loop(self, worker: Worker) -> None:
        """Main loop for a worker - pulls jobs from queue and executes them."""
        while self._running and worker.status != WorkerStatus.STOPPING:
            try:
                # Try to get a job with timeout (so we can check status)
                try:
                    job = await asyncio.wait_for(self._job_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                # Execute the job
                worker.mark_busy(job)
                job.status = JobStatus.IN_PROGRESS

                try:
                    import time

                    start_time = time.time()
                    result_data = await self._execute_job(worker, job)
                    elapsed = time.time() - start_time

                    # Success
                    result = JobResult(
                        job_id=job.job_id,
                        success=True,
                        data=result_data,
                        worker_id=worker.worker_id,
                    )
                    self._results[job.job_id] = result
                    worker.stats.success += 1
                    worker.stats.total_time += elapsed

                    # Remove from pending
                    self._pending_jobs.pop(job.job_id, None)
                    job.status = JobStatus.COMPLETED

                    # Signal waiters
                    if job.job_id in self._result_events:
                        self._result_events[job.job_id].set()

                    logger.info(
                        f"Worker {worker.worker_id} completed {job.task_type} "
                        f"({job.job_id[:8]}...) in {elapsed:.1f}s"
                    )

                except Exception as e:
                    # Failure - retry or mark as failed
                    job.retries += 1
                    worker.stats.fail += 1

                    if job.max_retries == -1 or job.retries < job.max_retries:
                        # Re-queue for retry
                        job.status = JobStatus.PENDING
                        await self._job_queue.put(job)
                        logger.warning(
                            f"Worker {worker.worker_id} failed {job.task_type} "
                            f"({job.job_id[:8]}...), retry {job.retries}: {e}"
                        )
                    else:
                        # Max retries exceeded
                        result = JobResult(
                            job_id=job.job_id,
                            success=False,
                            error=str(e),
                            worker_id=worker.worker_id,
                        )
                        self._results[job.job_id] = result
                        self._pending_jobs.pop(job.job_id, None)
                        job.status = JobStatus.FAILED

                        if job.job_id in self._result_events:
                            self._result_events[job.job_id].set()

                        logger.error(
                            f"Worker {worker.worker_id} failed {job.task_type} "
                            f"({job.job_id[:8]}...) after {job.retries} retries: {e}"
                        )

                finally:
                    worker.mark_idle()
                    self.save_state()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker {worker.worker_id} loop error: {e}")
                await asyncio.sleep(1)  # Prevent tight loop on error

    async def _execute_job(self, worker: Worker, job: Job) -> Any:
        """Execute a job on a worker.

        Args:
            worker: The worker to use
            job: The job to execute

        Returns:
            Result data from the job execution
        """
        client = worker.client
        if client is None:
            raise RuntimeError(f"Worker {worker.worker_id} has no client")

        task_type = job.task_type
        args = job.args
        kwargs = job.kwargs

        # Dispatch based on task type
        if task_type == "create_video":
            result = await client.create_video(*args, **kwargs)
            return {
                "video_id": result.video_id,
                "moderated": result.moderated,
                "parent_post_id": result.parent_post_id,
            }

        elif task_type == "create_video_via_ui":
            result = await client.create_video_via_ui(*args, **kwargs)
            return {
                "video_id": result.video_id,
                "moderated": result.moderated,
                "parent_post_id": result.parent_post_id,
            }

        elif task_type == "list_posts":
            posts = await client.list_posts(*args, **kwargs)
            return [p.to_dict() if hasattr(p, "to_dict") else p for p in posts]

        elif task_type == "get_post_details":
            post = await client.get_post_details(*args, **kwargs)
            return post._raw_data if hasattr(post, "_raw_data") else post

        elif task_type == "like_post":
            return await client.like_post(*args, **kwargs)

        elif task_type == "unlike_post":
            return await client.unlike_post(*args, **kwargs)

        else:
            raise ValueError(f"Unknown task type: {task_type}")
