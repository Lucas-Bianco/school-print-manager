import asyncio
import base64
import io
import struct
import zipfile
import zlib
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from printlab import rules
from printlab.fileinfo import FileError, parse_bgcode, parse_gcode_text, read_info
from printlab.main import create_app

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
SETTINGS = {"short_print_minutes": "30", "auto_approve_after": "13:30", "max_print_hours": "8"}


# ------------------------------------------------------------------ rules
@pytest.mark.parametrize("minutes, clock, status", [
    (25, "10:00", "approved"),          # short print, morning
    (30, "10:00", "approved"),          # exactly 30 min counts as short
    (120, "10:00", "pending_approval"),  # long print, morning -> teacher
    (120, "13:29", "pending_approval"),
    (120, "13:30", "approved"),         # long print after 1:30 PM
    (120, "15:00", "approved"),
    (9 * 60, "15:00", "rejected"),      # over the 8 h limit
])
def test_rules(minutes, clock, status):
    h, m = map(int, clock.split(":"))
    when = datetime(2026, 11, 23, h, m)
    assert rules.decide(minutes * 60, when, SETTINGS).status == status


# ------------------------------------------------------------- file makers
def make_gcode(est="1h 5m 30s", model="MK4S", grams="12.34") -> str:
    b64 = base64.b64encode(PNG).decode()
    return (f"; thumbnail begin 16x16 {len(b64)}\n; {b64}\n; thumbnail end\n"
            "G28\nG1 X10 Y10\n"
            f"; filament used [g] = {grams}\n"
            f"; estimated printing time (normal mode) = {est}\n"
            f"; printer_model = {model}\n")


def make_bgcode(est="2h 3m", model="COREONE", grams="40.5", compress=True) -> bytes:
    out = bytearray(b"GCDE") + struct.pack("<IH", 1, 1)

    def block(btype, payload: bytes, params: bytes, comp=0):
        data = zlib.compress(payload) if comp == 1 else payload
        head = struct.pack("<HHI", btype, comp, len(payload))
        if comp:
            head += struct.pack("<I", len(data))
        body = head + params + data
        return body + struct.pack("<I", zlib.crc32(body))

    out += block(0, b"Producer=PrusaSlicer 2.9.0\n", struct.pack("<H", 0))
    out += block(3, f"printer_model={model}\nestimated printing time (normal mode)={est}\n"
                    f"filament used [g]={grams}\n".encode(), struct.pack("<H", 0), 1 if compress else 0)
    out += block(5, PNG, struct.pack("<HHH", 0, 16, 16))
    out += block(1, b"G28\n", struct.pack("<H", 0))
    return bytes(out)


def make_3mf(seconds=5400, grams=22.1, model="BL-P001", sliced=True) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("3D/3dmodel.model", "<model/>")
        if sliced:
            z.writestr("Metadata/plate_1.gcode", "; HEADER_BLOCK_START\nG28\n")
            z.writestr("Metadata/plate_1.png", PNG)
            z.writestr("Metadata/slice_info.config",
                       f'<config><plate><metadata key="index" value="1"/>'
                       f'<metadata key="printer_model_id" value="{model}"/>'
                       f'<metadata key="prediction" value="{seconds}"/>'
                       f'<metadata key="weight" value="{grams}"/></plate></config>')
    return buf.getvalue()


# --------------------------------------------------------------- file info
def test_gcode_text():
    info = parse_gcode_text(make_gcode())
    assert info.est_seconds == 3600 + 5 * 60 + 30
    assert info.printer_model == "MK4S"
    assert info.filament_g == 12.3
    assert info.thumbnail_png == PNG


def test_gcode_without_estimate():
    with pytest.raises(FileError):
        parse_gcode_text("G28\nG1 X1\n")


@pytest.mark.parametrize("compress", [True, False])
def test_bgcode(compress):
    info = parse_bgcode(make_bgcode(compress=compress))
    assert info.est_seconds == 2 * 3600 + 3 * 60
    assert info.printer_model == "COREONE"
    assert info.filament_g == 40.5
    assert info.thumbnail_png == PNG


