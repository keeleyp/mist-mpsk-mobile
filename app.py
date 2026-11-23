#!/usr/bin/env python3
"""
Mist MPSK Mobile - a phone-friendly web app for creating Mist PSKs.

Runs on a Raspberry Pi (or any box). Open it on an iPhone or Android phone and
use "Add to Home Screen" to get an app icon. Sign in, pick a site, create a
PSK, then show or share its Wi-Fi QR code.

    python3 app.py                  # serve on [web] host/port from web.conf (optional)
    python3 app.py --reset-admin    # set user admin's password back to "admin"

On first start the app creates users.json with the user admin, password
admin, which must be changed at first sign-in. Admins add the other users in
the app. Org configs are .ini files in this folder (same format as the Mist
MPSK Generator's).
"""

import argparse
import configparser
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
from werkzeug.security import generate_password_hash

import mpsk_core as core
import users as userlib
from mpsk_core import MpskError
from users import GUEST_PSK_ROLE, ROLES, UserError, UserStore

__version__ = "1.1.0"

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_WEB_CONF = BASE_DIR / "web.conf"
DEFAULT_USERS = BASE_DIR / "users.json"
SECRET_KEY_FILE = BASE_DIR / ".secret_key"
ACTIVITY_LOG = BASE_DIR / "mpsk_activity.log"
ACTIVITY_LOG_MAX_BYTES = 5 * 1024 * 1024
ACTIVITY_LOG_BACKUPS = 10
DEFAULT_PORT = 5040

MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 15 * 60
SITE_CACHE_TTL = 5 * 60
RECENT_SITES = 5
ALL_SITES = "*"

log = logging.getLogger("mist_mpsk_mobile")


class WebConfError(Exception):
    pass


# --------------------------------------------------------------------------
# web.conf (optional): [web] settings. Old [user NAME] sections are only read
# once, to carry those logins into users.json as admins.
# --------------------------------------------------------------------------

def find_config_files(folder: Path = BASE_DIR) -> dict:
    return {p.name: p for p in sorted(folder.glob("*.ini"), key=lambda p: p.name.lower())}


def load_web_conf(path: Path) -> dict:
    parser = configparser.ConfigParser(interpolation=None)
    if path.exists():
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
    legacy = {}
    for section in parser.sections():
        if section.lower().startswith("user "):
            pw_hash = parser[section].get("password_hash", "").strip()
            if section[5:].strip() and ":" in pw_hash:
                legacy[section[5:].strip()] = pw_hash
    return {"host": (web.get("host", "0.0.0.0") or "0.0.0.0").strip(), "port": port,
            "session_hours": session_hours, "tls": tls, "legacy_users": legacy}


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
    """One tab-separated line per event. Passwords and passphrases are never logged."""
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

