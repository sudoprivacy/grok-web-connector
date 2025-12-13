"""State persistence for BrowserWorkerPool."""

import contextlib
import json
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .job import Job, JobResult

STATE_VERSION = 1


@dataclass
class PoolState:
    """Serializable state of the worker pool."""

    version: int = STATE_VERSION
    last_updated: datetime = field(default_factory=datetime.now)
    completed: dict[str, "JobResult"] = field(default_factory=dict)
    pending: list["Job"] = field(default_factory=list)
    in_progress: list["Job"] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize state to dict."""
        return {
            "version": self.version,
            "last_updated": self.last_updated.isoformat(),
            "completed": {job_id: result.to_dict() for job_id, result in self.completed.items()},
            "pending": [job.to_dict() for job in self.pending],
            "in_progress": [job.to_dict() for job in self.in_progress],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PoolState":
        """Deserialize state from dict."""
        from .job import Job, JobResult

        version = data.get("version", 1)
        if version != STATE_VERSION:
            # Handle version migration if needed in the future
            pass

        return cls(
            version=version,
            last_updated=datetime.fromisoformat(data["last_updated"]),
            completed={
                job_id: JobResult.from_dict(result_data)
                for job_id, result_data in data.get("completed", {}).items()
            },
            pending=[Job.from_dict(j) for j in data.get("pending", [])],
            in_progress=[Job.from_dict(j) for j in data.get("in_progress", [])],
        )


def save_state(state: PoolState, file_path: Path | str) -> None:
    """Save pool state to file atomically.

    Uses atomic write (write to temp file, then rename) to prevent corruption.

    Args:
        state: The pool state to save
        file_path: Path to the state file
    """
    file_path = Path(file_path)
    state.last_updated = datetime.now()
    data = state.to_dict()

    # Write to temp file in same directory for atomic rename
    temp_fd, temp_path = tempfile.mkstemp(
        dir=file_path.parent, prefix=".pool_state_", suffix=".tmp"
    )
    try:
        with open(temp_fd, "w") as f:
            json.dump(data, f, indent=2)
        # Atomic rename
        Path(temp_path).replace(file_path)
    except Exception:
        # Clean up temp file on error
        with contextlib.suppress(OSError):
            Path(temp_path).unlink()
        raise


def load_state(file_path: Path | str) -> PoolState | None:
    """Load pool state from file.

    Args:
        file_path: Path to the state file

    Returns:
        PoolState if file exists and is valid, None otherwise.
    """
    file_path = Path(file_path)
    if not file_path.exists():
        return None

    try:
        with open(file_path) as f:
            data = json.load(f)
        return PoolState.from_dict(data)
    except (json.JSONDecodeError, KeyError, ValueError):
        # Corrupted or invalid state file
        return None
