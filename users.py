"""
App users for Mist MPSK Mobile, kept in users.json (git-ignored, mode 600).

Roles:
  admin       every org config, every site, org-level keys, manages all users
  site_admin  one org config + one site: full key management there, adds guests for it
  guest       one org config + one site or all sites + one SSID: only creates
              and sees keys with the Mist PSK role "guest"
  self_service one org config + one site + one SSID: a kiosk screen where
              visitors enter name/company/email and get a key (PSK role
              "self_service"); signing out needs the password again
"""

import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

ROLES = {"admin": "Admin", "site_admin": "Site admin", "guest": "Guest", "self_service": "Self service"}
GUEST_PSK_ROLE = "guest"
SELF_SERVICE_PSK_ROLE = "self_service"
KEY_EXPIRY = {"1d": "1 day", "7d": "7 days", "30d": "30 days", "90d": "90 days", "": "Never"}
MIN_PASSWORD = 8
DEFAULT_ADMIN = "admin"


class UserError(Exception):
    pass


def valid_username(name: str) -> bool:
    return 1 <= len(name) <= 40 and all(c.isalnum() or c in "._-@" for c in name)


def temp_password() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(10))


class UserStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._users = {}
        self._mtime = None

    # -- persistence -----------------------------------------------------

    def _load(self):
        try:
            mtime = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            self._users, self._mtime = {}, None
            return
        if mtime == self._mtime:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise UserError(f"{self.path.name} could not be read: {e}")
        self._users = data.get("users", {})
        self._mtime = mtime

    def _save(self):
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".users-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"users": self._users}, f, indent=2, sort_keys=True)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self._mtime = self.path.stat().st_mtime_ns

    def exists(self) -> bool:
        return self.path.exists()

    def bootstrap(self, legacy_hashes: dict | None = None) -> list[str]:
        """First start: creates users.json with admin/admin (must change at
        first sign-in), plus any users from the old web.conf as admins.
        Returns what was created, for the log."""
        with self._lock:
            self._load()
            if self.exists():
                return []
            created = []
            for name, pw_hash in (legacy_hashes or {}).items():
                self._users[name] = self._record("admin", None, None, None, created_by="web.conf")
                self._users[name]["password_hash"] = pw_hash
                created.append(f"{name} (admin, from web.conf)")
            if not any(n.lower() == DEFAULT_ADMIN for n in self._users):
                rec = self._record("admin", None, None, None, created_by="first start")
                rec["password_hash"] = generate_password_hash(DEFAULT_ADMIN)
                rec["must_change"] = True
                self._users[DEFAULT_ADMIN] = rec
                created.append(f"{DEFAULT_ADMIN} (admin, password 'admin', must be changed at first sign-in)")
            self._save()
            return created

    @staticmethod
    def _record(role, config, site_id, site_name, created_by, ssid=None, key_expiry=None):
        return {"role": role, "config": config, "site_id": site_id, "site_name": site_name,
                "ssid": ssid, "key_expiry": key_expiry,
                "must_change": False, "created_by": created_by, "created": int(time.time())}

    # -- reads -----------------------------------------------------------

    def get(self, name: str) -> dict | None:
        with self._lock:
            self._load()
            for n, rec in self._users.items():
                if n.lower() == (name or "").lower():
                    return dict(rec, name=n)
            return None

    def all(self) -> list[dict]:
        with self._lock:
            self._load()
            return [dict(rec, name=n) for n, rec in sorted(self._users.items(), key=lambda kv: kv[0].lower())]

    def check_password(self, name: str, password: str, dummy_hash: str) -> dict | None:
        user = self.get(name)
        ok = check_password_hash(user["password_hash"] if user else dummy_hash, password)
        return user if ok and user else None

    # -- writes ----------------------------------------------------------

    def add(self, name, role, config, site_id, site_name, password, created_by,
            ssid=None, key_expiry=None) -> dict:
        name = (name or "").strip()
        if not valid_username(name):
            raise UserError("User names are 1-40 letters, numbers or . _ - @ (no spaces).")
        if role not in ROLES:
            raise UserError("Pick a role.")
        if role != "admin" and not config:
            raise UserError("Pick the org for this user.")
        if role in ("site_admin", "self_service") and (not site_id or site_id == "*"):
            raise UserError(f"A {ROLES[role].lower()} user needs one site.")
        if role in ("guest", "self_service") and not ssid:
            raise UserError("Pick the SSID this user's keys are for.")
        if role == "self_service" and (key_expiry or "") not in KEY_EXPIRY:
            raise UserError("Pick how long visitors' keys last.")
        if len(password) < MIN_PASSWORD:
            raise UserError(f"Passwords need at least {MIN_PASSWORD} characters.")
        with self._lock:
            self._load()
            if any(n.lower() == name.lower() for n in self._users):
                raise UserError(f"There is already a user called {name}.")
            scoped = role != "admin"
            rec = self._record(role, config if scoped else None, site_id if scoped else None,
                               site_name if scoped else None, created_by,
                               ssid=ssid if role in ("guest", "self_service") else None,
                               key_expiry=(key_expiry or "") if role == "self_service" else None)
            rec["password_hash"] = generate_password_hash(password)
            # A kiosk is signed in by staff with the password they were given, so no forced change.
            rec["must_change"] = role != "self_service"
            self._users[name] = rec
            self._save()
            return dict(rec, name=name)

    def set_password(self, name, password, must_change=False):
        if len(password) < MIN_PASSWORD:
            raise UserError(f"Passwords need at least {MIN_PASSWORD} characters.")
        with self._lock:
            self._load()
            user = self.get(name)
            if not user:
                raise UserError("No such user.")
            rec = self._users[user["name"]]
            rec["password_hash"] = generate_password_hash(password)
            rec["must_change"] = must_change
            self._save()

    def delete(self, name):
        with self._lock:
            self._load()
            user = self.get(name)
            if not user:
                raise UserError("No such user.")
            if user["role"] == "admin" and sum(1 for u in self._users.values() if u["role"] == "admin") <= 1:
                raise UserError("You can't delete the last admin.")
            del self._users[user["name"]]
            self._save()

    def reset_admin(self) -> str:
        """CLI recovery: admin/admin, must change at next sign-in."""
        with self._lock:
            self._load()
            existing = self.get(DEFAULT_ADMIN)
            name = existing["name"] if existing else DEFAULT_ADMIN
            rec = self._users.get(name) or self._record("admin", None, None, None, created_by="--reset-admin")
            rec.update(role="admin", config=None, site_id=None, site_name=None,
                       password_hash=generate_password_hash(DEFAULT_ADMIN), must_change=True)
            self._users[name] = rec
            self._save()
            return name
