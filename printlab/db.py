"""SQLite storage. One file, safe to copy for backups."""
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS teachers (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL,
    pin_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS printers (
    id               INTEGER PRIMARY KEY,
    name             TEXT NOT NULL,
    kind             TEXT NOT NULL,            -- prusalink | bambu | mock
    models           TEXT NOT NULL DEFAULT '', -- comma list of slicer printer_model values it accepts
    host             TEXT NOT NULL DEFAULT '',
    api_key          TEXT NOT NULL DEFAULT '',
    username         TEXT NOT NULL DEFAULT '',
    password         TEXT NOT NULL DEFAULT '',
    serial           TEXT NOT NULL DEFAULT '',
    access_code      TEXT NOT NULL DEFAULT '',
    storage          TEXT NOT NULL DEFAULT 'usb',
    use_ams          INTEGER NOT NULL DEFAULT 1,
    enabled          INTEGER NOT NULL DEFAULT 1,
    maintenance      INTEGER NOT NULL DEFAULT 0,
    maintenance_note TEXT NOT NULL DEFAULT '',
    state            TEXT NOT NULL DEFAULT 'needs_clear', -- needs_clear | ready | printing
    current_job_id   INTEGER,
    last_job_id      INTEGER,
    live_state       TEXT NOT NULL DEFAULT 'offline',
    live_progress    REAL,
    live_remaining   INTEGER,
    live_message     TEXT NOT NULL DEFAULT '',
    last_seen        TEXT,
    last_error       TEXT NOT NULL DEFAULT '',
    sort             INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS jobs (
    id                INTEGER PRIMARY KEY,
    created_at        TEXT NOT NULL,
    student_name      TEXT NOT NULL,
    class_code        TEXT NOT NULL DEFAULT '',
    title             TEXT NOT NULL,
    stored_file       TEXT NOT NULL,
    file_kind         TEXT NOT NULL,           -- bgcode | gcode | 3mf
    printer_model     TEXT NOT NULL DEFAULT '',
    target_printer_id INTEGER,
    est_seconds       INTEGER NOT NULL,
    filament_g        REAL,
    has_thumbnail     INTEGER NOT NULL DEFAULT 0,
    plate             INTEGER,
    status            TEXT NOT NULL,           -- pending_approval | approved | printing | done | failed | rejected | cancelled
    decision_reason   TEXT NOT NULL DEFAULT '',
    position          REAL NOT NULL DEFAULT 0,
    approved_by       TEXT NOT NULL DEFAULT '',
    approved_at       TEXT,
    reject_reason     TEXT NOT NULL DEFAULT '',
    printer_id        INTEGER,
    started_at        TEXT,
    seen_printing     INTEGER NOT NULL DEFAULT 0,
    progress          REAL,
    remaining_s       INTEGER,
    finished_at       TEXT,
    fail_reason       TEXT NOT NULL DEFAULT '',
    fail_reported_by  TEXT NOT NULL DEFAULT '',
    requeued_from     INTEGER
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, position);
CREATE INDEX IF NOT EXISTS jobs_student ON jobs(student_name);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    ts         TEXT NOT NULL,
    job_id     INTEGER,
    printer_id INTEGER,
    type       TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT ''
);
"""

DEFAULT_SETTINGS = {
    "lab_name": "Print Lab",
    "short_print_minutes": "30",       # prints this long or shorter are auto-approved
    "auto_approve_after": "13:30",     # anything submitted at/after this time of day is auto-approved
    "max_print_hours": "8",            # longer prints are rejected outright
    "max_jobs_per_student_per_day": "3",  # 0 = no limit
    "class_codes": "",                 # comma list; empty = any code accepted
    "max_upload_mb": "200",
}


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)

    def q(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args=()) -> dict | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args=()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, args)
            return cur.lastrowid

    def update(self, table: str, row_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE {table} SET {cols} WHERE id=?", (*fields.values(), row_id))

    # settings -----------------------------------------------------------
    def setting(self, key: str) -> str:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        if row:
            return row["value"]
        return DEFAULT_SETTINGS.get(key, "")

    def set_setting(self, key: str, value: str) -> None:
        self.x("INSERT INTO settings(key, value) VALUES(?, ?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def all_settings(self) -> dict:
        out = dict(DEFAULT_SETTINGS)
        for r in self.q("SELECT key, value FROM settings"):
            out[r["key"]] = r["value"]
        return out

    def log(self, type_: str, detail: str = "", job_id=None, printer_id=None) -> None:
        self.x("INSERT INTO events(ts, job_id, printer_id, type, detail) VALUES(?,?,?,?,?)",
               (now_iso(), job_id, printer_id, type_, detail))

    def backup_to(self, dest: Path) -> None:
        with self.lock:
            target = sqlite3.connect(str(dest))
            try:
                self.conn.backup(target)
            finally:
                target.close()
