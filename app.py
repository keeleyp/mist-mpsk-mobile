#!/usr/bin/env python3
"""
Mist MPSK Mobile - a phone-friendly web app for creating Mist site PSKs.

Runs on a Raspberry Pi (or any box). Open it on an iPhone or Android phone and
use "Add to Home Screen" to get an app icon. Sign in, pick a site, create a
PSK, then show or share its Wi-Fi QR code.

    python3 app.py                  # serve on [web] host/port from web.conf
    python3 app.py --read-only      # nobody can create or delete PSKs
    python3 app.py --hash-password  # print a password_hash line for web.conf

Configs are .ini files in this folder (same format as the Mist MPSK
Generator's). Users, their hashed passwords and the configs each may use live
in web.conf.
"""

import argparse
import configparser
import getpass
import logging
import logging.handlers
import os
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from flask import (Flask, Response, abort, flash, redirect, render_template, request, send_from_directory,
                   session, url_for)
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

import mpsk_core as core
from mpsk_core import MpskError

__version__ = "1.0.0"

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_WEB_CONF = BASE_DIR / "web.conf"
SECRET_KEY_FILE = BASE_DIR / ".secret_key"
ACTIVITY_LOG = BASE_DIR / "mpsk_activity.log"
ACTIVITY_LOG_MAX_BYTES = 5 * 1024 * 1024
ACTIVITY_LOG_BACKUPS = 10
DEFAULT_PORT = 5040

MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 15 * 60
SITE_CACHE_TTL = 5 * 60
RECENT_SITES = 5

log = logging.getLogger("mist_mpsk_mobile")


class WebConfError(Exception):
    pass


# --------------------------------------------------------------------------
# web.conf
# --------------------------------------------------------------------------

def find_config_files(folder: Path = BASE_DIR) -> dict:
    return {p.name: p for p in sorted(folder.glob("*.ini"), key=lambda p: p.name.lower())}


def load_web_conf(path: Path, config_folder: Path = BASE_DIR) -> dict:
    if not path.exists():
        raise WebConfError(f"{path.name} not found. Copy web.conf.example to {path.name} and add at least one "
                           "[user NAME] section (python3 app.py --hash-password makes the password_hash line).")
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path)
    except configparser.Error as e:
        raise WebConfError(f"{path.name} could not be parsed: {e}")

    web = parser["web"] if "web" in parser else {}
    try:
        port = int(web.get("port", DEFAULT_PORT))
        session_hours = int(web.get("session_hours", 12))
    except ValueError:
        raise WebConfError(f"{path.name}: [web] port and session_hours must be whole numbers.")
    certfile = (web.get("certfile", "") or "").strip()
    keyfile = (web.get("keyfile", "") or "").strip()
    if bool(certfile) != bool(keyfile):
        raise WebConfError(f"{path.name}: [web] needs both certfile and keyfile for HTTPS, or neither.")
    tls = None
    if certfile:
        cert, key = (Path(p).expanduser() for p in (certfile, keyfile))
        cert, key = (p if p.is_absolute() else path.parent / p for p in (cert, key))
        for p in (cert, key):
            if not p.is_file():
                raise WebConfError(f"{path.name}: [web] file not found: {p}")
        tls = (str(cert), str(key))

    available = find_config_files(config_folder)
    users, problems = {}, []
    for section in parser.sections():
        if not section.lower().startswith("user "):
            if section != "web":
                problems.append(f"unknown section [{section}] (users are [user NAME])")
            continue
        name = section[5:].strip()
        sec = parser[section]
        pw_hash = sec.get("password_hash", "").strip()
        if not name:
            problems.append(f"[{section}] has no user name")
            continue
        if not pw_hash or ":" not in pw_hash:
            problems.append(f"[{section}] password_hash is missing or not a hash "
                            "(make one with python3 app.py --hash-password)")
            continue
        raw = [c.strip() for c in sec.get("configs", "").replace("\n", ",").split(",") if c.strip()]
        if not raw:
            problems.append(f"[{section}] configs is empty - list the .ini files this user may use, or *")
            continue
        if raw != ["*"]:
            missing = [c for c in raw if c not in available]
            if missing:
                problems.append(f"[{section}] configs not found in {config_folder}: {', '.join(missing)}")
                continue
        try:
            read_only = sec.getboolean("read_only", fallback=False)
        except ValueError:
            problems.append(f"[{section}] read_only must be true or false")
            continue
        if name.lower() in {u.lower() for u in users}:
            problems.append(f"user {name} is defined twice")
            continue
        users[name] = {"name": name, "password_hash": pw_hash, "configs": raw,
                       "all_configs": raw == ["*"], "read_only": read_only}

    if problems:
        raise WebConfError(f"{path.name} has problems:\n  - " + "\n  - ".join(problems))
    if not users:
        raise WebConfError(f"{path.name} has no [user NAME] sections - add at least one login.")
    return {"host": (web.get("host", "0.0.0.0") or "0.0.0.0").strip(), "port": port,
            "session_hours": session_hours, "tls": tls, "users": users}


