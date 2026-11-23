"""Bambu Lab printers (X1C, P1S, ...) over the local network.

Uses the printer's LAN interface: MQTT (TLS, port 8883) for status and commands,
and FTPS (implicit TLS, port 990) to upload the sliced .gcode.3mf.

Printer setup (on the printer's screen):
  - Settings > Network: turn on "LAN Only Mode" and note the Access Code.
  - On newer firmware, also turn on "Developer Mode" (otherwise third-party
    software can read status but cannot start prints).
  - Settings > Device: note the serial number.

The printer uses a self-signed certificate, so certificate checks are turned
off for these two connections only. Keep printers on the school LAN.
"""
import asyncio
import ftplib
import json
import ssl
import threading
import time
from pathlib import Path

from .base import (ATTENTION, BUSY, FINISHED, IDLE, OFFLINE, PAUSED, PRINTING,
                   STOPPED, Adapter, PrinterError, Status)

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover
    mqtt = None

STATE_MAP = {
    "IDLE": IDLE,
    "RUNNING": PRINTING,
    "PREPARE": PRINTING,
    "SLICING": BUSY,
    "PAUSE": PAUSED,
    "FINISH": FINISHED,
    "FAILED": STOPPED,
}


def _insecure_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class ImplicitFTPS(ftplib.FTP_TLS):
    """FTP over implicit TLS with session reuse on the data channel (Bambu needs both)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self.host)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size

    def storbinary(self, cmd, fp, blocksize=65536, callback=None, rest=None):
        # Same as the stdlib version but without conn.unwrap(), which hangs on Bambu printers.
        self.voidcmd("TYPE I")
        with self.transfercmd(cmd, rest) as conn:
            while buf := fp.read(blocksize):
                conn.sendall(buf)
        return self.voidresp()


class BambuAdapter(Adapter):
    file_kinds = {"3mf"}

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        if mqtt is None:
            raise PrinterError("paho-mqtt is not installed")
        self.host = cfg["host"].strip()
        self.serial = cfg["serial"].strip()
        self.code = cfg["access_code"].strip()
        self.use_ams = bool(cfg.get("use_ams", 1))
        self.report: dict = {}
        self.last_msg = 0.0
        self.connected = False
        self.lock = threading.Lock()

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"printlab-{self.serial}")
        self.client.username_pw_set("bblp", self.code)
        self.client.tls_set_context(_insecure_ctx())
        self.client.tls_insecure_set(True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(2, 30)
        self.client.connect_async(self.host, 8883, keepalive=30)
        self.client.loop_start()

    # MQTT callbacks (run on paho's thread) --------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            self.connected = False
            return
        self.connected = True
        client.subscribe(f"device/{self.serial}/report")
        self._publish({"pushing": {"sequence_id": "0", "command": "pushall"}})

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected = False

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload)
        except ValueError:
            return
        part = data.get("print")
        if isinstance(part, dict):
            with self.lock:
                self.report.update(part)   # the printer sends partial updates
                self.last_msg = time.monotonic()

    def _publish(self, payload: dict) -> None:
        info = self.client.publish(f"device/{self.serial}/request", json.dumps(payload))
        if info.rc != 0:
            raise PrinterError("not connected to printer")

    # Adapter API -----------------------------------------------------------
    async def get_status(self) -> Status:
        with self.lock:
            rep = dict(self.report)
            age = time.monotonic() - self.last_msg if self.last_msg else None
        if not self.connected or age is None or age > 90:
            if self.connected and age is not None and age > 30:
                try:
                    self._publish({"pushing": {"sequence_id": "0", "command": "pushall"}})
                except PrinterError:
                    pass
            if not self.connected or age is None or age > 90:
                return Status(OFFLINE, message="no MQTT data (check IP, access code, LAN mode)")
        state = STATE_MAP.get(str(rep.get("gcode_state", "")).upper(), BUSY)
        if state == PAUSED and rep.get("print_error"):
            state = ATTENTION
        remaining = rep.get("mc_remaining_time")
        return Status(
            state=state,
            progress=rep.get("mc_percent"),
            remaining_s=int(remaining) * 60 if remaining is not None else None,
            raw={k: rep.get(k) for k in ("gcode_state", "mc_percent", "mc_remaining_time",
                                         "print_error", "subtask_name")},
        )

    def _upload(self, path: Path, remote_name: str) -> None:
        ftp = ImplicitFTPS(context=_insecure_ctx())
        try:
            ftp.connect(self.host, 990, timeout=30)
            ftp.login("bblp", self.code)
            ftp.prot_p()
            with open(path, "rb") as fh:
                ftp.storbinary(f"STOR {remote_name}", fh)
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()

    async def start_job(self, path: Path, remote_name: str, job: dict) -> None:
        try:
            await asyncio.to_thread(self._upload, path, remote_name)
        except (OSError, ftplib.Error) as e:
            raise PrinterError(f"FTPS upload failed: {e}") from e
        plate = job.get("plate") or 1
        self._publish({"print": {
            "sequence_id": "0",
            "command": "project_file",
            "param": f"Metadata/plate_{plate}.gcode",
            "project_id": "0",
            "profile_id": "0",
            "task_id": "0",
            "subtask_id": "0",
            "subtask_name": remote_name,
            "url": f"file:///sdcard/{remote_name}",
            "md5": "",
            "timelapse": False,
            "bed_type": "auto",
            "bed_levelling": True,
            "flow_cali": False,
            "vibration_cali": False,
            "layer_inspect": False,
            "use_ams": self.use_ams,
            "ams_mapping": [],
        }})

    async def pause(self) -> None:
        self._publish({"print": {"sequence_id": "0", "command": "pause"}})

    async def resume(self) -> None:
        self._publish({"print": {"sequence_id": "0", "command": "resume"}})

    async def stop(self) -> None:
        self._publish({"print": {"sequence_id": "0", "command": "stop"}})

    async def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()