def create_app(web_conf: dict, store: UserStore, config_folder: Path = BASE_DIR,
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
    limiter = LoginLimiter()
    site_cache = TtlCache(SITE_CACHE_TTL)
    org_cache = TtlCache(60 * 60)
    dummy_hash = generate_password_hash(secrets.token_hex(16))

    # -- who may do what ---------------------------------------------------

    def current_user():
        name = session.get("user")
        return store.get(name) if name else None

    def is_admin(u):
        return u["role"] == "admin"

    def is_guest(u):
        return u["role"] == "guest"

    def can_manage_keys(u):
        """Delete keys and see every key (not just guest ones)."""
        return u["role"] in ("admin", "site_admin")

    def can_manage_users(u):
        return u["role"] in ("admin", "site_admin")

    def can_site(u, site_id):
        if is_admin(u):
            return True
        if u["role"] == "site_admin":
            return u.get("site_id") == site_id
        return u.get("site_id") in (None, ALL_SITES) or u.get("site_id") == site_id

    def user_configs(u) -> dict:
        available = find_config_files(config_folder)
        if is_admin(u):
            return available
        return {u["config"]: available[u["config"]]} if u.get("config") in available else {}

    def api_for(config_name) -> core.Mist:
        available = find_config_files(config_folder)
        if config_name not in available:
            raise MpskError(f"Config {config_name} no longer exists.")
        return core.Mist(core.load_config(available[config_name]))

    def current_api() -> core.Mist:
        configs = user_configs(current_user())
        if not configs:
            raise MpskError("The org config assigned to you no longer exists. Ask an admin to check it.")
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

    def deny(reason):
        activity("refused", path=request.path, reason=reason)
        abort(403, reason)

    def load_site(api, site_id):
        u = current_user()
        if not can_site(u, site_id):
            deny("You don't have access to that site.")
        return api.site(site_id)

    def visible(u, psks):
        return psks if can_manage_keys(u) else [p for p in psks if p.get("role") == GUEST_PSK_ROLE]

    @app.context_processor
    def inject_globals():
        u = current_user()
        ctx = {"version": __version__, "user": u, "csrf": session.get("csrf", ""), "roles": ROLES,
               "fmt_expiry": fmt_expiry, "vlan_text": vlan_text}
        if u:
            ctx.update(config_name=session.get("config"), config_count=len(user_configs(u)),
                       is_admin=is_admin(u), can_manage_keys=can_manage_keys(u),
                       can_manage_users=can_manage_users(u))
        return ctx

    PUBLIC = {"login", "static", "manifest", "service_worker", "offline"}
    WHILE_MUST_CHANGE = {"change_password", "logout"}

    @app.before_request
    def require_login():
        if request.endpoint in PUBLIC:
            return None
        u = current_user()
        if not u:
            if session.get("user"):
                activity("signed out", reason="user no longer exists")
            session.clear()
            return redirect(url_for("login"))
        session.permanent = True
        if u.get("must_change") and request.endpoint not in WHILE_MUST_CHANGE:
            return redirect(url_for("change_password"))
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
    @app.errorhandler(UserError)
    def app_error(e):
        activity("error", config=session.get("config"), path=request.path, message=str(e))
        return render_template("error.html", message=str(e)), 502 if isinstance(e, MpskError) else 400

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

    # -- sign in / out / password -----------------------------------------

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
        u = store.check_password(username, password, dummy_hash)
        if not u:
            limiter.fail(key)
            activity("sign-in failed", user=username,
                     reason="wrong password" if store.get(username) else "unknown user name")
            flash("Wrong user name or password.", "error")
            return render_template("login.html", username=username), 401
        limiter.clear(key)
        session.clear()
        session["user"] = u["name"]
        session["csrf"] = secrets.token_urlsafe(32)
        session.permanent = True
        activity("signed in", role=u["role"], browser=request.headers.get("User-Agent", "")[:200])
        return redirect(url_for("change_password" if u.get("must_change") else "index"))

    @app.post("/logout")
    def logout():
        check_csrf()
        activity("signed out")
        session.clear()
        flash("Signed out.", "info")
        return redirect(url_for("login"))

    @app.route("/password", methods=["GET", "POST"])
    def change_password():
        u = current_user()
        problems = []
        if request.method == "POST":
            check_csrf()
            current = request.form.get("current", "")
            new, repeat = request.form.get("new", ""), request.form.get("repeat", "")
            if not store.check_password(u["name"], current, dummy_hash):
                problems.append("Your current password is wrong.")
            if new != repeat:
                problems.append("The new passwords don't match.")
            if len(new) < userlib.MIN_PASSWORD:
                problems.append(f"Use at least {userlib.MIN_PASSWORD} characters.")
            if new and new == current:
                problems.append("Choose a password different from the current one.")
            if new.lower() == userlib.DEFAULT_ADMIN:
                problems.append("That password is too easy to guess.")
            if not problems:
                store.set_password(u["name"], new)
                activity("changed password")
                flash("Password changed.", "ok")
                return redirect(url_for("index"))
        return render_template("password.html", problems=problems, forced=u.get("must_change"))

    @app.route("/configs", methods=["GET", "POST"])
    def configs():
        available = user_configs(current_user())
        if request.method == "POST":
            check_csrf()
            name = request.form.get("config", "")
            if name not in available:
                deny("That org isn't assigned to you.")
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
        u = current_user()
        if not user_configs(u):
            return render_template("error.html", message=(
                "No org config is available to you. Ask an admin to check your user, "
                "or add an org .ini file to the app folder.")), 500
        if u["role"] == "site_admin" or (is_guest(u) and u.get("site_id") not in (None, ALL_SITES)):
            return redirect(url_for("site", site_id=u["site_id"]))
        api = current_api()
        sites = site_list(api, refresh=request.args.get("refresh") == "1")
        return render_template("sites.html", org_name=org_name(api), sites=sites,
                               recent=session.get("recent", []), show_org_keys=is_admin(u))

    def key_rows(psks, url_for_psk):
        return [dict(p, _url=url_for_psk(p["id"])) for p in psks]

    @app.get("/site/<site_id>")
    def site(site_id):
        u = current_user()
        api = current_api()
        site = load_site(api, site_id)
        remember_site(site)
        wlans = api.site_wlans(site_id)
        shown = {w["ssid"] for w in wlans}
        psks = visible(u, api.psks(site_id))
        if api.cfg.ssids:
            psks = [p for p in psks if p.get("ssid") in shown]
        back = None if (u["role"] == "site_admin" or (is_guest(u) and u.get("site_id") not in (None, ALL_SITES))) \
            else url_for("index")
        return render_template("keys.html", heading=site.get("name"), back_url=back, wlans=wlans,
                               psks=key_rows(psks, lambda pid: url_for("psk", site_id=site_id, psk_id=pid)),
                               new_url=url_for("new_psk", site_id=site_id), now=time.time(),
                               guest_view=is_guest(u),
                               none_usable=("No SSID at this site can use site PSKs (needs a WPA2/WPA3 "
                                            "Personal WLAN with local multiple passphrases)."))

    # -- create ----------------------------------------------------------

    def new_key_page(wlans, existing, create, subtitle, back_url, after_url, scope_log):
        u = current_user()
        api = current_api()
        form = {"usage": "multi"}
        usable = [w for w in wlans if w["usable"]]
        if len(usable) == 1:
            form["ssid"] = usable[0]["ssid"]
        problems = []
        if request.method == "POST":
            check_csrf()
            form = request.form.to_dict()
            if form.get("expiry") == "date":
                form["expiry"] = form.get("expiry_date", "").strip() or "date"
            role = GUEST_PSK_ROLE if is_guest(u) else None
            body, problems = core.validate_new_psk(form, wlans, existing(), role=role)
            if not problems:
                try:
                    created = create(body)
                except MpskError as e:
                    activity("create failed", config=api.cfg.name, **scope_log, psk=body["name"],
                             ssid=body["ssid"], message=str(e))
                    problems = [str(e)]
                else:
                    activity("created", config=api.cfg.name, **scope_log, psk=body["name"], ssid=body["ssid"],
                             vlan=body.get("vlan_id"), usage=body["usage"], psk_role=body.get("role"),
                             expires=fmt_expiry(body.get("expire_time")) if body.get("expire_time") else None)
                    flash(f"PSK {body['name']} created.", "ok")
                    return redirect(after_url(created["id"]))
        if not form.get("passphrase"):
            form["passphrase"] = core.generate_passphrase(api.cfg)
        return render_template("new.html", subtitle=subtitle, back_url=back_url, wlans=wlans, form=form,
                               problems=problems, alphabet=api.cfg.alphabet, length=api.cfg.length,
                               today=datetime.now().strftime("%Y-%m-%d"), guest_key=is_guest(u))

    @app.route("/site/<site_id>/new", methods=["GET", "POST"])
    def new_psk(site_id):
        api = current_api()
        site = load_site(api, site_id)
        wlans = api.site_wlans(site_id)
        if not any(w["usable"] for w in wlans):
            return render_template("error.html", message=(
                f"{site.get('name')} has no SSID that can use site PSKs. It needs an enabled WLAN with "
                "WPA2/WPA3 Personal (PSK) security and local multiple passphrases - not RADIUS or "
                "Cloud PSKs (dynamic PSK).")), 409
        return new_key_page(wlans, lambda: api.psks(site_id), lambda body: api.create_psk(site_id, body),
                            site.get("name"), url_for("site", site_id=site_id),
                            lambda pid: url_for("psk", site_id=site_id, psk_id=pid, new=1),
                            {"site": site.get("name")})

    # -- view / share / delete (site) -----------------------------------

    def check_key_visible(u, p):
        if not can_manage_keys(u) and p.get("role") != GUEST_PSK_ROLE:
            abort(404, "That PSK isn't one you can see.")

    def load_psk(site_id, psk_id):
        u = current_user()
        api = current_api()
        site = load_site(api, site_id)
        p = api.psk(site_id, psk_id)
        check_key_visible(u, p)
        wlan = next((w for w in api.site_wlans(site_id) if w["ssid"] == p.get("ssid")), None)
        if api.cfg.ssids and not wlan:
            abort(404, "That PSK's SSID isn't one this config manages.")
        return api, site, p, bool(wlan and wlan["hidden"])

    def psk_page(api, scope_name, p, hidden, back_url, png_url, delete_url, scope_log):
        payload = core.wifi_qr_payload(p.get("ssid", ""), p.get("passphrase", ""), hidden)
        activity("viewed", config=api.cfg.name, **scope_log, psk=p.get("name"), ssid=p.get("ssid"))
        return render_template("psk.html", scope_name=scope_name, psk=p, hidden=hidden,
                               qr=Markup(core.qr_svg(payload)), back_url=back_url, png_url=png_url,
                               delete_url=delete_url if can_manage_keys(current_user()) else None,
                               expired=bool(p.get("expire_time")) and p["expire_time"] < time.time())

    def png_response(api, scope_name, p, hidden, scope_log):
        payload = core.wifi_qr_payload(p.get("ssid", ""), p.get("passphrase", ""), hidden)
        png = core.qr_card_png(payload, p.get("ssid", ""), p.get("passphrase", ""),
                               f"{scope_name} - {p.get('name', '')}")
        activity("shared image", config=api.cfg.name, **scope_log, psk=p.get("name"))
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in p.get("name", "psk"))[:40]
        return Response(png, mimetype="image/png",
                        headers={"Content-Disposition": f'inline; filename="wifi-{safe}.png"'})

    def delete_key(api, p, do_delete, back_url, after_url, scope_log):
        check_csrf()
        if not can_manage_keys(current_user()):
            deny("Your role can't delete PSKs.")
        if request.form.get("confirm_name", "").strip().lower() != (p.get("name") or "").lower():
            flash("Type the PSK's name exactly to confirm the delete.", "error")
            return redirect(back_url)
        try:
            do_delete()
        except MpskError as e:
            activity("delete failed", config=api.cfg.name, **scope_log, psk=p.get("name"), message=str(e))
            raise
        activity("deleted", config=api.cfg.name, **scope_log, psk=p.get("name"), ssid=p.get("ssid"))
        flash(f"PSK {p.get('name')} deleted. Devices using it will be disconnected at their next reconnect.", "ok")
        return redirect(after_url)

    @app.get("/site/<site_id>/psk/<psk_id>")
    def psk(site_id, psk_id):
        api, site, p, hidden = load_psk(site_id, psk_id)
        return psk_page(api, site.get("name", ""), p, hidden, url_for("site", site_id=site_id),
                        url_for("psk_png", site_id=site_id, psk_id=psk_id),
                        url_for("delete_psk", site_id=site_id, psk_id=psk_id), {"site": site.get("name")})

    @app.get("/site/<site_id>/psk/<psk_id>/qr.png")
    def psk_png(site_id, psk_id):
        api, site, p, hidden = load_psk(site_id, psk_id)
        return png_response(api, site.get("name", ""), p, hidden, {"site": site.get("name")})

    @app.post("/site/<site_id>/psk/<psk_id>/delete")
    def delete_psk(site_id, psk_id):
        api, site, p, _ = load_psk(site_id, psk_id)
        return delete_key(api, p, lambda: api.delete_psk(site_id, psk_id),
                          url_for("psk", site_id=site_id, psk_id=psk_id), url_for("site", site_id=site_id),
                          {"site": site.get("name")})

    # -- org-level keys (admins only) -------------------------------------

    def require_admin():
        if not is_admin(current_user()):
            deny("Only admins can use org-level keys.")

    ORG_SCOPE = {"site": "(org level)"}

    @app.get("/org")
    def org_keys():
        require_admin()
        api = current_api()
        wlans = api.org_wlans()
        psks = api.org_psks()
        if api.cfg.ssids:
            psks = [p for p in psks if p.get("ssid") in api.cfg.ssids]
        return render_template("keys.html", heading="Org Level Keys", back_url=url_for("index"), wlans=wlans,
                               psks=key_rows(psks, lambda pid: url_for("org_psk", psk_id=pid)),
                               new_url=url_for("new_org_psk"), now=time.time(), guest_view=False,
                               subtitle=org_name(api),
                               none_usable=("No WLAN in this org uses Cloud PSKs. Org-level keys need a "
                                            "WPA2/WPA3 Personal WLAN with dynamic PSK from Cloud PSKs."))

    @app.route("/org/new", methods=["GET", "POST"])
    def new_org_psk():
        require_admin()
        api = current_api()
        wlans = api.org_wlans()
        if not wlans:
            return render_template("error.html", message=(
                "No WLAN in this org uses Cloud PSKs, so org-level keys wouldn't be used. Set a WLAN to "
                "WPA2/WPA3 Personal with dynamic PSK from Cloud PSKs first.")), 409
        return new_key_page(wlans, api.org_psks, api.create_org_psk, f"Org level - {org_name(api)}",
                            url_for("org_keys"), lambda pid: url_for("org_psk", psk_id=pid, new=1), ORG_SCOPE)

    def load_org_psk(psk_id):
        require_admin()
        api = current_api()
        p = api.org_psk(psk_id)
        wlan = next((w for w in api.org_wlans() if w["ssid"] == p.get("ssid")), None)
        return api, p, bool(wlan and wlan["hidden"])

    @app.get("/org/psk/<psk_id>")
    def org_psk(psk_id):
        api, p, hidden = load_org_psk(psk_id)
        return psk_page(api, f"{org_name(api)} (org level)", p, hidden, url_for("org_keys"),
                        url_for("org_psk_png", psk_id=psk_id), url_for("delete_org_psk", psk_id=psk_id), ORG_SCOPE)

    @app.get("/org/psk/<psk_id>/qr.png")
    def org_psk_png(psk_id):
        api, p, hidden = load_org_psk(psk_id)
        return png_response(api, org_name(api), p, hidden, ORG_SCOPE)

    @app.post("/org/psk/<psk_id>/delete")
    def delete_org_psk(psk_id):
        api, p, _ = load_org_psk(psk_id)
        return delete_key(api, p, lambda: api.delete_org_psk(psk_id), url_for("org_psk", psk_id=psk_id),
                          url_for("org_keys"), ORG_SCOPE)

    # -- app users ------------------------------------------------------

    def manageable(actor, target) -> bool:
        if actor["name"].lower() == target["name"].lower():
            return False
        if is_admin(actor):
            return True
        return (actor["role"] == "site_admin" and target["role"] == "guest"
                and target.get("config") == actor.get("config") and target.get("site_id") == actor.get("site_id"))

    def require_user_admin():
        u = current_user()
        if not can_manage_users(u):
            deny("Your role can't manage users.")
        return u

    def config_label(name):
        try:
            return core.load_config(find_config_files(config_folder)[name]).label or name
        except (KeyError, MpskError):
            return name

    @app.get("/users")
    def users_page():
        u = require_user_admin()
        rows = [dict(t, can_edit=manageable(u, t)) for t in store.all()
                if is_admin(u) or manageable(u, t) or t["name"] == u["name"]]
        return render_template("users.html", rows=rows, config_label=config_label)

    @app.route("/users/new", methods=["GET", "POST"])
    def new_user():
        u = require_user_admin()
        configs = find_config_files(config_folder) if is_admin(u) else {}
        if request.method == "POST":
            form = request.form.to_dict()
        else:   # re-shown after picking an org: keep name/role, never a password in the URL
            form = {k: v for k, v in request.args.items() if k in ("name", "role", "config")}
        if not is_admin(u):
            form.update(role="guest", config=u["config"], site_id=u["site_id"])
        form.setdefault("role", "guest")
        form.setdefault("config", next(iter(configs)) if len(configs) == 1 else "")
        form.setdefault("password", userlib.temp_password())
        sites, problems = [], []
        if is_admin(u) and form.get("config") in configs:
            try:
                sites = site_list(api_for(form["config"]))
            except MpskError as e:
                problems.append(f"Couldn't read the sites for {form['config']}: {e}")
        if request.method == "POST":
            check_csrf()
            role, config, site_id = form.get("role"), form.get("config"), form.get("site_id") or ""
            site_name = None
            if role != "admin":
                if is_admin(u):
                    if config not in configs:
                        problems.append("Pick the org for this user.")
                    elif role == "guest" and site_id == ALL_SITES:
                        site_name = "All sites"
                    else:
                        match = next((s for s in sites if s["id"] == site_id), None)
                        if not match:
                            problems.append("Pick a site." if role == "site_admin" else "Pick a site, or All sites.")
                        else:
                            site_name = match["name"]
                else:
                    site_name = u.get("site_name")
            if not problems:
                try:
                    created = store.add(form.get("name", ""), role, config, site_id or None, site_name,
                                        form.get("password", ""), created_by=u["name"])
                except UserError as e:
                    problems.append(str(e))
                else:
                    activity("added user", target=created["name"], role=role,
                             config=created.get("config"), site=site_name)
                    return render_template("user_created.html", created=created, password=form["password"],
                                           config_label=config_label)
        return render_template("user_new.html", form=form, configs=configs, sites=sites, problems=problems,
                               all_sites=ALL_SITES, config_label=config_label,
                               fixed=None if is_admin(u) else u)

    def target_user(name):
        u = require_user_admin()
        check_csrf()
        t = store.get(name)
        if not t or not manageable(u, t):
            deny("You can't change that user.")
        return u, t

    @app.post("/users/<name>/reset")
    def reset_user(name):
        u, t = target_user(name)
        password = userlib.temp_password()
        store.set_password(t["name"], password, must_change=True)
        activity("reset password", target=t["name"])
        return render_template("user_created.html", created=store.get(t["name"]), password=password,
                               reset=True, config_label=config_label)

    @app.post("/users/<name>/delete")
    def delete_user(name):
        u, t = target_user(name)
        store.delete(t["name"])
        activity("deleted user", target=t["name"], role=t["role"])
        flash(f"User {t['name']} deleted.", "ok")
        return redirect(url_for("users_page"))

    return app


