"""Teacher PINs, the admin password, and short-lived sessions (kept in memory)."""
import hashlib
import hmac
import secrets
import time

WEB_SESSION_S = 8 * 3600
PANEL_SESSION_S = 60          # the touch panel locks again after 60 s without a teacher action
MAX_FAILS = 5
LOCKOUT_S = 300


def hash_secret(secret: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode(), salt.encode(), 120_000).hex()
    return f"{salt}${digest}"


def check_secret(secret: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    got = hashlib.pbkdf2_hmac("sha256", secret.encode(), salt.encode(), 120_000).hex()
    return hmac.compare_digest(got, digest)


class Sessions:
    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self.fails: dict[str, list[float]] = {}

    def create(self, role: str, name: str, ttl: int) -> str:
        token = secrets.token_urlsafe(24)
        self.sessions[token] = {"role": role, "name": name, "ttl": ttl, "expires": time.time() + ttl}
        return token

    def get(self, token: str | None) -> dict | None:
        if not token:
            return None
        s = self.sessions.get(token)
        if not s:
            return None
        if s["expires"] < time.time():
            self.sessions.pop(token, None)
            return None
        s["expires"] = time.time() + s["ttl"]   # sliding expiry
        return s

    def drop(self, token: str | None) -> None:
        if token:
            self.sessions.pop(token, None)

    # brute-force protection -------------------------------------------------
    def locked_out(self, who: str) -> bool:
        now = time.time()
        recent = [t for t in self.fails.get(who, []) if now - t < LOCKOUT_S]
        self.fails[who] = recent
        return len(recent) >= MAX_FAILS

    def record_fail(self, who: str) -> None:
        self.fails.setdefault(who, []).append(time.time())

    def clear_fails(self, who: str) -> None:
        self.fails.pop(who, None)
