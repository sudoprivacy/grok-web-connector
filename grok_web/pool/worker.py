"""Worker class for BrowserWorkerPool."""

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..client import NodriverClient
    from .job import Job


class WorkerStatus(Enum):
    """Status of a worker."""

    IDLE = "idle"
    BUSY = "busy"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class WorkerStats:
    """Statistics for a worker."""

    success: int = 0
    fail: int = 0
    total_time: float = 0.0  # seconds

    @property
    def total(self) -> int:
        return self.success + self.fail

    @property
    def success_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return self.success / self.total


@dataclass
class Worker:
    """A browser worker that executes jobs."""

    worker_id: int
    port: int
    client: "NodriverClient | None" = None
    status: WorkerStatus = WorkerStatus.IDLE
    current_job: "Job | None" = None
    stats: WorkerStats = field(default_factory=WorkerStats)
    _task_done_event: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self):
        # Set event initially (no task running)
        self._task_done_event.set()

    async def wait_current_task(self, timeout: float | None = None) -> bool:
        """Wait for the current task to complete.

        Args:
            timeout: Maximum time to wait in seconds. None = wait forever.

        Returns:
            True if task completed, False if timeout.
        """
        try:
            await asyncio.wait_for(self._task_done_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def mark_busy(self, job: "Job"):
        """Mark worker as busy with a job."""
        self.status = WorkerStatus.BUSY
        self.current_job = job
        self._task_done_event.clear()

    def mark_idle(self):
        """Mark worker as idle (task completed)."""
        self.status = WorkerStatus.IDLE
        self.current_job = None
        self._task_done_event.set()

    def mark_stopping(self):
        """Mark worker as stopping (will finish current task then stop)."""
        self.status = WorkerStatus.STOPPING

    def mark_stopped(self):
        """Mark worker as fully stopped."""
        self.status = WorkerStatus.STOPPED
        self.current_job = None
        self._task_done_event.set()

    def to_dict(self) -> dict:
        """Serialize worker state for status reporting."""
        return {
            "worker_id": self.worker_id,
            "port": self.port,
            "status": self.status.value,
            "current_job_id": self.current_job.job_id if self.current_job else None,
            "stats": {
                "success": self.stats.success,
                "fail": self.stats.fail,
                "total": self.stats.total,
                "success_rate": round(self.stats.success_rate, 2),
            },
        }