def load_secret_key(path: Path = SECRET_KEY_FILE) -> str:
    """Kept on disk so a restart (or a Pi reboot) doesn't sign every phone out."""
    try:
        key = path.read_text().strip()
        if len(key) >= 32:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key)
    return key


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

class LoginLimiter:
    def __init__(self):
        self._failures, self._lock = {}, threading.Lock()

    def _recent(self, key, now):
        return [t for t in self._failures.get(key, []) if now - t < LOCKOUT_SECONDS]

    def locked(self, key) -> bool:
        with self._lock:
            return len(self._recent(key, time.time())) >= MAX_FAILED_LOGINS

    def fail(self, key):
        with self._lock:
            now = time.time()
            self._failures[key] = self._recent(key, now) + [now]

    def clear(self, key):
        with self._lock:
            self._failures.pop(key, None)


class TtlCache:
    def __init__(self, ttl):
        self.ttl, self._data, self._lock = ttl, {}, threading.Lock()

    def get(self, key):
        with self._lock:
            entry = self._data.get(key)
            return entry[1] if entry and time.time() - entry[0] < self.ttl else None

    def put(self, key, value):
        with self._lock:
            self._data[key] = (time.time(), value)

    def drop(self, key):
        with self._lock:
            self._data.pop(key, None)


_activity_logger = logging.getLogger("mist_mpsk_mobile.activity")
_activity_logger.propagate = False


def setup_activity_log(path: Path):
    for handler in list(_activity_logger.handlers):
        _activity_logger.removeHandler(handler)
        handler.close()
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=ACTIVITY_LOG_MAX_BYTES, backupCount=ACTIVITY_LOG_BACKUPS, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    _activity_logger.addHandler(handler)
    _activity_logger.setLevel(logging.INFO)


def _clean(value) -> str:
    return " ".join(str(value).split())


def activity(event: str, user=None, **fields):
    """One tab-separated line per event. Passphrases are never logged."""
    if user is None:
        user = session.get("user") or "-"
    parts = [datetime.now().isoformat(timespec="seconds"), f"ip={request.remote_addr}",
             f"user={_clean(user) or '-'}", f"event={event}"]
    parts += [f"{k}={_clean(v)}" for k, v in fields.items() if v is not None and v != ""]
    line = "\t".join(parts)
    try:
        _activity_logger.info(line)
    except Exception as e:   # never let logging break a request
        log.error("Could not write activity log: %s", e)
    log.info("activity: %s", line)


def fmt_expiry(epoch) -> str:
    if not epoch:
        return "Never"
    return datetime.fromtimestamp(int(epoch)).strftime("%d %b %Y %H:%M")


def vlan_text(psk: dict) -> str:
    v = psk.get("vlan_id")
    return "" if v in (None, "", 0) else str(v)


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------

