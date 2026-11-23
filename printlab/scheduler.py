"""Background loop: polls printers, finishes jobs, and starts the next one when a bed is cleared."""
import asyncio
import logging
from datetime import datetime
from pathlib import Path

from .db import DB, now_iso
from .printers import FILE_KINDS, PrinterError, make_adapter
from .printers.base import (ACCEPTS_JOBS, ACTIVE, ERROR, FINISHED, IDLE,
                            OFFLINE, STOPPED)

log = logging.getLogger("printlab.scheduler")

ADAPTER_FIELDS = ("kind", "host", "api_key", "username", "password", "serial",
                  "access_code", "storage", "use_ams")
START_GRACE_S = 240   # a job that never shows as printing within this time is marked failed


def models_of(printer: dict) -> set[str]:
    return {m.strip().upper() for m in printer["models"].split(",") if m.strip()}


def compatible(job: dict, printer: dict) -> bool:
    if job["file_kind"] not in FILE_KINDS.get(printer["kind"], set()):
        return False
    if job["target_printer_id"]:
        return job["target_printer_id"] == printer["id"]
    models = models_of(printer)
    if not models:          # printer with no model list accepts any file of its kind
        return True
    return job["printer_model"].upper() in models


def remote_name(job: dict) -> str:
    ext = {"bgcode": ".bgcode", "gcode": ".gcode", "3mf": ".gcode.3mf"}[job["file_kind"]]
    return f"job{job['id']}{ext}"


def _seconds_since(iso: str | None) -> float:
    if not iso:
        return 1e9
    return (datetime.now() - datetime.fromisoformat(iso)).total_seconds()


