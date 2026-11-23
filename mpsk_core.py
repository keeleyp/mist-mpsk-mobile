"""
Mist MPSK Mobile - Mist API + config logic (no Flask in here).

A config is an .ini file with the same [mist] / [psk] / [network] sections as
the Mist MPSK Generator, so a mist_mpsk_generator.ini works as-is (its [files]
and [pdf] sections are ignored). One extra, optional section:

    [app]
    ssids = Corp-PSK, IoT-PSK     ; only offer these SSIDs (default: every eligible one)
    company = Customer A          ; company name shown in the app (default: the Mist org name)
"""

import configparser
import io
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

REQUEST_TIMEOUT = 30
PSK_AUTH_TYPES = {"psk", "psk-tkip", "psk-wpa2-tkip"}
QR_SPECIAL_CHARS = set('\\;,:"')
# Leaves out look-alikes (0 O 1 l I) and QR-escaped characters (\ ; , : ").
DEFAULT_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!#$%&*+-=?@"
DEFAULT_LENGTH = 12
VLAN_VAR_RE = re.compile(r"^\{\{\s*[A-Za-z_][A-Za-z0-9_]*\s*\}\}$")


class MpskError(Exception):
    pass


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

@dataclass
class Config:
    name: str            # file name, e.g. customer-a.ini
    label: str           # [app] company (or name), or "" (then the Mist org name is used)
    base_url: str
    api_token: str
    org_id: str
    alphabet: str
    length: int
    ssids: list = field(default_factory=list)   # allow-list, empty = all eligible
    verify: object = True
    proxies: dict = field(default_factory=dict)


def cloud_to_base_url(cloud: str) -> str:
    cloud = cloud.strip().lower()
    cloud = re.sub(r"^https?://", "", cloud).split("/")[0]
    if not cloud:
        raise MpskError("[mist] cloud is required (e.g. api.mist.com, api.eu.mist.com, api.gc3.mist.com).")
    if "." not in cloud:
        cloud = {"global": "api.mist.com", "us": "api.mist.com", "eu": "api.eu.mist.com"}.get(
            cloud, f"api.{cloud}.mist.com")
    if cloud.startswith("manage."):
        cloud = "api." + cloud[len("manage."):]
    return f"https://{cloud}/api/v1"


def read_network(parser: configparser.ConfigParser):
    """Optional [network] section for TLS-inspecting proxies (e.g. Zscaler).
    With no section, behaviour is normal (certifi CA store, env-var proxies)."""
    if "network" not in parser:
        return True, {}
    section = parser["network"]
    ca_bundle = section.get("ca_bundle", "").strip()
    verify_ssl = section.getboolean("verify_ssl", fallback=True)
    if ca_bundle:
        ca_path = Path(ca_bundle).expanduser()
        if not ca_path.is_file():
            raise MpskError(f"[network] ca_bundle does not exist: {ca_path} - this should be a PEM file "
                            "with your proxy's root CA certificate (e.g. exported from Zscaler).")
        verify = str(ca_path)
    else:
        verify = verify_ssl
    proxies = {}
    for scheme in ("http", "https"):
        value = section.get(f"{scheme}_proxy", "").strip()
        if value:
            proxies[scheme] = value
    return verify, proxies


