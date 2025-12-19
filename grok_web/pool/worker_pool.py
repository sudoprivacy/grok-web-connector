"""BrowserWorkerPool - Manage multiple concurrent browser workers."""

import asyncio
import contextlib
import logging
import platform
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from ..browser import find_nodriver_chromes, get_available_port, get_pid_on_port
from ..client import NodriverClient
from ..models import GrokCookies
from .job import Job, JobResult, JobStatus
from .persistence import PoolState, load_state, save_state
from .worker import Worker, WorkerStats, WorkerStatus

logger = logging.getLogger(__name__)


def _kill_process_tree(pid: int) -> None:
    """Kill a process and all its children (process tree).

    On Windows, Chrome spawns child processes that continue running even after
    the parent is killed. We need to kill the entire process tree.

    Args:
        pid: Process ID to kill (including all descendants)
    """
    try:
        if platform.system() == "Windows":
            # Use taskkill /T to kill process tree on Windows
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=10,
            )
        else:
            # On Unix-like systems, use SIGKILL
            import contextlib
            import os
            import signal

            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception as e:
        logger.warning(f"Failed to kill process tree for PID {pid}: {e}")


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
        state_file: Path | str | None = None,
        max_retries: int = -1,
        config_path: Path | str | None = None,
        cookies: GrokCookies | None = None,
        headless: bool = False,
        close_chrome: bool = True,
        fail_condition: Callable[[dict], bool] | None = None,
        requeue_position: Literal["front", "back"] = "back",
    ):
        """
        Initialize BrowserWorkerPool.

        Args:
            num_workers: Initial number of workers to start
            state_file: Path to state file for persistence. None = no persistence.
            max_retries: Max retries per job. -1 = unlimited retries.
            config_path: Path to grok config file (default: ~/.grok-config.json)
            cookies: Pre-loaded GrokCookies. If None, loads from config.
            headless: Run Chrome in headless mode.
            close_chrome: Terminate Chrome processes on pool exit. Default True.
            fail_condition: Callable that takes result data dict and returns True if
                the job should be considered failed (even if no exception was raised).
                Example: `lambda r: r.get("moderated", False)` to fail moderated videos.
            requeue_position: Where to put failed jobs for retry: "front" (high priority)
                or "back" (normal priority). Default "back".

        Port Allocation:
            Ports are allocated automatically. The pool first reuses existing nodriver
            Chrome instances (identified by grok_chrome_ temp profile), then launches
            new Chrome on available ports as needed.
        """
        self._num_workers = num_workers
        self._state_file = Path(state_file) if state_file else None
        self._max_retries = max_retries
        self._config_path = config_path
        self._cookies = cookies
        self._headless = headless
        self._close_chrome = close_chrome
        self._fail_condition = fail_condition
        self._requeue_position = requeue_position

        # Worker management
        self._workers: dict[int, Worker] = {}
        self._worker_tasks: dict[int, asyncio.Task] = {}
        self._next_worker_id = 0
        self._used_ports: set[int] = set()  # Ports currently in use by workers
        self._available_nodriver_ports: list[int] = []  # Existing nodriver Chrome to reuse

        # Job management - two queues for priority support
        self._job_queue: asyncio.Queue[Job] = asyncio.Queue()  # Normal priority
        self._priority_queue: asyncio.Queue[Job] = asyncio.Queue()  # High priority (front)
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

        # Find existing nodriver Chrome instances to reuse
        self._available_nodriver_ports = find_nodriver_chromes()
        if self._available_nodriver_ports:
            logger.info(
                f"Found {len(self._available_nodriver_ports)} existing nodriver Chrome: {self._available_nodriver_ports}"
            )

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
                    # Terminate Chrome process if close_chrome is True AND we launched it
                    # (if we reused existing Chrome, _chrome_process will be None)
                    if self._close_chrome and hasattr(worker.client, "_chrome_process"):
                        chrome_process = worker.client._chrome_process
                        if chrome_process is not None:
                            # Chrome launches child processes and the parent exits quickly
                            # Find the actual Chrome PID by port
                            actual_pid = get_pid_on_port(worker.port)
                            if actual_pid:
                                logger.info(
                                    f"Killing Chrome process tree (PID: {actual_pid}) for worker {worker.worker_id}"
                                )
                                # Kill the entire process tree (parent + all children)
                                _kill_process_tree(actual_pid)

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

        Port allocation strategy:
        1. First, try to reuse existing nodriver Chrome instances
        2. If none available, find an available port and launch new Chrome

        Returns:
            worker_id of the new worker
        """
        worker_id = self._next_worker_id
        self._next_worker_id += 1

        # Smart port allocation
        port = None
        reusing = False

        # Try to reuse an existing nodriver Chrome
        while self._available_nodriver_ports:
            candidate = self._available_nodriver_ports.pop(0)
            if candidate not in self._used_ports:
                port = candidate
                reusing = True
                break

        # If no reusable Chrome, find an available port
        if port is None:
            port = get_available_port(exclude=self._used_ports)

        self._used_ports.add(port)

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
            action = "reusing" if reusing else "launched new"
            logger.info(f"Worker {worker_id} started ({action} Chrome on port {port})")
        except Exception as e:
            logger.error(f"Failed to start worker {worker_id}: {e}")
            worker.status = WorkerStatus.ERROR
            self._used_ports.discard(port)
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
                # Terminate Chrome process if close_chrome is True AND we launched it
                # (if we reused existing Chrome, _chrome_process will be None)
                if self._close_chrome and hasattr(worker.client, "_chrome_process"):
                    chrome_process = worker.client._chrome_process
                    if chrome_process is not None:
                        # Chrome launches child processes and the parent exits quickly
                        # Find the actual Chrome PID by port
                        actual_pid = get_pid_on_port(worker.port)
                        if actual_pid:
                            logger.info(
                                f"Killing Chrome process tree (PID: {actual_pid}) for worker {worker_id}"
                            )
                            # Kill the entire process tree (parent + all children)
                            _kill_process_tree(actual_pid)

                await worker.client.__aexit__(None, None, None)
            except Exception as e:
                logger.warning(f"Error closing worker {worker_id}: {e}")

        # Release the port
        self._used_ports.discard(worker.port)

        worker.mark_stopped()
        del self._workers[worker_id]
        logger.info(f"Worker {worker_id} removed (port {worker.port} released)")

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
            # Check if all jobs are done (both queues must be empty)
            queues_empty = self._job_queue.empty() and self._priority_queue.empty()
            if not self._pending_jobs and queues_empty:
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
        """Number of pending jobs (both queues)."""
        return len(self._pending_jobs) + self._job_queue.qsize() + self._priority_queue.qsize()

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
            "priority_queue_size": self._priority_queue.qsize(),
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
                # Try priority queue first (non-blocking), then regular queue
                job = None
                with contextlib.suppress(asyncio.QueueEmpty):
                    job = self._priority_queue.get_nowait()

                if job is None:
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

                    # Check fail_condition (soft failure)
                    if self._fail_condition and self._fail_condition(result_data):
                        # Soft failure - requeue
                        await self._handle_job_failure(
                            worker, job, f"fail_condition returned True: {result_data}"
                        )
                    else:
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
                    # Hard failure - exception raised
                    await self._handle_job_failure(worker, job, str(e))

                finally:
                    worker.mark_idle()
                    self.save_state()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker {worker.worker_id} loop error: {e}")
                await asyncio.sleep(1)  # Prevent tight loop on error

    async def _handle_job_failure(self, worker: Worker, job: Job, error: str) -> None:
        """Handle job failure - retry or mark as failed.

        Args:
            worker: The worker that executed the job
            job: The failed job
            error: Error message describing the failure
        """
        job.retries += 1
        worker.stats.fail += 1

        if job.max_retries == -1 or job.retries < job.max_retries:
            # Re-queue for retry
            job.status = JobStatus.PENDING
            if self._requeue_position == "front":
                await self._priority_queue.put(job)
            else:
                await self._job_queue.put(job)
            logger.warning(
                f"Worker {worker.worker_id} failed {job.task_type} "
                f"({job.job_id[:8]}...), retry {job.retries}: {error}"
            )
        else:
            # Max retries exceeded
            result = JobResult(
                job_id=job.job_id,
                success=False,
                error=error,
                worker_id=worker.worker_id,
            )
            self._results[job.job_id] = result
            self._pending_jobs.pop(job.job_id, None)
            job.status = JobStatus.FAILED

            if job.job_id in self._result_events:
                self._result_events[job.job_id].set()

            logger.error(
                f"Worker {worker.worker_id} failed {job.task_type} "
                f"({job.job_id[:8]}...) after {job.retries} retries: {error}"
            )

    def _serialize_result(self, result: Any) -> Any:
        """Convert result to JSON-serializable format.

        Uses Pydantic's model_dump() if available, otherwise smart conversion.
        This ensures consistent serialization across all task types while maintaining
        zero maintenance overhead.

        Args:
            result: The result object from client method call

        Returns:
            JSON-serializable dict, list, or primitive value
        """
        # Pydantic models (VideoGenerationResult, PostDetails, etc.)
        if hasattr(result, "model_dump"):
            return result.model_dump(
                mode="python",
                exclude_none=True,  # Skip None values for cleaner output
                exclude_unset=False,
            )

        # List of results (e.g., list_posts returns List[PostSummary])
        if isinstance(result, list):
            return [self._serialize_result(item) for item in result]

        # Already serializable primitive types
        if isinstance(result, dict | str | int | float | bool | type(None)):
            return result

        # Objects with to_dict() method (legacy support)
        if hasattr(result, "to_dict") and callable(result.to_dict):
            return result.to_dict()

        # Fallback: convert __dict__ to dict, excluding private attributes
        if hasattr(result, "__dict__"):
            return {k: v for k, v in result.__dict__.items() if not k.startswith("_")}

        # Return as-is (will fail during JSON serialization if not compatible)
        return result

    async def _execute_job(self, worker: Worker, job: Job) -> Any:
        """Execute a job by dynamically calling the client method.

        The task_type directly maps to NodriverClient method names.
        This approach maintains zero maintenance overhead - adding new client methods
        automatically makes them available through the worker pool.

        Examples:
            task_type="create_video" → calls client.create_video(*args, **kwargs)
            task_type="list_posts" → calls client.list_posts(*args, **kwargs)
            task_type="delete_video" → calls client.delete_video(*args, **kwargs)

        Args:
            worker: The worker to use
            job: The job to execute

        Returns:
            JSON-serializable result data (automatically serialized via _serialize_result)

        Raises:
            RuntimeError: If worker has no client
            ValueError: If task_type doesn't match any client method
            AttributeError: If task_type is not callable
        """
        client = worker.client
        if client is None:
            raise RuntimeError(f"Worker {worker.worker_id} has no client")

        # Copy kwargs to avoid modifying original job
        kwargs = dict(job.kwargs)

        # Extract ui_delay if present (for UI operations)
        ui_delay = kwargs.pop("ui_delay", None)
        if ui_delay is not None:
            original_ui_delay = client._ui_delay
            client._ui_delay = ui_delay

        try:
            # Dynamic method lookup - task_type must match a client method name
            try:
                method = getattr(client, job.task_type)
            except AttributeError:
                # Provide helpful error message with available methods
                available = [
                    m
                    for m in dir(client)
                    if not m.startswith("_") and callable(getattr(client, m, None))
                ]
                raise ValueError(
                    f"Unknown task_type: '{job.task_type}'. "
                    f"Available client methods: {', '.join(sorted(available)[:20])}..."
                ) from None

            # Verify it's callable
            if not callable(method):
                raise ValueError(f"task_type '{job.task_type}' is not a callable method")

            # Call the method with job args/kwargs
            result = await method(*job.args, **kwargs)

            # Serialize result to JSON-compatible format
            return self._serialize_result(result)

        finally:
            # Restore original ui_delay
            if ui_delay is not None:
                client._ui_delay = original_ui_delay
