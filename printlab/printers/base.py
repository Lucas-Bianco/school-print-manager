"""Common interface every printer adapter implements."""
from dataclasses import dataclass, field
from pathlib import Path

# Normalised printer states used everywhere else in the app.
IDLE = "idle"            # ready for a job
PRINTING = "printing"
PAUSED = "paused"
ATTENTION = "attention"  # printer needs a person (filament runout, etc.)
FINISHED = "finished"    # last job completed
STOPPED = "stopped"      # last job was cancelled / failed on the printer
ERROR = "error"
BUSY = "busy"            # preparing, homing, uploading...
OFFLINE = "offline"

ACCEPTS_JOBS = {IDLE, FINISHED, STOPPED}
ACTIVE = {PRINTING, PAUSED, ATTENTION, BUSY}


@dataclass
class Status:
    state: str = OFFLINE
    progress: float | None = None      # 0-100
    remaining_s: int | None = None
    message: str = ""
    raw: dict = field(default_factory=dict)


class PrinterError(RuntimeError):
    pass


class Adapter:
    """Override all of these. All methods are async and must not block the event loop."""

    file_kinds: set[str] = set()   # which uploaded file kinds this printer can run

    def __init__(self, cfg: dict):
        self.cfg = cfg

    async def get_status(self) -> Status:
        raise NotImplementedError

    async def start_job(self, path: Path, remote_name: str, job: dict) -> None:
        raise NotImplementedError

    async def pause(self) -> None:
        raise NotImplementedError

    async def resume(self) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        pass
