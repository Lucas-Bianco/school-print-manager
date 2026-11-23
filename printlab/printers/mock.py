"""A pretend printer for trying the system without real hardware.

`host` is used as a speed-up factor (e.g. "60" makes a 1 h print take 1 min).
"""
import time
from pathlib import Path

from .base import (FINISHED, IDLE, PAUSED, PRINTING, STOPPED, Adapter,
                   PrinterError, Status)


class MockAdapter(Adapter):
    file_kinds = {"bgcode", "gcode", "3mf"}

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        try:
            self.speed = max(float(cfg.get("host") or 60), 1)
        except ValueError:
            self.speed = 60
        self.state = IDLE
        self.duration = 0.0
        self.elapsed = 0.0
        self.last = time.monotonic()

    def _advance(self):
        now = time.monotonic()
        if self.state == PRINTING:
            self.elapsed += (now - self.last) * self.speed
            if self.elapsed >= self.duration:
                self.state = FINISHED
        self.last = now

    async def get_status(self) -> Status:
        self._advance()
        if self.state in (PRINTING, PAUSED):
            pct = min(100.0, 100 * self.elapsed / self.duration) if self.duration else 0
            return Status(self.state, round(pct, 1), int(self.duration - self.elapsed))
        return Status(self.state)

    async def start_job(self, path: Path, remote_name: str, job: dict) -> None:
        self._advance()
        if self.state not in (IDLE, FINISHED, STOPPED):
            raise PrinterError(f"printer is {self.state}")
        self.duration = float(job["est_seconds"])
        self.elapsed = 0.0
        self.state = PRINTING

    async def pause(self) -> None:
        self._advance()
        if self.state == PRINTING:
            self.state = PAUSED

    async def resume(self) -> None:
        self._advance()
        if self.state == PAUSED:
            self.state = PRINTING

    async def stop(self) -> None:
        self._advance()
        if self.state in (PRINTING, PAUSED):
            self.state = STOPPED