def load_config(path: Path) -> Config:
    if not path.is_file():
        raise MpskError(f"Config file not found: {path.name}")
    # Interpolation off so '%' can appear in the allowed character list.
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except configparser.Error as e:
        raise MpskError(f"{path.name} could not be parsed: {e}")

    problems = []

    def get(section, key, default=None):
        if parser.has_option(section, key):
            return parser.get(section, key).strip()
        if default is None:
            problems.append(f"[{section}] {key} is missing")
            return ""
        return default

    api_token = get("mist", "api_token")
    org_id = get("mist", "org_id")
    cloud = get("mist", "cloud")
    alphabet_raw = get("psk", "allowed_characters", DEFAULT_ALPHABET)
    length_raw = get("psk", "length", str(DEFAULT_LENGTH))
    # [app] company is shown in the app instead of the Mist org name ([app] name is the old spelling)
    label = get("app", "company", "") or get("app", "name", "")
    ssids = [s.strip() for s in get("app", "ssids", "").split(",") if s.strip()]

    alphabet = "".join(dict.fromkeys(alphabet_raw))
    bad = [ch for ch in alphabet if not (0x20 <= ord(ch) <= 0x7E)]
    if bad:
        problems.append("[psk] allowed_characters may only contain printable ASCII")
    if len(alphabet) < 10:
        problems.append(f"[psk] allowed_characters has only {len(alphabet)} distinct characters; use at least 10")
    try:
        length = int(length_raw)
        if not 8 <= length <= 63:
            problems.append("[psk] length must be between 8 and 63")
    except ValueError:
        length = 0
        problems.append(f"[psk] length must be a whole number, got {length_raw!r}")
    base_url = ""
    if cloud:
        try:
            base_url = cloud_to_base_url(cloud)
        except MpskError as e:
            problems.append(str(e))
    try:
        verify, proxies = read_network(parser)
    except MpskError as e:
        problems.append(str(e))
        verify, proxies = True, {}

    if problems:
        raise MpskError(f"Config problems in {path.name}: " + "; ".join(problems))
    return Config(name=path.name, label=label, base_url=base_url, api_token=api_token, org_id=org_id,
                  alphabet=alphabet, length=length, ssids=ssids, verify=verify, proxies=proxies)


# --------------------------------------------------------------------------
# Mist API
# --------------------------------------------------------------------------