class Scheduler:
    def __init__(self, db: DB, upload_dir: Path, backup_dir: Path):
        self.db = db
        self.upload_dir = upload_dir
        self.backup_dir = backup_dir
        self.adapters: dict[int, tuple[tuple, object]] = {}
        self.lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.last_backup_day = None

    # adapters ---------------------------------------------------------------
    def adapter(self, printer: dict):
        cfg = tuple(printer[f] for f in ADAPTER_FIELDS)
        cached = self.adapters.get(printer["id"])
        if cached and cached[0] == cfg:
            return cached[1]
        if cached:
            asyncio.ensure_future(cached[1].close())
        ad = make_adapter(printer)
        self.adapters[printer["id"]] = (cfg, ad)
        return ad

    async def forget(self, printer_id: int) -> None:
        cached = self.adapters.pop(printer_id, None)
        if cached:
            await cached[1].close()

    async def close(self) -> None:
        for _, ad in self.adapters.values():
            try:
                await ad.close()
            except Exception:
                pass
        self.adapters.clear()

    # main loop ----------------------------------------------------------------
    async def run(self, interval: float = 4.0) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("tick failed")
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), interval)
            except asyncio.TimeoutError:
                pass

    def poke(self) -> None:
        """Run a tick soon (e.g. right after a bed is cleared or a job approved)."""
        self.wake.set()

    async def tick(self) -> None:
        async with self.lock:
            for p in self.db.q("SELECT * FROM printers WHERE enabled=1 ORDER BY sort, id"):
                try:
                    await self._tick_printer(p)
                except Exception as e:
                    log.exception("printer %s", p["name"])
                    self.db.update("printers", p["id"], last_error=f"{type(e).__name__}: {e}")
            self._daily_backup()

    async def _tick_printer(self, p: dict) -> None:
        ad = self.adapter(p)
        st = await ad.get_status()
        fields = dict(live_state=st.state, live_progress=st.progress,
                      live_remaining=st.remaining_s, live_message=st.message)
        if st.state != OFFLINE:
            fields["last_seen"] = now_iso()
        self.db.update("printers", p["id"], **fields)

        job = None
        if p["current_job_id"]:
            job = self.db.one("SELECT * FROM jobs WHERE id=?", (p["current_job_id"],))

        if job and job["status"] == "printing":
            await self._follow_job(p, job, st)
            return
        if job:  # job was cancelled/failed elsewhere; release the printer
            self.db.update("printers", p["id"], current_job_id=None, last_job_id=job["id"],
                           state="needs_clear" if p["state"] == "printing" else p["state"])
            return

        if p["state"] == "ready" and not p["maintenance"] and st.state in ACCEPTS_JOBS:
            await self._start_next(p, ad)

    async def _follow_job(self, p: dict, job: dict, st) -> None:
        since_start = _seconds_since(job["started_at"])
        if st.state in ACTIVE:
            self.db.update("jobs", job["id"], seen_printing=1,
                           progress=st.progress, remaining_s=st.remaining_s)
            return
        if st.state == OFFLINE:
            return
        seen = job["seen_printing"]
        if st.state == FINISHED and (seen or since_start > 60):
            self._end_job(p, job, "done")
        elif st.state in (STOPPED, ERROR) and (seen or since_start > 60):
            reason = "Stopped on the printer" if st.state == STOPPED else "Printer reported an error"
            self._end_job(p, job, "failed", reason, "printer")
        elif st.state == IDLE and seen:
            # Printer went straight back to idle (e.g. someone acknowledged the finish screen).
            self._end_job(p, job, "done")
        elif st.state == IDLE and since_start > START_GRACE_S:
            self._end_job(p, job, "failed", "Printer never started the job", "system")

    def _end_job(self, p: dict, job: dict, status: str, reason: str = "", by: str = "") -> None:
        fields = dict(status=status, finished_at=now_iso())
        if status == "done":
            fields["progress"] = 100
            fields["remaining_s"] = 0
        else:
            fields["fail_reason"] = reason
            fields["fail_reported_by"] = by
        self.db.update("jobs", job["id"], **fields)
        self.db.update("printers", p["id"], state="needs_clear", current_job_id=None, last_job_id=job["id"])
        self.db.log(f"job_{status}", reason, job["id"], p["id"])

    def next_job_for(self, p: dict) -> dict | None:
        for job in self.db.q("SELECT * FROM jobs WHERE status='approved' ORDER BY position, id"):
            if compatible(job, p):
                return job
        return None

    async def _start_next(self, p: dict, ad) -> None:
        job = self.next_job_for(p)
        if not job:
            return
        path = self.upload_dir / job["stored_file"]
        try:
            await ad.start_job(path, remote_name(job), job)
        except (PrinterError, OSError, Exception) as e:  # keep the job queued and show the error
            msg = f"Could not start job {job['id']}: {e}"
            log.warning(msg)
            self.db.update("printers", p["id"], last_error=msg)
            self.db.log("start_failed", str(e), job["id"], p["id"])
            return
        self.db.update("jobs", job["id"], status="printing", printer_id=p["id"],
                       started_at=now_iso(), seen_printing=0, progress=0,
                       remaining_s=job["est_seconds"])
        self.db.update("printers", p["id"], state="printing", current_job_id=job["id"], last_error="")
        self.db.log("job_started", "", job["id"], p["id"])

    # backups ----------------------------------------------------------------
    def _daily_backup(self) -> None:
        today = datetime.now().date()
        if self.last_backup_day == today:
            return
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.db.backup_to(self.backup_dir / f"printlab-{today.isoformat()}.db")
        backups = sorted(self.backup_dir.glob("printlab-*.db"))
        for old in backups[:-14]:
            old.unlink(missing_ok=True)
        self.last_backup_day = today

    # helpers used by the API -----------------------------------------------------
    async def printer_command(self, printer_id: int, command: str) -> None:
        p = self.db.one("SELECT * FROM printers WHERE id=?", (printer_id,))
        if not p:
            raise PrinterError("no such printer")
        ad = self.adapter(p)
        await getattr(ad, command)()

    def estimate_starts(self) -> dict[int, str]:
        """Rough start time per approved job, assuming beds get cleared promptly."""
        now = datetime.now().timestamp()
        free_at = {}
        printers = [p for p in self.db.q("SELECT * FROM printers WHERE enabled=1 AND maintenance=0")]
        for p in printers:
            rem = 0
            if p["current_job_id"]:
                rem = p["live_remaining"] or 0
            free_at[p["id"]] = now + rem
        out = {}
        for job in self.db.q("SELECT * FROM jobs WHERE status='approved' ORDER BY position, id"):
            options = [p for p in printers if compatible(job, p)]
            if not options:
                continue
            best = min(options, key=lambda p: free_at[p["id"]])
            start = free_at[best["id"]]
            out[job["id"]] = datetime.fromtimestamp(start).replace(microsecond=0).isoformat()
            free_at[best["id"]] = start + job["est_seconds"]
        return out