def create_app(web_conf: dict, force_read_only: bool = False, config_folder: Path = BASE_DIR,
               activity_log: Path = None, secret_key: str = None) -> Flask:
    setup_activity_log(activity_log or ACTIVITY_LOG)
    app = Flask(__name__)
    app.secret_key = secret_key or load_secret_key()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=bool(web_conf.get("tls")),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=web_conf["session_hours"]),
    )
    users = web_conf["users"]
    limiter = LoginLimiter()
    site_cache = TtlCache(SITE_CACHE_TTL)
    org_cache = TtlCache(60 * 60)
    dummy_hash = generate_password_hash(secrets.token_hex(16))

    def current_user():
        name = session.get("user")
        return users.get(name) if name else None

    def read_only(user) -> bool:
        return force_read_only or user["read_only"]

    def allowed_configs(user) -> dict:
        available = find_config_files(config_folder)
        names = list(available) if user["all_configs"] else user["configs"]
        return {n: available[n] for n in names if n in available}

    def current_api() -> core.Mist:
        user = current_user()
        configs = allowed_configs(user)
        name = session.get("config")
        if name not in configs:
            if len(configs) == 1:
                name = session["config"] = next(iter(configs))
            else:
                abort(redirect(url_for("configs")))
        return core.Mist(core.load_config(configs[name]))

    def org_name(api: core.Mist) -> str:
        key = (api.cfg.name, api.cfg.org_id)
        name = org_cache.get(key)
        if name is None:
            name = api.cfg.label or api.org().get("name") or api.cfg.org_id
            org_cache.put(key, name)
        return name

    def site_list(api: core.Mist, refresh=False) -> list:
        key = (api.cfg.name, api.cfg.org_id)
        sites = None if refresh else site_cache.get(key)
        if sites is None:
            sites = api.sites()
            site_cache.put(key, sites)
        return sites

    def remember_site(site):
        recent = [s for s in session.get("recent", []) if s["id"] != site["id"]]
        session["recent"] = ([{"id": site["id"], "name": site.get("name") or site["id"]}] + recent)[:RECENT_SITES]

    def check_csrf():
        token = request.form.get("csrf", "")
        if not token or not secrets.compare_digest(token, session.get("csrf", "")):
            activity("rejected form", path=request.path, reason="missing or expired form token")
            abort(400, "This form has expired - go back, reload the page and try again.")

    def require_write():
        if read_only(current_user()):
            activity("refused", path=request.path, reason="read-only user")
            abort(403, "Your login is read-only: you can view PSKs but not create or delete them.")

    def error_page(e: Exception, status=502):
        activity("error", config=session.get("config"), path=request.path, message=str(e))
        return render_template("error.html", message=str(e)), status

    @app.context_processor
    def inject_globals():
        user = current_user()
        ctx = {"version": __version__, "user": user, "csrf": session.get("csrf", ""),
               "fmt_expiry": fmt_expiry, "vlan_text": vlan_text}
        if user:
            ctx["read_only"] = read_only(user)
            ctx["config_name"] = session.get("config")
            ctx["config_count"] = len(allowed_configs(user))
        return ctx

    PUBLIC = {"login", "static", "manifest", "service_worker", "offline"}

    @app.before_request
    def require_login():
        if request.endpoint in PUBLIC:
            return None
        if not current_user():
            if session.get("user"):
                activity("signed out", reason="user no longer in web.conf")
            session.clear()
            return redirect(url_for("login"))
        session.permanent = True
        return None

    @app.after_request
    def headers(resp):
        if request.endpoint != "static":
            resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    @app.errorhandler(MpskError)
    def mpsk_error(e):
        return error_page(e)

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    def http_error(e):
        return render_template("error.html", message=e.description), e.code

    # -- PWA plumbing ----------------------------------------------------

    @app.get("/manifest.webmanifest")
    def manifest():
        return send_from_directory(app.static_folder, "manifest.webmanifest",
                                   mimetype="application/manifest+json")

    @app.get("/sw.js")
    def service_worker():
        resp = send_from_directory(app.static_folder, "sw.js", mimetype="text/javascript")
        resp.headers["Service-Worker-Allowed"] = "/"
        return resp

    @app.get("/offline")
    def offline():
        return render_template("offline.html")

    # -- sign in ---------------------------------------------------------

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET":
            if current_user():
                return redirect(url_for("index"))
            return render_template("login.html")
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        key = (request.remote_addr, username.lower())
        if limiter.locked(key):
            activity("sign-in blocked", user=username, reason="locked out after repeated wrong passwords")
            flash(f"Too many failed sign-ins - try again in {LOCKOUT_SECONDS // 60} minutes.", "error")
            return render_template("login.html", username=username), 429
        user = next((u for n, u in users.items() if n.lower() == username.lower()), None)
        ok = check_password_hash(user["password_hash"] if user else dummy_hash, password) and user is not None
        if not ok:
            limiter.fail(key)
            activity("sign-in failed", user=username, reason="wrong password" if user else "unknown user name")
            flash("Wrong user name or password.", "error")
            return render_template("login.html", username=username), 401
        limiter.clear(key)
        session.clear()
        session["user"] = user["name"]
        session["csrf"] = secrets.token_urlsafe(32)
        session.permanent = True
        activity("signed in", browser=request.headers.get("User-Agent", "")[:200])
        return redirect(url_for("index"))

    @app.post("/logout")
    def logout():
        check_csrf()
        activity("signed out")
        session.clear()
        flash("Signed out.", "info")
        return redirect(url_for("login"))

    @app.route("/configs", methods=["GET", "POST"])
    def configs():
        available = allowed_configs(current_user())
        if request.method == "POST":
            check_csrf()
            name = request.form.get("config", "")
            if name not in available:
                activity("config refused", config=name, reason="not assigned to this user")
                abort(403, "That config isn't assigned to you.")
            session["config"] = name
            session.pop("recent", None)
            activity("chose config", config=name)
            return redirect(url_for("index"))
        rows = []
        for n, p in available.items():
            try:
                cfg = core.load_config(p)
                rows.append({"name": n, "label": cfg.label, "cloud": cfg.base_url.split("/")[2], "error": None})
            except MpskError as e:
                rows.append({"name": n, "label": "", "cloud": "", "error": str(e)})
        return render_template("configs.html", rows=rows)

    # -- sites -----------------------------------------------------------

    @app.get("/")
    def index():
        if not allowed_configs(current_user()):
            return render_template("error.html", message=(
                "None of the config files assigned to you exist any more. Ask the admin to check web.conf.")), 500
        api = current_api()
        sites = site_list(api, refresh=request.args.get("refresh") == "1")
        return render_template("sites.html", org_name=org_name(api), sites=sites,
                               recent=session.get("recent", []))

    @app.get("/site/<site_id>")
    def site(site_id):
        api = current_api()
        site = api.site(site_id)
        remember_site(site)
        wlans = api.site_wlans(site_id)
        shown = {w["ssid"] for w in wlans}
        psks = api.psks(site_id)
        if api.cfg.ssids:
            psks = [p for p in psks if p.get("ssid") in shown]
        return render_template("site.html", site=site, psks=psks, wlans=wlans, now=time.time(),
                               org_name=org_name(api))

    # -- create ----------------------------------------------------------

    @app.route("/site/<site_id>/new", methods=["GET", "POST"])
    def new_psk(site_id):
        require_write()
        api = current_api()
        site = api.site(site_id)
        wlans = api.site_wlans(site_id)
        usable = [w for w in wlans if w["usable"]]
        if not usable:
            return render_template("error.html", message=(
                f"{site.get('name')} has no SSID that can use site PSKs. It needs an enabled WLAN with "
                "WPA2/WPA3 personal (PSK) security and local multiple passphrases - not RADIUS or "
                "Cloud PSKs (dynamic PSK).")), 409
        form = {"usage": "multi", "ssid": usable[0]["ssid"] if len(usable) == 1 else ""}
        problems = []
        if request.method == "POST":
            check_csrf()
            form = request.form.to_dict()
            if form.get("expiry") == "date":
                form["expiry"] = form.get("expiry_date", "").strip() or "date"
            body, problems = core.validate_new_psk(form, wlans, api.psks(site_id))
            if not problems:
                try:
                    created = api.create_psk(site_id, body)
                except MpskError as e:
                    activity("create failed", config=api.cfg.name, site=site.get("name"), psk=body["name"],
                             ssid=body["ssid"], message=str(e))
                    problems = [str(e)]
                else:
                    activity("created", config=api.cfg.name, site=site.get("name"), psk=body["name"],
                             ssid=body["ssid"], vlan=body.get("vlan_id"), usage=body["usage"],
                             expires=fmt_expiry(body.get("expire_time")) if body.get("expire_time") else None)
                    flash(f"PSK {body['name']} created.", "ok")
                    return redirect(url_for("psk", site_id=site_id, psk_id=created["id"], new=1))
        if not form.get("passphrase"):
            form["passphrase"] = core.generate_passphrase(api.cfg)
        return render_template("new.html", site=site, wlans=wlans, form=form, problems=problems,
                               alphabet=api.cfg.alphabet, length=api.cfg.length,
                               today=datetime.now().strftime("%Y-%m-%d"))

    # -- view / share / delete ------------------------------------------

    def load_psk(site_id, psk_id):
        api = current_api()
        site = api.site(site_id)
        p = api.psk(site_id, psk_id)
        wlan = next((w for w in api.site_wlans(site_id) if w["ssid"] == p.get("ssid")), None)
        if api.cfg.ssids and not wlan:
            abort(404, "That PSK's SSID isn't one this config manages.")
        hidden = bool(wlan and wlan["hidden"])
        return api, site, p, hidden

    @app.get("/site/<site_id>/psk/<psk_id>")
    def psk(site_id, psk_id):
        api, site, p, hidden = load_psk(site_id, psk_id)
        payload = core.wifi_qr_payload(p.get("ssid", ""), p.get("passphrase", ""), hidden)
        activity("viewed", config=api.cfg.name, site=site.get("name"), psk=p.get("name"), ssid=p.get("ssid"))
        return render_template("psk.html", site=site, psk=p, hidden=hidden, qr=Markup(core.qr_svg(payload)),
                               is_new=request.args.get("new") == "1",
                               expired=bool(p.get("expire_time")) and p["expire_time"] < time.time())

    @app.get("/site/<site_id>/psk/<psk_id>/qr.png")
    def psk_png(site_id, psk_id):
        api, site, p, hidden = load_psk(site_id, psk_id)
        payload = core.wifi_qr_payload(p.get("ssid", ""), p.get("passphrase", ""), hidden)
        png = core.qr_card_png(payload, p.get("ssid", ""), p.get("passphrase", ""),
                               f"{site.get('name', '')} - {p.get('name', '')}")
        activity("shared image", config=api.cfg.name, site=site.get("name"), psk=p.get("name"))
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in p.get("name", "psk"))[:40]
        return Response(png, mimetype="image/png",
                        headers={"Content-Disposition": f'inline; filename="wifi-{safe}.png"'})

    @app.post("/site/<site_id>/psk/<psk_id>/delete")
    def delete_psk(site_id, psk_id):
        check_csrf()
        require_write()
        api, site, p, _ = load_psk(site_id, psk_id)
        if request.form.get("confirm_name", "").strip().lower() != (p.get("name") or "").lower():
            flash("Type the PSK's name exactly to confirm the delete.", "error")
            return redirect(url_for("psk", site_id=site_id, psk_id=psk_id))
        try:
            api.delete_psk(site_id, psk_id)
        except MpskError as e:
            activity("delete failed", config=api.cfg.name, site=site.get("name"), psk=p.get("name"), message=str(e))
            raise
        activity("deleted", config=api.cfg.name, site=site.get("name"), psk=p.get("name"), ssid=p.get("ssid"))
        flash(f"PSK {p.get('name')} deleted. Devices using it will be disconnected at their next reconnect.", "ok")
        return redirect(url_for("site", site_id=site_id))

    return app