def test_3mf(tmp_path):
    p = tmp_path / "part.gcode.3mf"
    p.write_bytes(make_3mf())
    info = read_info(p, p.name)
    assert (info.kind, info.est_seconds, info.printer_model, info.filament_g, info.plate) == ("3mf", 5400, "BL-P001", 22.1, 1)


def test_unsliced_3mf(tmp_path):
    p = tmp_path / "project.3mf"
    p.write_bytes(make_3mf(sliced=False))
    with pytest.raises(FileError, match="isn't sliced"):
        read_info(p, p.name)


# ---------------------------------------------------------------- full flow
@pytest.fixture
def lab(tmp_path):
    app = create_app(tmp_path, run_scheduler=False)
    with TestClient(app) as client:
        client.post("/api/setup", json={"password": "adminpass1"})
        client.post("/api/login", json={"admin_password": "adminpass1"})
        # A pretend CORE One that runs prints 3600x faster than real time.
        r = client.post("/api/admin/printers", json={"name": "Core One", "kind": "mock", "models": "COREONE", "host": "3600"})
        assert r.status_code == 200, r.text
        client.post("/api/admin/teachers", json={"name": "Ms Rivera", "pin": "4321"})
        client.post("/api/logout")
        client.cookies.clear()
        yield app, client


def tick(app):
    asyncio.run(app.state.sched.tick())


def upload(client, data: bytes, filename: str, name="Ada Lovelace"):
    return client.post("/api/jobs", data={"name": name, "class_code": "ENG1"},
                       files={"file": (filename, data, "application/octet-stream")})


def test_full_flow(lab, monkeypatch):
    app, client = lab
    sched = app.state.sched

    # Long print in the morning -> waits for a teacher.
    monkeypatch.setattr("printlab.main.datetime", _FixedDT(datetime(2026, 11, 23, 10, 0)))
    r = upload(client, make_bgcode(est="2h"), "bracket.bgcode")
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["status"] == "pending_approval"
    assert job["has_thumbnail"]

    # Students can't approve.
    assert client.post(f"/api/jobs/{job['id']}/approve").status_code == 401

    # Wrong-printer file is refused with a clear message.
    r = upload(client, make_gcode(model="MK4S"), "hook.gcode")
    assert r.status_code == 400 and "MK4S" in r.json()["detail"]

    # Teacher approves from the touch panel (PIN, short session token).
    t = client.post("/api/login", json={"pin": "4321", "mode": "panel"}).json()
    assert t["role"] == "teacher"
    hdr = {"X-Panel-Token": t["token"]}
    assert client.post(f"/api/jobs/{job['id']}/approve", headers=hdr).status_code == 200

    # Printer starts as "needs_clear": nothing starts until someone confirms the bed is empty.
    tick(app)
    assert _job(client, job["id"])["status"] == "approved"
    client.post("/api/printers/1/bed-cleared")
    tick(app)
    assert _job(client, job["id"])["status"] == "printing"

    tick(app)  # scheduler sees it printing
    # Jump the pretend printer to the end of the print.
    ad = sched.adapters[1][1]
    ad.elapsed = ad.duration
    tick(app)  # sees "finished"
    j = _job(client, job["id"])
    assert j["status"] == "done"
    st = client.get("/api/state").json()
    assert st["printers"][0]["state"] == "needs_clear"

    # Someone reports the finished print as failed, then re-queues it.
    r = client.post(f"/api/jobs/{job['id']}/report-failure", json={"reason": "Spaghetti / messy", "by": "Sam"})
    assert r.status_code == 200
    assert _job(client, job["id"])["status"] == "failed"
    r = client.post(f"/api/jobs/{job['id']}/requeue", json={"name": "Ada Lovelace"})
    assert r.status_code == 200 and r.json()["status"] == "pending_approval"