class Mist:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base = cfg.base_url
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Token {cfg.api_token}",
                               "Content-Type": "application/json"})
        self.s.verify = cfg.verify
        if cfg.proxies:
            self.s.proxies.update(cfg.proxies)
        if cfg.verify is False:
            requests.packages.urllib3.disable_warnings(
                requests.packages.urllib3.exceptions.InsecureRequestWarning)

    def request(self, method: str, path: str, **kwargs):
        url = self.base + path
        for attempt in range(3):
            try:
                r = self.s.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
            except requests.exceptions.SSLError:
                raise MpskError("TLS certificate check failed talking to Mist. Behind a TLS-inspecting "
                                "proxy (e.g. Zscaler)? Set [network] ca_bundle in the config.")
            except requests.exceptions.ProxyError as e:
                raise MpskError(f"Proxy error talking to Mist: {e}. Check [network] http_proxy / https_proxy.")
            except requests.exceptions.RequestException as e:
                raise MpskError(f"Network error talking to Mist: {e}")
            if r.status_code == 429 and attempt < 2:
                time.sleep(int(r.headers.get("Retry-After", "0") or 0) or 2 ** (attempt + 1))
                continue
            if r.status_code >= 400:
                hint = {401: " - check api_token and cloud",
                        403: " - the token lacks permission (creating PSKs needs write access to the site)",
                        404: " - check org_id and cloud"}.get(r.status_code, "")
                detail = _error_detail(r)
                raise MpskError(f"Mist API {method} {path} returned HTTP {r.status_code}{hint}"
                                + (f": {detail}" if detail else ""))
            return r.json() if r.content else {}
        raise MpskError("Mist API rate limit reached - wait a minute and try again.")

    def get_all(self, path: str, params: dict | None = None) -> list:
        out, page = [], 1
        params = dict(params or {})
        while True:
            params.update({"limit": 1000, "page": page})
            batch = self.request("GET", path, params=params)
            if not isinstance(batch, list):
                raise MpskError(f"Unexpected response from {path}")
            out.extend(batch)
            if len(batch) < 1000:
                return out
            page += 1

    # -- reads ------------------------------------------------------------

    def org(self) -> dict:
        return self.request("GET", f"/orgs/{self.cfg.org_id}")

    def sites(self) -> list:
        sites = self.get_all(f"/orgs/{self.cfg.org_id}/sites")
        return sorted(({"id": s["id"], "name": s.get("name") or s["id"]} for s in sites),
                      key=lambda s: s["name"].lower())

    def site(self, site_id: str) -> dict:
        site = self.request("GET", f"/sites/{site_id}")
        if site.get("org_id") != self.cfg.org_id:
            raise MpskError("That site is not in this config's org.")
        return site

    def site_wlans(self, site_id: str) -> list:
        """Every enabled SSID at the site, with whether site PSKs can be used on it."""
        out = {}
        for w in self.request("GET", f"/sites/{site_id}/wlans/derived"):
            if w.get("enabled", True) is False or not w.get("ssid"):
                continue
            entry = out.setdefault(w["ssid"], {"ssid": w["ssid"], "hidden": False, "problem": None,
                                               "psk_wlans": 0})
            entry["hidden"] = entry["hidden"] or bool(w.get("hide_ssid"))
            auth = (w.get("auth") or {}).get("type")
            if auth not in PSK_AUTH_TYPES:
                continue
            reason = dynamic_psk_problem(w)
            if reason:
                entry["problem"] = reason
            else:
                entry["psk_wlans"] += 1
        result = []
        for e in out.values():
            if e["psk_wlans"] == 0 and not e["problem"]:
                continue    # not a PSK SSID at all (open, 802.1X ...) - not shown
            e["usable"] = e["psk_wlans"] > 0 and not e["problem"]
            if e["usable"] and self.cfg.ssids and e["ssid"] not in self.cfg.ssids:
                continue    # not in this config's allow-list
            result.append(e)
        return sorted(result, key=lambda e: e["ssid"].lower())

    def psks(self, site_id: str) -> list:
        return sorted(self.get_all(f"/sites/{site_id}/psks"),
                      key=lambda p: ((p.get("name") or "").lower(), p.get("ssid") or ""))

    def psk(self, site_id: str, psk_id: str) -> dict:
        p = self.request("GET", f"/sites/{site_id}/psks/{psk_id}")
        if p.get("site_id") and p["site_id"] != site_id:
            raise MpskError("That PSK is not at this site.")
        return p

    # -- org-level keys (Cloud PSKs) -------------------------------------

    def org_wlans(self) -> list:
        """SSIDs that use org-level keys: WPA2/3 Personal with dynamic PSK
        from Cloud PSKs. Same shape as site_wlans()."""
        out = {}
        for w in self.get_all(f"/orgs/{self.cfg.org_id}/wlans"):
            if w.get("enabled", True) is False or not w.get("ssid"):
                continue
            if (w.get("auth") or {}).get("type") not in PSK_AUTH_TYPES:
                continue
            dpsk = w.get("dynamic_psk") or {}
            if not (dpsk.get("enabled") and dpsk.get("source") == "cloud_psks"):
                continue
            e = out.setdefault(w["ssid"], {"ssid": w["ssid"], "hidden": False, "problem": None,
                                           "psk_wlans": 0, "usable": True})
            e["hidden"] = e["hidden"] or bool(w.get("hide_ssid"))
            e["psk_wlans"] += 1
        result = [e for e in out.values() if not self.cfg.ssids or e["ssid"] in self.cfg.ssids]
        return sorted(result, key=lambda e: e["ssid"].lower())

    def org_local_psk_ssids(self) -> list:
        """SSIDs of the org's WLANs (templates) that use local site PSKs - the
        choices for a user who works at every site."""
        ssids = set()
        for w in self.get_all(f"/orgs/{self.cfg.org_id}/wlans"):
            if w.get("enabled", True) is False or not w.get("ssid"):
                continue
            if (w.get("auth") or {}).get("type") in PSK_AUTH_TYPES and not dynamic_psk_problem(w):
                ssids.add(w["ssid"])
        if self.cfg.ssids:
            ssids &= set(self.cfg.ssids)
        return sorted(ssids, key=str.lower)

    def org_psks(self) -> list:
        return sorted(self.get_all(f"/orgs/{self.cfg.org_id}/psks"),
                      key=lambda p: ((p.get("name") or "").lower(), p.get("ssid") or ""))

    def org_psk(self, psk_id: str) -> dict:
        p = self.request("GET", f"/orgs/{self.cfg.org_id}/psks/{psk_id}")
        if p.get("site_id"):
            raise MpskError("That is a site PSK, not an org-level one.")
        return p

    def create_org_psk(self, body: dict) -> dict:
        return self.request("POST", f"/orgs/{self.cfg.org_id}/psks", json=body)

    def delete_org_psk(self, psk_id: str) -> None:
        self.request("DELETE", f"/orgs/{self.cfg.org_id}/psks/{psk_id}")

    # -- writes -----------------------------------------------------------

    def create_psk(self, site_id: str, body: dict) -> dict:
        return self.request("POST", f"/sites/{site_id}/psks", json=body)

    def delete_psk(self, site_id: str, psk_id: str) -> None:
        self.request("DELETE", f"/sites/{site_id}/psks/{psk_id}")