def hash_password_cli():
    if not sys.stdin.isatty():
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("New password: ")
        if password != getpass.getpass("Repeat password: "):
            sys.exit("Passwords don't match.")
    if len(password) < 8:
        sys.exit("Use at least 8 characters.")
    print(f"password_hash = {generate_password_hash(password)}")


def main():
    parser = argparse.ArgumentParser(description="Mist MPSK Mobile web app.")
    parser.add_argument("--web-conf", type=Path, default=DEFAULT_WEB_CONF,
                        help=f"users/settings file (default {DEFAULT_WEB_CONF.name} in the app folder)")
    parser.add_argument("--config-dir", type=Path, default=BASE_DIR,
                        help="folder holding the config .ini files (default: the app folder)")
    parser.add_argument("--read-only", action="store_true", help="nobody can create or delete PSKs")
    parser.add_argument("--hash-password", action="store_true",
                        help="prompt for a password and print the password_hash line for web.conf")
    parser.add_argument("--host", help="override [web] host")
    parser.add_argument("--port", type=int, help="override [web] port")
    args = parser.parse_args()

    if args.hash_password:
        hash_password_cli()
        return

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        web_conf = load_web_conf(args.web_conf, args.config_dir)
    except WebConfError as e:
        sys.exit(f"Error: {e}")
    host = args.host or web_conf["host"]
    port = args.port or web_conf["port"]
    scheme = "https" if web_conf["tls"] else "http"
    print(f"Mist MPSK Mobile {__version__}: {scheme}://{host}:{port}  "
          f"({len(web_conf['users'])} user(s), configs in {args.config_dir})")
    if args.read_only:
        print("Running with --read-only: nobody can create or delete PSKs.")
    create_app(web_conf, force_read_only=args.read_only, config_folder=args.config_dir).run(
        host=host, port=port, threaded=True, ssl_context=web_conf["tls"])


if __name__ == "__main__":
    main()
