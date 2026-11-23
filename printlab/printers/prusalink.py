"""Prusa printers (MK4, CORE One, CORE One L, XL, MINI) through PrusaLink's local API (v1).

Needs the printer's IP address plus either its PrusaLink API key or its
PrusaLink username/password (shown on the printer under Settings > Network > PrusaLink).
"""
from pathlib import Path
from urllib.parse import quote

import httpx

from .base import (ATTENTION, BUSY, ERROR, FINISHED, IDLE, OFFLINE, PAUSED,
                   PRINTING, STOPPED, Adapter, PrinterError, Status)

STATE_MAP = {
    "IDLE": IDLE,
    "READY": IDLE,
    "PRINTING": PRINTING,
    "PAUSED": PAUSED,
    "FINISHED": FINISHED,
    "STOPPED": STOPPED,
    "ERROR": ERROR,
    "ATTENTION": ATTENTION,
    "BUSY": BUSY,
}


class PrusaLinkAdapter(Adapter):
    file_kinds = {"bgcode", "gcode"}

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        host = cfg["host"].strip()
        if not host.startswith("http"):
            host = "http://" + host
        self.base = host.rstrip("/")
        headers = {}
        auth = None
        if cfg.get("api_key"):
            headers["X-Api-Key"] = cfg["api_key"]
        else:
            auth = httpx.DigestAuth(cfg.get("username") or "maker", cfg.get("password") or "")
        self.client = httpx.AsyncClient(base_url=self.base, headers=headers, auth=auth, timeout=10)
        self.storage = cfg.get("storage") or "usb"

    async def _status_json(self) -> dict:
        r = await self.client.get("/api/v1/status")
        if r.status_code == 401:
            raise PrinterError("PrusaLink login failed: check the API key or password")
        r.raise_for_status()
        return r.json()

    async def get_status(self) -> Status:
        try:
            data = await self._status_json()
        except PrinterError as e:
            return Status(OFFLINE, message=str(e))
        except (httpx.HTTPError, ValueError) as e:
            return Status(OFFLINE, message=type(e).__name__)
        p = data.get("printer", {})
        job = data.get("job") or {}
        state = STATE_MAP.get(str(p.get("state", "")).upper(), BUSY)
        return Status(
            state=state,
            progress=job.get("progress"),
            remaining_s=job.get("time_remaining"),
            raw=data,
        )

    async def _job_id(self) -> int:
        data = await self._status_json()
        job = data.get("job") or {}
        if "id" not in job:
            raise PrinterError("no active job on the printer")
        return job["id"]

    async def start_job(self, path: Path, remote_name: str, job: dict) -> None:
        url = f"/api/v1/files/{self.storage}/{quote(remote_name)}"
        size = path.stat().st_size
        with open(path, "rb") as fh:
            r = await self.client.put(
                url,
                content=fh.read(),
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(size),
                    "Print-After-Upload": "?1",
                    "Overwrite": "?1",
                },
                timeout=600,
            )
        if r.status_code >= 400:
            raise PrinterError(f"upload failed ({r.status_code}): {r.text[:200]}")

    async def pause(self) -> None:
        jid = await self._job_id()
        (await self.client.put(f"/api/v1/job/{jid}/pause")).raise_for_status()

    async def resume(self) -> None:
        jid = await self._job_id()
        (await self.client.put(f"/api/v1/job/{jid}/resume")).raise_for_status()

    async def stop(self) -> None:
        jid = await self._job_id()
        (await self.client.delete(f"/api/v1/job/{jid}")).raise_for_status()

    async def close(self) -> None:
        await self.client.aclose()