def _error_detail(r) -> str:
    try:
        body = r.json()
        if isinstance(body, dict):
            return str(body.get("detail") or body.get("message") or body)[:300]
    except ValueError:
        pass
    return r.text.strip()[:300]


def dynamic_psk_problem(wlan: dict) -> str | None:
    """Site PSKs are only used by a PSK WLAN whose keys are local. A dynamic
    PSK WLAN takes keys from RADIUS or from org-level Cloud PSKs instead."""
    dpsk = wlan.get("dynamic_psk") or {}
    if not dpsk.get("enabled"):
        return None
    source = dpsk.get("source") or "radius"
    if source == "cloud_psks":
        return "uses org-level Cloud PSKs, so site PSKs would be ignored"
    return f"takes its keys from {'RADIUS' if source == 'radius' else source}, so site PSKs would be ignored"


# --------------------------------------------------------------------------
# PSK helpers
# --------------------------------------------------------------------------

def generate_passphrase(cfg: Config, avoid=()) -> str:
    avoid = set(avoid)
    while True:
        key = "".join(secrets.choice(cfg.alphabet) for _ in range(cfg.length))
        if key not in avoid and key[0] != " " and key[-1] != " ":
            return key


def validate_new_psk(form: dict, wlans: list, existing: list, role: str | None = None) -> tuple[dict, list]:
    """Turns the New PSK form into a Mist POST body. Returns (body, problems)."""
    problems = []
    name = (form.get("name") or "").strip()
    ssid = form.get("ssid") or ""
    passphrase = form.get("passphrase") or ""
    vlan_raw = (form.get("vlan") or "").strip()
    usage = form.get("usage") or "multi"
    expiry = (form.get("expiry") or "").strip()     # "", "1d", "7d", "30d", "90d" or YYYY-MM-DD

    if not name:
        problems.append("Give the PSK a name.")
    elif len(name) > 64:
        problems.append("Keep the name to 64 characters or fewer.")
    usable = {w["ssid"]: w for w in wlans if w["usable"]}
    if ssid not in usable:
        problems.append("Pick an SSID from the list.")
    if not 8 <= len(passphrase) <= 63:
        problems.append("The passphrase must be 8 to 63 characters.")
    elif any(not (0x20 <= ord(c) <= 0x7E) for c in passphrase):
        problems.append("The passphrase may only use plain keyboard characters (printable ASCII).")
    elif passphrase != passphrase.strip():
        problems.append("The passphrase can't start or end with a space.")
    if name and ssid and any((p.get("name") or "").lower() == name.lower() and p.get("ssid") == ssid
                             for p in existing):
        problems.append(f"There is already a PSK called {name!r} on {ssid} at this site.")
    if passphrase and any(p.get("passphrase") == passphrase and p.get("ssid") == ssid for p in existing):
        problems.append("Another PSK on this SSID already uses that passphrase - generate a new one.")
    if usage not in ("multi", "single"):
        problems.append("Usage must be multi or single.")

    body = {"name": name, "ssid": ssid, "passphrase": passphrase, "usage": usage}
    if role:
        body["role"] = role
    if vlan_raw:
        if vlan_raw.isdigit() and 1 <= int(vlan_raw) <= 4094:
            body["vlan_id"] = int(vlan_raw)
        elif VLAN_VAR_RE.match(vlan_raw):
            body["vlan_id"] = vlan_raw.replace(" ", "")
        else:
            problems.append("VLAN must be 1-4094, a site variable like {{guest_vlan}}, or blank.")
    if expiry:
        now = int(time.time())
        days = {"1d": 1, "7d": 7, "30d": 30, "90d": 90}.get(expiry)
        if days:
            body["expire_time"] = now + days * 86400
        else:
            try:
                t = time.strptime(expiry, "%Y-%m-%d")
                # end of that day, Pi local time
                body["expire_time"] = int(time.mktime(t)) + 86399
                if body["expire_time"] <= now:
                    problems.append("The expiry date is in the past.")
            except ValueError:
                problems.append("Expiry must be a date (YYYY-MM-DD) or one of the presets.")
    return body, problems