def test_short_print_auto_and_failure_stops_printer(lab, monkeypatch):
    app, client = lab
    monkeypatch.setattr("printlab.main.datetime", _FixedDT(datetime(2026, 11, 23, 9, 0)))
    job = upload(client, make_bgcode(est="20m"), "keychain.bgcode").json()
    assert job["status"] == "approved"

    client.post("/api/printers/1/bed-cleared")
    tick(app)
    assert _job(client, job["id"])["status"] == "printing"
    tick(app)

    r = client.post(f"/api/jobs/{job['id']}/report-failure", json={"reason": "Not sticking to bed"})
    assert r.status_code == 200
    assert app.state.sched.adapters[1][1].state == "stopped"
    st = client.get("/api/state").json()
    assert st["printers"][0]["state"] == "needs_clear"
    assert st["failed_today"][0]["fail_reason"] == "Not sticking to bed"


def test_after_cutoff_long_print_auto(lab, monkeypatch):
    app, client = lab
    monkeypatch.setattr("printlab.main.datetime", _FixedDT(datetime(2026, 11, 23, 14, 0)))
    job = upload(client, make_bgcode(est="3h"), "vase.bgcode").json()
    assert job["status"] == "approved"
    assert "after" in job["decision_reason"]


def test_wrong_pin_lockout(lab):
    app, client = lab
    for _ in range(5):
        assert client.post("/api/login", json={"pin": "0000"}).status_code == 401
    assert client.post("/api/login", json={"pin": "4321"}).status_code == 429


# helpers --------------------------------------------------------------------
def _job(client, job_id):
    return next(j for j in client.get("/api/jobs", params={"name": "Ada Lovelace"}).json() if j["id"] == job_id)


class _FixedDT(datetime):
    """Stand-in for datetime in printlab.main so 'now' is fixed for the rules check."""

    def __new__(cls, fixed):
        obj = datetime.__new__(cls, fixed.year, fixed.month, fixed.day, fixed.hour, fixed.minute)
        obj._fixed = fixed
        return obj

    def now(self, tz=None):
        return self._fixed

    def __call__(self, *a, **kw):
        return datetime(*a, **kw)


# ------------------------------------------------- PrusaLink request shapes
def test_prusalink_adapter(tmp_path):
    import httpx
    from printlab.printers.prusalink import PrusaLinkAdapter

    seen = []

    def handler(req: httpx.Request):
        seen.append((req.method, req.url.path, dict(req.headers)))
        if req.url.path == "/api/v1/status":
            return httpx.Response(200, json={"printer": {"state": "PRINTING"},
                                             "job": {"id": 7, "progress": 42.0, "time_remaining": 600}})
        return httpx.Response(204 if req.method != "PUT" or "files" not in req.url.path else 201)

    ad = PrusaLinkAdapter({"host": "192.168.1.50", "api_key": "abc123", "storage": "usb"})
    ad.client = httpx.AsyncClient(base_url=ad.base, headers={"X-Api-Key": "abc123"},
                                  transport=httpx.MockTransport(handler))
    f = tmp_path / "x.bgcode"
    f.write_bytes(b"GCDE")

    async def go():
        st = await ad.get_status()
        assert (st.state, st.progress, st.remaining_s) == ("printing", 42.0, 600)
        await ad.start_job(f, "job5.bgcode", {})
        await ad.pause()
        await ad.stop()
    asyncio.run(go())

    upload = next(s for s in seen if s[0] == "PUT" and "files" in s[1])
    assert upload[1] == "/api/v1/files/usb/job5.bgcode"
    assert upload[2]["print-after-upload"] == "?1" and upload[2]["x-api-key"] == "abc123"
    assert ("PUT", "/api/v1/job/7/pause") in [(m, p) for m, p, _ in seen]
    assert ("DELETE", "/api/v1/job/7") in [(m, p) for m, p, _ in seen]
