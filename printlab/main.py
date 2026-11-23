"""Web server: the upload site, TV board, touch panel, teacher and admin pages, plus the JSON API."""
import asyncio
import csv
import io
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import rules
from .auth import PANEL_SESSION_S, WEB_SESSION_S, Sessions, check_secret, hash_secret
from .db import DB, now_iso
from .fileinfo import FileError, kind_for, read_info
from .printers import FILE_KINDS
from .scheduler import Scheduler, compatible

STATIC = Path(__file__).parent / "static"
COOKIE = "printlab_session"
FAIL_REASONS = ["Spaghetti / messy", "Not sticking to bed", "Layer shift", "Clogged / no filament",
                "Wrong size or file", "Other"]
SECRET_FIELDS = ("api_key", "password", "access_code")
PRINTER_FIELDS = ("name", "kind", "models", "host", "api_key", "username", "password", "serial",
                  "access_code", "storage", "use_ams", "enabled", "maintenance", "maintenance_note", "sort")

log = logging.getLogger("printlab")


def display_name(full: str) -> str:
    parts = full.split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[-1][0].upper()}."
    return full


def create_app(data_dir: Path | None = None, run_scheduler: bool = True) -> FastAPI:
    data_dir = Path(data_dir or os.environ.get("PRINTLAB_DATA") or Path.cwd() / "data")
    upload_dir = data_dir / "uploads"
    thumb_dir = data_dir / "thumbs"
    for d in (data_dir, upload_dir, thumb_dir):
        d.mkdir(parents=True, exist_ok=True)

    db = DB(data_dir / "printlab.db")
    sched = Scheduler(db, upload_dir, data_dir / "backups")
    sessions = Sessions()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(sched.run()) if run_scheduler else None
        yield
        if task:
            task.cancel()
        await sched.close()

    app = FastAPI(title="Print Lab", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.db = db
    app.state.sched = sched
    app.state.sessions = sessions

    # ------------------------------------------------------------ auth helpers
    def session_of(request: Request) -> dict | None:
        token = request.headers.get("X-Panel-Token") or request.cookies.get(COOKIE)
        return sessions.get(token)

    def teacher(request: Request) -> dict:
        s = session_of(request)
        if not s or s["role"] not in ("teacher", "admin"):
            raise HTTPException(401, "Teacher login needed")
        return s

    def admin(request: Request) -> dict:
        s = session_of(request)
        if not s or s["role"] != "admin":
            raise HTTPException(401, "Admin login needed")
        return s

    def get_job(job_id: int) -> dict:
        job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not job:
            raise HTTPException(404, "No such job")
        return job

    def get_printer(pid: int) -> dict:
        p = db.one("SELECT * FROM printers WHERE id=?", (pid,))
        if not p:
            raise HTTPException(404, "No such printer")
        return p

    def public_job(j: dict, starts: dict | None = None) -> dict:
        return {
            "id": j["id"], "name": display_name(j["student_name"]), "title": j["title"],
            "status": j["status"], "est_seconds": j["est_seconds"], "est": rules.fmt_duration(j["est_seconds"]),
            "filament_g": j["filament_g"], "printer_model": j["printer_model"],
            "printer_id": j["printer_id"], "target_printer_id": j["target_printer_id"],
            "progress": j["progress"], "remaining_s": j["remaining_s"],
            "created_at": j["created_at"], "finished_at": j["finished_at"],
            "decision_reason": j["decision_reason"], "reject_reason": j["reject_reason"],
            "fail_reason": j["fail_reason"], "has_thumbnail": bool(j["has_thumbnail"]),
            "est_start": (starts or {}).get(j["id"]), "position": j["position"],
        }

    def public_printer(p: dict, jobs_by_id: dict) -> dict:
        cur = jobs_by_id.get(p["current_job_id"])
        last = jobs_by_id.get(p["last_job_id"])
        if p["maintenance"]:
            shown = "maintenance"
        elif p["live_state"] == "offline":
            shown = "offline"
        elif p["state"] == "needs_clear":
            shown = "needs_clear"
        elif cur:
            shown = p["live_state"] if p["live_state"] in ("paused", "attention") else "printing"
        else:
            shown = "ready"
        return {
            "id": p["id"], "name": p["name"], "kind": p["kind"], "models": p["models"],
            "state": shown, "live_state": p["live_state"], "progress": p["live_progress"],
            "remaining_s": p["live_remaining"], "message": p["live_message"],
            "maintenance_note": p["maintenance_note"], "last_error": p["last_error"],
            "current_job": public_job(cur) if cur else None,
            "last_job": public_job(last) if last else None,
        }

    def snapshot() -> dict:
        printers = db.q("SELECT * FROM printers WHERE enabled=1 ORDER BY sort, id")
        ids = {i for p in printers for i in (p["current_job_id"], p["last_job_id"]) if i}
        jobs_by_id = {}
        if ids:
            marks = ",".join("?" * len(ids))
            jobs_by_id = {j["id"]: j for j in db.q(f"SELECT * FROM jobs WHERE id IN ({marks})", tuple(ids))}
        starts = sched.estimate_starts()
        queue = db.q("SELECT * FROM jobs WHERE status IN ('approved','pending_approval') ORDER BY position, id")
        recent_failed = db.q("SELECT * FROM jobs WHERE status='failed' AND finished_at >= ? ORDER BY finished_at DESC",
                             (datetime.now().replace(hour=0, minute=0, second=0).isoformat(),))
        s = db.all_settings()
        return {
            "now": now_iso(),
            "lab_name": s["lab_name"],
            "rules": {"short_print_minutes": s["short_print_minutes"], "auto_approve_after": s["auto_approve_after"]},
            "printers": [public_printer(p, jobs_by_id) for p in printers],
            "queue": [public_job(j, starts) for j in queue if j["status"] == "approved"],
            "pending": [public_job(j) for j in queue if j["status"] == "pending_approval"],
            "failed_today": [public_job(j) for j in recent_failed],
            "fail_reasons": FAIL_REASONS,
        }

    # ------------------------------------------------------------------ pages
    for page in ("board", "panel", "teacher", "admin"):
        def make(page=page):
            async def handler():
                return FileResponse(STATIC / f"{page}.html")
            return handler
        app.get(f"/{page}", include_in_schema=False)(make())

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # ----------------------------------------------------------- public API
    @app.get("/api/state")
    async def state():
        return snapshot()

    @app.get("/api/printers")
    async def printers_list():
        return [{"id": p["id"], "name": p["name"], "kind": p["kind"], "models": p["models"],
                 "file_kinds": sorted(FILE_KINDS.get(p["kind"], []))}
                for p in db.q("SELECT * FROM printers WHERE enabled=1 ORDER BY sort, id")]

    @app.post("/api/jobs")
    async def upload(name: str = Form(...), class_code: str = Form(""),
                     printer_id: int | None = Form(None), file: UploadFile = File(...)):
        name = " ".join(name.split())[:60]
        if len(name) < 2:
            raise HTTPException(400, "Enter your name.")
        s = db.all_settings()
        codes = {c.strip().upper() for c in s["class_codes"].split(",") if c.strip()}
        class_code = class_code.strip().upper()[:20]
        if codes and class_code not in codes:
            raise HTTPException(400, "That class code isn't valid. Ask your teacher for it.")
        limit = int(s["max_jobs_per_student_per_day"] or 0)
        if limit:
            today = datetime.now().date().isoformat()
            n = db.one("SELECT COUNT(*) AS n FROM jobs WHERE lower(student_name)=lower(?) AND created_at >= ? "
                       "AND status NOT IN ('rejected','cancelled')", (name, today))["n"]
            if n >= limit:
                raise HTTPException(400, f"You've reached today's limit of {limit} prints.")

        original = Path(file.filename or "upload").name
        try:
            kind = kind_for(original)
        except FileError as e:
            raise HTTPException(400, str(e))

        max_bytes = int(s["max_upload_mb"]) * 1024 * 1024
        stored = f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(4)}.{ 'gcode.3mf' if kind == '3mf' else kind}"
        dest = upload_dir / stored
        size = 0
        with open(dest, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(400, f"File is bigger than {s['max_upload_mb']} MB.")
                out.write(chunk)

        try:
            info = await asyncio.to_thread(read_info, dest, original)
        except FileError as e:
            dest.unlink(missing_ok=True)
            raise HTTPException(400, str(e))
        except Exception:
            dest.unlink(missing_ok=True)
            log.exception("could not read %s", original)
            raise HTTPException(400, "Couldn't read this file. Re-slice it and try again.")

        printers = db.q("SELECT * FROM printers WHERE enabled=1")
        candidate = {"file_kind": info.kind, "printer_model": info.printer_model, "target_printer_id": printer_id}
        if printer_id and not any(p["id"] == printer_id for p in printers):
            dest.unlink(missing_ok=True)
            raise HTTPException(400, "That printer isn't available.")
        if printers and not any(compatible(candidate, p) for p in printers):
            dest.unlink(missing_ok=True)
            model = info.printer_model or "an unknown printer"
            raise HTTPException(400, f"This file was sliced for {model}, and no printer here accepts it. "
                                     "Re-slice it for one of our printers, or pick the printer by hand.")

        decision = rules.decide(info.est_seconds, datetime.now(), s)
        pos = (db.one("SELECT MAX(position) AS m FROM jobs")["m"] or 0) + 1
        title = re.sub(r"\.(gcode\.3mf|bgcode|gcode|3mf)$", "", original, flags=re.I)[:80]
        job_id = db.x(
            "INSERT INTO jobs(created_at, student_name, class_code, title, stored_file, file_kind, printer_model,"
            " target_printer_id, est_seconds, filament_g, plate, status, decision_reason, position, reject_reason,"
            " approved_at, approved_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), name, class_code, title, stored, info.kind, info.printer_model, printer_id,
             info.est_seconds, info.filament_g, info.plate, decision.status, decision.reason, pos,
             decision.reason if decision.status == "rejected" else "",
             now_iso() if decision.status == "approved" else None,
             "auto" if decision.status == "approved" else ""))
        if info.thumbnail_png:
            (thumb_dir / f"{job_id}.png").write_bytes(info.thumbnail_png)
            db.update("jobs", job_id, has_thumbnail=1)
        db.log("submitted", decision.reason, job_id)
        if decision.status == "rejected":
            dest.unlink(missing_ok=True)
        sched.poke()
        return public_job(get_job(job_id))

    @app.get("/api/jobs")
    async def my_jobs(name: str):
        rows = db.q("SELECT * FROM jobs WHERE lower(student_name)=lower(?) ORDER BY id DESC LIMIT 30",
                    (" ".join(name.split()),))
        starts = sched.estimate_starts()
        return [public_job(j, starts) for j in rows]

    @app.get("/api/jobs/{job_id}/thumbnail")
    async def thumbnail(job_id: int):
        path = thumb_dir / f"{job_id}.png"
        if not path.exists():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/png")

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel(job_id: int, request: Request):
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        job = get_job(job_id)
        s = session_of(request)
        is_owner = body.get("name", "").strip().lower() == job["student_name"].lower()
        if not (s or is_owner):
            raise HTTPException(403, "Only the student who sent it or a teacher can cancel this.")
        if job["status"] == "printing":
            if not s:
                raise HTTPException(403, "Ask a teacher to cancel a print that has started.")
            await _stop_printer(job)
        elif job["status"] not in ("pending_approval", "approved"):
            raise HTTPException(400, f"Job is already {job['status']}.")
        db.update("jobs", job_id, status="cancelled", finished_at=now_iso())
        db.log("cancelled", s["name"] if s else "student", job_id)
        sched.poke()
        return {"ok": True}

    async def _stop_printer(job: dict) -> None:
        if not job["printer_id"]:
            return
        try:
            await sched.printer_command(job["printer_id"], "stop")
        except Exception as e:
            log.warning("stop failed: %s", e)
        db.update("printers", job["printer_id"], state="needs_clear", current_job_id=None, last_job_id=job["id"])

    @app.post("/api/jobs/{job_id}/report-failure")
    async def report_failure(job_id: int, request: Request):
        body = await request.json()
        reason = str(body.get("reason") or "Other")[:120]
        who = str(body.get("by") or "").strip()[:60] or "someone in the lab"
        job = get_job(job_id)
        if job["status"] not in ("printing", "done"):
            raise HTTPException(400, f"Job is {job['status']}, it can't be reported as failed.")
        if job["status"] == "printing":
            await _stop_printer(job)
        db.update("jobs", job_id, status="failed", fail_reason=reason, fail_reported_by=who,
                  finished_at=job["finished_at"] or now_iso())
        db.log("failure_reported", f"{reason} ({who})", job_id, job["printer_id"])
        sched.poke()
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/requeue")
    async def requeue(job_id: int, request: Request):
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        job = get_job(job_id)
        s = session_of(request)
        is_owner = body.get("name", "").strip().lower() == job["student_name"].lower()
        if not (s or is_owner):
            raise HTTPException(403, "Only the student who sent it or a teacher can re-queue this.")
        if job["status"] not in ("failed", "cancelled"):
            raise HTTPException(400, "Only failed or cancelled jobs can be re-queued.")
        if not (upload_dir / job["stored_file"]).exists():
            raise HTTPException(400, "The file is gone. Please upload it again.")
        decision = rules.decide(job["est_seconds"], datetime.now(), db.all_settings())
        pos = (db.one("SELECT MAX(position) AS m FROM jobs")["m"] or 0) + 1
        new_id = db.x(
            "INSERT INTO jobs(created_at, student_name, class_code, title, stored_file, file_kind, printer_model,"
            " target_printer_id, est_seconds, filament_g, plate, has_thumbnail, status, decision_reason, position,"
            " requeued_from, approved_at, approved_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), job["student_name"], job["class_code"], job["title"], job["stored_file"], job["file_kind"],
             job["printer_model"], job["target_printer_id"], job["est_seconds"], job["filament_g"], job["plate"],
             job["has_thumbnail"], decision.status, decision.reason, pos, job_id,
             now_iso() if decision.status == "approved" else None, "auto" if decision.status == "approved" else ""))
        if job["has_thumbnail"]:
            src = thumb_dir / f"{job_id}.png"
            if src.exists():
                (thumb_dir / f"{new_id}.png").write_bytes(src.read_bytes())
        db.log("requeued", f"from #{job_id}", new_id)
        sched.poke()
        return public_job(get_job(new_id))

    @app.post("/api/printers/{pid}/bed-cleared")
    async def bed_cleared(pid: int):
        p = get_printer(pid)
        if p["state"] != "needs_clear":
            return {"ok": True}
        db.update("printers", pid, state="ready")
        db.log("bed_cleared", "", None, pid)
        sched.poke()
        return {"ok": True}

    @app.post("/api/printers/{pid}/pause")
    async def pause(pid: int):
        get_printer(pid)
        try:
            await sched.printer_command(pid, "pause")
        except Exception as e:
            raise HTTPException(502, f"Printer didn't respond: {e}")
        db.log("paused", "", None, pid)
        return {"ok": True}

    # ------------------------------------------------------------ login
    @app.get("/api/me")
    async def me(request: Request):
        s = session_of(request)
        return {"role": s["role"], "name": s["name"]} if s else {"role": None,
                                                                  "admin_set": bool(db.setting("admin_password_hash"))}

    @app.post("/api/login")
    async def login(request: Request):
        body = await request.json()
        who = request.client.host if request.client else "?"
        if sessions.locked_out(who):
            raise HTTPException(429, "Too many wrong tries. Wait 5 minutes.")
        mode = body.get("mode", "web")
        if body.get("admin_password") is not None:
            stored = db.setting("admin_password_hash")
            if not stored or not check_secret(body["admin_password"], stored):
                sessions.record_fail(who)
                raise HTTPException(401, "Wrong password.")
            role, name = "admin", "Admin"
        else:
            pin = str(body.get("pin", ""))
            match = next((t for t in db.q("SELECT * FROM teachers") if check_secret(pin, t["pin_hash"])), None)
            if not match:
                sessions.record_fail(who)
                raise HTTPException(401, "Wrong PIN.")
            role, name = "teacher", match["name"]
        sessions.clear_fails(who)
        ttl = PANEL_SESSION_S if mode == "panel" else WEB_SESSION_S
        token = sessions.create(role, name, ttl)
        resp = JSONResponse({"role": role, "name": name, "token": token, "ttl": ttl})
        if mode != "panel":
            resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=ttl)
        return resp

    @app.post("/api/logout")
    async def logout(request: Request):
        sessions.drop(request.headers.get("X-Panel-Token") or request.cookies.get(COOKIE))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE)
        return resp

    @app.post("/api/setup")
    async def setup(request: Request):
        """First run only: choose the admin password."""
        if db.setting("admin_password_hash"):
            raise HTTPException(400, "Already set up.")
        body = await request.json()
        pw = str(body.get("password", ""))
        if len(pw) < 8:
            raise HTTPException(400, "Use at least 8 characters.")
        db.set_setting("admin_password_hash", hash_secret(pw))
        return {"ok": True}

    # ------------------------------------------------------------ teacher API
    @app.post("/api/jobs/{job_id}/approve")
    async def approve(job_id: int, s: dict = Depends(teacher)):
        job = get_job(job_id)
        if job["status"] != "pending_approval":
            raise HTTPException(400, f"Job is already {job['status']}.")
        db.update("jobs", job_id, status="approved", approved_by=s["name"], approved_at=now_iso())
        db.log("approved", s["name"], job_id)
        sched.poke()
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/reject")
    async def reject(job_id: int, request: Request, s: dict = Depends(teacher)):
        body = await request.json()
        job = get_job(job_id)
        if job["status"] not in ("pending_approval", "approved"):
            raise HTTPException(400, f"Job is already {job['status']}.")
        reason = str(body.get("reason") or "Rejected by teacher")[:200]
        db.update("jobs", job_id, status="rejected", reject_reason=reason, approved_by=s["name"],
                  finished_at=now_iso())
        db.log("rejected", f"{reason} ({s['name']})", job_id)
        return {"ok": True}

    @app.post("/api/queue/reorder")
    async def reorder(request: Request, s: dict = Depends(teacher)):
        body = await request.json()
        ids = [int(i) for i in body.get("ids", [])]
        for n, jid in enumerate(ids, start=1):
            db.x("UPDATE jobs SET position=? WHERE id=? AND status IN ('approved','pending_approval')", (n, jid))
        db.log("reordered", s["name"])
        return {"ok": True}

    @app.post("/api/printers/{pid}/{command}")
    async def printer_cmd(pid: int, command: str, s: dict = Depends(teacher)):
        if command not in ("resume", "stop"):
            raise HTTPException(404)
        p = get_printer(pid)
        if command == "stop":
            job = db.one("SELECT * FROM jobs WHERE id=?", (p["current_job_id"],)) if p["current_job_id"] else None
            if job:
                await _stop_printer(job)
                db.update("jobs", job["id"], status="cancelled", finished_at=now_iso())
            else:
                try:
                    await sched.printer_command(pid, "stop")
                except Exception as e:
                    raise HTTPException(502, f"Printer didn't respond: {e}")
        else:
            try:
                await sched.printer_command(pid, "resume")
            except Exception as e:
                raise HTTPException(502, f"Printer didn't respond: {e}")
        db.log(command, s["name"], p["current_job_id"], pid)
        sched.poke()
        return {"ok": True}

    @app.get("/api/history")
    async def history(s: dict = Depends(teacher), limit: int = 200):
        return [public_job(j) | {"student_name": j["student_name"], "class_code": j["class_code"],
                                 "approved_by": j["approved_by"], "fail_reported_by": j["fail_reported_by"]}
                for j in db.q("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (min(limit, 1000),))]

    # -------------------------------------------------------------- admin API
    def printer_admin_view(p: dict) -> dict:
        out = {k: p[k] for k in ("id", *PRINTER_FIELDS, "state", "live_state", "last_error", "last_seen")}
        for f in SECRET_FIELDS:
            out[f] = "********" if p[f] else ""
        return out

    def clean_printer(body: dict, existing: dict | None = None) -> dict:
        data = {}
        for f in PRINTER_FIELDS:
            if f not in body:
                continue
            v = body[f]
            if f in SECRET_FIELDS and v == "********":
                continue  # unchanged
            if f in ("use_ams", "enabled", "maintenance", "sort"):
                v = int(v or 0)
            else:
                v = str(v or "").strip()
            data[f] = v
        kind = data.get("kind", existing["kind"] if existing else "")
        if kind not in FILE_KINDS:
            raise HTTPException(400, "Kind must be prusalink, bambu or mock.")
        if not (data.get("name") or (existing and existing["name"])):
            raise HTTPException(400, "Give the printer a name.")
        return data

    @app.get("/api/admin/printers")
    async def admin_printers(s: dict = Depends(admin)):
        return [printer_admin_view(p) for p in db.q("SELECT * FROM printers ORDER BY sort, id")]

    @app.post("/api/admin/printers")
    async def admin_add_printer(request: Request, s: dict = Depends(admin)):
        data = clean_printer(await request.json())
        cols = ", ".join(data)
        pid = db.x(f"INSERT INTO printers({cols}) VALUES({', '.join('?' * len(data))})", tuple(data.values()))
        db.log("printer_added", data.get("name", ""), None, pid)
        sched.poke()
        return printer_admin_view(get_printer(pid))

    @app.put("/api/admin/printers/{pid}")
    async def admin_edit_printer(pid: int, request: Request, s: dict = Depends(admin)):
        existing = get_printer(pid)
        data = clean_printer(await request.json(), existing)
        db.update("printers", pid, **data)
        sched.poke()
        return printer_admin_view(get_printer(pid))

    @app.delete("/api/admin/printers/{pid}")
    async def admin_delete_printer(pid: int, s: dict = Depends(admin)):
        p = get_printer(pid)
        if p["current_job_id"]:
            raise HTTPException(400, "This printer is running a job. Stop it first.")
        await sched.forget(pid)
        db.x("DELETE FROM printers WHERE id=?", (pid,))
        return {"ok": True}

    @app.get("/api/admin/settings")
    async def admin_settings(s: dict = Depends(admin)):
        out = db.all_settings()
        out.pop("admin_password_hash", None)
        return out

    @app.put("/api/admin/settings")
    async def admin_save_settings(request: Request, s: dict = Depends(admin)):
        body = await request.json()
        allowed = ("lab_name", "short_print_minutes", "auto_approve_after", "max_print_hours",
                   "max_jobs_per_student_per_day", "class_codes", "max_upload_mb")
        for k in allowed:
            if k in body:
                v = str(body[k]).strip()
                if k == "auto_approve_after":
                    try:
                        rules.parse_hhmm(v)
                    except ValueError:
                        raise HTTPException(400, "Time must look like 13:30.")
                elif k in ("short_print_minutes", "max_jobs_per_student_per_day", "max_upload_mb"):
                    if not v.isdigit():
                        raise HTTPException(400, f"{k} must be a whole number.")
                elif k == "max_print_hours":
                    try:
                        float(v)
                    except ValueError:
                        raise HTTPException(400, "Max print hours must be a number.")
                db.set_setting(k, v)
        if body.get("new_admin_password"):
            if len(body["new_admin_password"]) < 8:
                raise HTTPException(400, "Use at least 8 characters.")
            db.set_setting("admin_password_hash", hash_secret(body["new_admin_password"]))
        return await admin_settings(s)

    @app.get("/api/admin/teachers")
    async def admin_teachers(s: dict = Depends(admin)):
        return db.q("SELECT id, name FROM teachers ORDER BY name")

    @app.post("/api/admin/teachers")
    async def admin_add_teacher(request: Request, s: dict = Depends(admin)):
        body = await request.json()
        name = str(body.get("name", "")).strip()
        pin = str(body.get("pin", "")).strip()
        if not name or not pin.isdigit() or not 4 <= len(pin) <= 8:
            raise HTTPException(400, "Enter a name and a 4-8 digit PIN.")
        if any(check_secret(pin, t["pin_hash"]) for t in db.q("SELECT pin_hash FROM teachers")):
            raise HTTPException(400, "Another teacher already uses that PIN.")
        tid = db.x("INSERT INTO teachers(name, pin_hash) VALUES(?, ?)", (name, hash_secret(pin)))
        return {"id": tid, "name": name}

    @app.delete("/api/admin/teachers/{tid}")
    async def admin_delete_teacher(tid: int, s: dict = Depends(admin)):
        db.x("DELETE FROM teachers WHERE id=?", (tid,))
        return {"ok": True}

    @app.get("/api/admin/export.csv")
    async def export_csv(s: dict = Depends(admin)):
        buf = io.StringIO()
        cols = ["id", "created_at", "student_name", "class_code", "title", "printer_model", "est_seconds",
                "filament_g", "status", "decision_reason", "approved_by", "reject_reason", "printer_id",
                "started_at", "finished_at", "fail_reason", "fail_reported_by"]
        w = csv.writer(buf)
        w.writerow(cols)
        for j in db.q("SELECT * FROM jobs ORDER BY id"):
            w.writerow([j[c] for c in cols])
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=print-history.csv"})

    return app