def wifi_qr_payload(ssid: str, passphrase: str, hidden: bool) -> str:
    def esc(s: str) -> str:
        return "".join("\\" + ch if ch in QR_SPECIAL_CHARS else ch for ch in s)
    return f"WIFI:T:WPA;S:{esc(ssid)};P:{esc(passphrase)};{'H:true;' if hidden else ''};"


def qr_svg(payload: str) -> str:
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(payload, image_factory=qrcode.image.svg.SvgPathImage,
                      error_correction=qrcode.constants.ERROR_CORRECT_M, border=2)
    svg = img.to_string(encoding="unicode")
    # drop the XML declaration so it can be inlined, and let CSS size it
    svg = re.sub(r"^<\?xml[^>]*>\s*", "", svg)
    return re.sub(r'\s(width|height)="[^"]*"', "", svg, count=2)


def qr_card_png(payload: str, ssid: str, passphrase: str, title: str) -> bytes:
    """A shareable image: QR code with the SSID and passphrase printed under it."""
    import qrcode
    from PIL import Image, ImageDraw, ImageFont

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=14, border=3)
    qr.add_data(payload)
    qr.make(fit=True)
    code = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    w = max(code.width, 640)
    card = Image.new("RGB", (w, code.height + 230), "white")
    card.paste(code, ((w - code.width) // 2, 0))
    draw = ImageDraw.Draw(card)

    def font(size, mono=False):
        names = (["DejaVuSansMono.ttf", "Menlo.ttc", "Courier New.ttf"] if mono
                 else ["DejaVuSans.ttf", "Helvetica.ttc", "Arial.ttf"])
        for n in names:
            try:
                return ImageFont.truetype(n, size)
            except OSError:
                continue
        return ImageFont.load_default(size)

    y = code.height + 5
    for text, size, mono, fill in ((title, 30, False, "#555555"), (f"Wi-Fi: {ssid}", 38, False, "black"),
                                   (f"Password: {passphrase}", 36, True, "black")):
        f = font(size, mono)
        while draw.textlength(text, font=f) > w - 40 and size > 14:
            size -= 2
            f = font(size, mono)
        draw.text(((w - draw.textlength(text, font=f)) // 2, y), text, font=f, fill=fill)
        y += size + 26
    buf = io.BytesIO()
    card.save(buf, "PNG")
    return buf.getvalue()
