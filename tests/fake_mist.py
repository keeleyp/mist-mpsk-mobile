"""A small stateful stand-in for the Mist API, used by the tests (no network)."""
import re
import time
import uuid

import mpsk_core as core

ORG_ID = "00000000-0000-0000-0000-0000000000aa"
OTHER_ORG = "00000000-0000-0000-0000-0000000000bb"


class FakeMist:
    def __init__(self):
        self.calls = []
        self.sites = {
            "s1": {"id": "s1", "name": "London Office", "org_id": ORG_ID},
            "s2": {"id": "s2", "name": "Manchester Store", "org_id": ORG_ID},
            "s3": {"id": "s3", "name": "Leeds Warehouse", "org_id": ORG_ID},
            "x9": {"id": "x9", "name": "Someone Else", "org_id": OTHER_ORG},
        }
        psk = {"auth": {"type": "psk"}, "enabled": True}
        self.wlans = {
            "s1": [dict(psk, ssid="Corp-PSK"), dict(psk, ssid="IoT-PSK", hide_ssid=True),
                   {"ssid": "Corp-1X", "auth": {"type": "eap"}, "enabled": True},
                   dict(psk, ssid="Cloud-Keys", dynamic_psk={"enabled": True, "source": "cloud_psks"})],
            "s2": [dict(psk, ssid="Store;Guest")],
            "s3": [{"ssid": "Open", "auth": {"type": "open"}, "enabled": True}],
        }
        now = int(time.time())
        self.psks = {
            "s1": [{"id": "p1", "site_id": "s1", "name": "Reception iPad", "ssid": "Corp-PSK",
                    "passphrase": "Abc123xyz!", "usage": "multi", "vlan_id": 110, "created_time": now - 86400},
                   {"id": "p2", "site_id": "s1", "name": "Old Printer", "ssid": "IoT-PSK",
                    "passphrase": "Zz9Zz9Zz9", "usage": "single", "expire_time": now - 3600}],
            "s2": [], "s3": [],
        }

    def request(self, api, method, path, **kwargs):
        self.calls.append((method, path))
        if path == f"/orgs/{ORG_ID}":
            return {"id": ORG_ID, "name": "Acme Retail"}
        if path == f"/orgs/{ORG_ID}/sites":
            return [s for s in self.sites.values() if s["org_id"] == ORG_ID]
        m = re.fullmatch(r"/sites/([^/]+)(/.*)?", path)
        if not m or m.group(1) not in self.sites:
            raise core.MpskError(f"Mist API {method} {path} returned HTTP 404")
        sid, rest = m.group(1), m.group(2) or ""
        if rest == "":
            return self.sites[sid]
        if rest == "/wlans/derived":
            return self.wlans.get(sid, [])
        if rest == "/psks" and method == "GET":
            return list(self.psks.get(sid, []))
        if rest == "/psks" and method == "POST":
            body = dict(kwargs["json"], id=str(uuid.uuid4()), site_id=sid, created_time=int(time.time()))
            self.psks[sid].append(body)
            return body
        m2 = re.fullmatch(r"/psks/([^/]+)", rest)
        if m2:
            for p in self.psks.get(sid, []):
                if p["id"] == m2.group(1):
                    if method == "DELETE":
                        self.psks[sid].remove(p)
                        return {}
                    return p
        raise core.MpskError(f"Mist API {method} {path} returned HTTP 404")

    def install(self):
        fake = self

        def request(api, method, path, **kwargs):
            return fake.request(api, method, path, **kwargs)
        core.Mist.request = request
        return self


def write_config(folder, name="acme.ini", extra=""):
    (folder / name).write_text(
        "[mist]\ncloud = eu\napi_token = test-token\n"
        f"org_id = {ORG_ID}\n\n[psk]\nlength = 12\n{extra}")