def main():
    parser = argparse.ArgumentParser(description="Mist MPSK Mobile web app.")
    parser.add_argument("--web-conf", type=Path, default=DEFAULT_WEB_CONF,
                        help=f"settings file (optional; default {DEFAULT_WEB_CONF.name} in the app folder)")
    parser.add_argument("--users", type=Path, default=DEFAULT_USERS,
                        help=f"user store (default {DEFAULT_USERS.name} in the app folder)")
    parser.add_argument("--config-dir", type=Path, default=BASE_DIR,
                        help="folder holding the org config .ini files (default: the app folder)")
    parser.add_argument("--reset-admin", action="store_true",
                        help="set user admin's password back to 'admin' (must be changed at next sign-in) and exit")
    parser.add_argument("--host", help="override [web] host")
    parser.add_argument("--port", type=int, help="override [web] port")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = UserStore(args.users)
    if args.reset_admin:
        name = store.reset_admin()
        print(f"User {name}: password reset to 'admin' - it must be changed at the next sign-in.")
        return
    try:
        web_conf = load_web_conf(args.web_conf)
        for line in store.bootstrap(web_conf["legacy_users"]):
            print(f"Created user {line}")
    except (WebConfError, UserError) as e:
        sys.exit(f"Error: {e}")
    host = args.host or web_conf["host"]
    port = args.port or web_conf["port"]
    scheme = "https" if web_conf["tls"] else "http"
    print(f"Mist MPSK Mobile {__version__}: {scheme}://{host}:{port}  "
          f"({len(store.all())} user(s), configs in {args.config_dir})")
    create_app(web_conf, store, config_folder=args.config_dir).run(
        host=host, port=port, threaded=True, ssl_context=web_conf["tls"])


if __name__ == "__main__":
    main()
