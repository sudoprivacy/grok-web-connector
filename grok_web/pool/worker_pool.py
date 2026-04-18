"""BrowserWorkerPool - Grok-specific BrowserPool subclass.

Thin wrapper around ai-dev-browser's BrowserPool that adds:
- Per-worker Chrome profiles (grok-chrome-w0, w1, ...)
- Clean CDP shutdown via browser_stop()
- Consecutive failure detection with browser health check
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from ai_dev_browser.core import browser_stop, is_port_in_use
from ai_dev_browser.pool import BrowserPool
from ai_dev_browser.pool.job import Job, JobResult, JobStatus
from ai_dev_browser.pool.worker import Worker, WorkerStatus

from ..client import GROK_CHROME_PROFILE, GrokClient
from ..exceptions import GrokError
from ..models import GrokCookies

logger = logging.getLogger(__name__)


# Substrings that, when seen in an exception's string form, strongly suggest
# the Chrome/CDP transport is dead — the connection was refused, the socket
# was reset, or the WebSocket hung up. Anything matching these counts toward
# the dead-browser detector; plain business errors (GrokAPIError "No match..."
# etc.) do not.
_INFRA_ERROR_HINTS = (
    "connection refused",
    "connectionrefusederror",
    "connectionreseterror",
    "winerror 1225",  # no connection due to target active refusal (Windows)
    "winerror 10054",  # connection reset by peer
    "websocket",
    "protocolexception",
    "cdp command timed out",
    "target closed",
    "browser disconnected",
)


def _is_infra_failure(exc: BaseException) -> bool:
    """Return True if the exception looks like browser/transport trouble
    rather than a legitimate business failure.

    Business failures inherit from GrokError (GrokAPIError, GrokNotFoundError,
    etc.) — we explicitly exclude those. Everything else is checked against a
    small allow-list of strings that actually indicate a dead CDP connection.
    """
    if isinstance(exc, GrokError):
        return False
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(hint in text for hint in _INFRA_ERROR_HINTS)


class BrowserWorkerPool(BrowserPool[GrokClient]):
    """
    Manage multiple concurrent browser workers with job queuing and persistence.

    Extends ai-dev-browser's BrowserPool with Grok-specific behavior:
    - Per-worker named Chrome profiles for session isolation
    - Clean Chrome shutdown via CDP Browser.close() (browser_stop)
    - Browser health monitoring with fail-fast on dead Chrome

    Example:
        async with BrowserWorkerPool(num_workers=3, state_file="progress.json") as pool:
            # Submit jobs
            job_id = await pool.submit("create_video", post_id="abc", adjustment_prompt="Orbit")

            # Dynamically add worker
            await pool.add_worker()

            # Wait for all jobs
            results = await pool.wait()
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
                IMPORTANT: Set to False when using create_image() - the image gallery
                is ephemeral (not saved) and closing Chrome will lose all generated
                images. Keep Chrome open to browse and select images from the gallery.
            fail_condition: Callable that takes result data dict and returns True if
                the job should be considered failed (even if no exception was raised).
                Example: `lambda r: r.get("moderated", False)` to fail moderated videos.
            requeue_position: Where to put failed jobs for retry: "front" (high priority)
                or "back" (normal priority). Default "back".

        Port Allocation:
            Ports are allocated automatically. The pool first reuses existing
            Chrome instances (identified by grok_chrome_ temp profile), then launches
            new Chrome on available ports as needed.
        """
        # Grok-specific params (not in base class)
        self._config_path = config_path
        self._cookies = cookies

        super().__init__(
            client_class=GrokClient,
            workers=num_workers,
            max_retries=max_retries,
            state_file=state_file,
            headless=headless,
            close_browsers=close_chrome,
            fail_condition=fail_condition,
            requeue_position=requeue_position,
            profile="temp",  # grok manages cookies via CDP, not cookies.dat
        )

    # =========================================================================
    # Overrides
    # =========================================================================

    async def add_worker(self) -> int:
        """Add a new worker to the pool.

        Each worker gets a named profile (e.g., "grok-chrome-w0") for Chrome
        reuse across runs. Port allocation is handled by browser_start().

        Returns:
            worker_id of the new worker
        """
        worker_id = self._next_worker_id
        self._next_worker_id += 1

        # Each worker gets its own named profile for Chrome reuse
        profile = f"{GROK_CHROME_PROFILE}-w{worker_id}"

        # Initialize browser client with per-worker profile.
        # force_new_chrome=True -> pass reuse="none" to browser_start. This
        # SKIPS ai-dev-browser's workspace-wide reuse (which is
        # profile-agnostic and would collapse all workers onto the first
        # existing Chrome), while STILL honoring the profile-dir-conflict
        # safety path — i.e. cross-run reuse of the same per-worker profile
        # is preserved. Without this, multiple workers silently share one
        # Chrome on one port, defeating the whole point of the pool.
        client = GrokClient(
            cookies=self._cookies,
            config_path=self._config_path,
            headless=self._headless,
            auto_launch=True,
            profile=profile,
            force_new_chrome=True,
        )

        try:
            await client.__aenter__()
            # Port is determined by browser_start, read it back
            actual_port = client._remote_port
            worker = Worker(worker_id=worker_id, port=actual_port)
            self._workers[worker_id] = worker
            self._used_ports.add(actual_port)
            worker.client = client
            logger.info(f"Worker {worker_id} started on port {actual_port} (profile: {profile})")
        except Exception as e:
            logger.error(f"Failed to start worker {worker_id}: {e}")
            raise

        # Start worker loop
        task = asyncio.create_task(self._worker_loop(worker))
        self._worker_tasks[worker_id] = task

        return worker_id

    async def _close_worker_client(self, worker: Worker) -> None:
        """Close worker client and optionally stop Chrome via CDP.

        Uses browser_stop() for clean shutdown (flushes cookies via
        CDP Browser.close()) instead of force-killing the process.
        """
        if not worker.client:
            return

        try:
            # First, call __aexit__ to save cookies and release CDP connection
            # This must happen BEFORE killing Chrome, otherwise cookies can't be saved
            await worker.client.__aexit__(None, None, None)

            # Then, terminate Chrome if close_browsers is True
            if self._close_browsers and worker.port:
                try:
                    browser_stop(port=worker.port)
                    logger.info(
                        f"Stopped Chrome on port {worker.port} for worker {worker.worker_id}"
                    )
                except Exception:
                    pass  # Chrome may already be gone
        except Exception as e:
            logger.warning(f"Error closing worker {worker.worker_id}: {e}")

    async def _worker_loop(self, worker: Worker) -> None:
        """Main loop for a worker - pulls jobs from queue and executes them.

        Extends base _worker_loop with a fail-fast branch for genuine
        browser death: if we rack up consecutive "infrastructure" failures
        AND the Chrome port is no longer reachable, this worker exits its
        loop and leaves the shared job queues alone. Other live workers
        keep consuming — we never drain pending jobs out from under them.

        Only failures that plausibly indicate browser trouble count toward
        the consecutive_failures tally. Legitimate business failures
        (GrokAPIError, ValueError, etc.) are not symptoms of a dead
        browser — they reset the counter.
        """
        consecutive_failures = 0
        max_consecutive_failures = 3

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
                    result_data, business_success = await self._execute_job(worker, job)
                    elapsed = time.time() - start_time

                    # Check fail_condition (soft failure)
                    if self._fail_condition and self._fail_condition(result_data):
                        await self._handle_job_failure(
                            worker, job, f"fail_condition returned True: {result_data}"
                        )
                        # Soft failures reflect business state (e.g. moderated
                        # output), not browser health — don't count them.
                    else:
                        # Success
                        result = JobResult(
                            job_id=job.job_id,
                            success=business_success,
                            data=result_data,
                            worker_id=worker.worker_id,
                        )
                        self._results[job.job_id] = result
                        worker.stats.success += 1
                        worker.stats.total_time += elapsed

                        self._pending_jobs.pop(job.job_id, None)
                        job.status = JobStatus.COMPLETED

                        if job.job_id in self._result_events:
                            self._result_events[job.job_id].set()

                        logger.info(
                            f"Worker {worker.worker_id} completed {job.task_type} "
                            f"({job.job_id[:8]}...) in {elapsed:.1f}s"
                        )
                        consecutive_failures = 0

                except Exception as e:
                    await self._handle_job_failure(worker, job, str(e))
                    if _is_infra_failure(e):
                        consecutive_failures += 1
                    else:
                        # Legitimate business failure ("No matching video
                        # found", validation errors, etc.) — reset the
                        # browser-health counter. Repeated business misses
                        # must NOT be interpreted as a dead browser.
                        consecutive_failures = 0

                finally:
                    worker.mark_idle()
                    self.save_state()

                # Fail-fast: if only infra-style failures keep piling up AND
                # Chrome is unreachable, stop THIS worker. Do NOT drain the
                # shared queues — surviving workers on other Chromes must
                # keep processing the backlog.
                if (
                    consecutive_failures >= max_consecutive_failures
                    and not self._check_browser_alive(worker)
                ):
                    logger.error(
                        f"Worker {worker.worker_id} browser on port {worker.port} "
                        f"is dead after {consecutive_failures} consecutive failures; "
                        f"stopping this worker — other workers continue."
                    )
                    worker.mark_stopping()
                    break
                    break

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker {worker.worker_id} loop error: {e}")
                await asyncio.sleep(1)  # Prevent tight loop on error

    # =========================================================================
    # Grok-specific health checks
    # =========================================================================

    def _check_browser_alive(self, worker: Worker) -> bool:
        """Check if the worker's Chrome is still reachable."""
        if not worker.port:
            return False
        return is_port_in_use(port=worker.port)

    async def _fail_all_pending_jobs(self, worker: Worker, reason: str) -> None:
        """Fail all remaining pending jobs for a dead worker."""
        drained: list[Job] = []
        while not self._priority_queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                drained.append(self._priority_queue.get_nowait())
        while not self._job_queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                drained.append(self._job_queue.get_nowait())

        for job in drained:
            result = JobResult(
                job_id=job.job_id,
                success=False,
                error=reason,
                worker_id=worker.worker_id,
            )
            self._results[job.job_id] = result
            self._pending_jobs.pop(job.job_id, None)
            job.status = JobStatus.FAILED
            if job.job_id in self._result_events:
                self._result_events[job.job_id].set()
