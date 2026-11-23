import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as webapp          # noqa: E402
import mpsk_core as core      # noqa: E402
from fake_mist import FakeMist, write_config   # noqa: E402
from werkzeug.security import generate_password_hash   # noqa: E402

ORIGINAL_REQUEST = core.Mist.request
HASH = generate_password_hash("correct-horse")


class Base(unittest.TestCase):
    read_only = False
    extra_config = ""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        write_config(self.dir, extra=self.extra_config)
        self.fake = FakeMist().install()
        users = {"philip": {"name": "philip", "password_hash": HASH, "configs": ["*"], "all_configs": True,
                            "read_only": self.read_only}}
        conf = {"host": "127.0.0.1", "port": 0, "session_hours": 1, "tls": None, "users": users}
        self.app = webapp.create_app(conf, config_folder=self.dir, activity_log=self.dir / "act.log",
                                     secret_key="x" * 64)
        self.app.testing = True
        self.c = self.app.test_client()

    def tearDown(self):
        core.Mist.request = ORIGINAL_REQUEST
        self.tmp.cleanup()

    def login(self):
        r = self.c.post("/login", data={"username": "Philip", "password": "correct-horse"})
        self.assertEqual(r.status_code, 302)
        with self.c.session_transaction() as s:
            self.csrf = s["csrf"]

    def log(self):
        return (self.dir / "act.log").read_text()


class TestCore(unittest.TestCase):
    def test_qr_payload_escapes(self):
        self.assertEqual(core.wifi_qr_payload("A;B", 'p:w"d', True), r'WIFI:T:WPA;S:A\;B;P:p\:w\"d;H:true;;')

    def test_validate(self):
        wlans = [{"ssid": "Corp", "usable": True, "hidden": False}]
        body, problems = core.validate_new_psk(
            {"name": "x", "ssid": "Corp", "passphrase": "abcdefgh", "vlan": "{{ guest_vlan }}", "expiry": "7d"},
            wlans, [])
        self.assertEqual(problems, [])
        self.assertEqual(body["vlan_id"], "{{guest_vlan}}")
        self.assertIn("expire_time", body)
        _, problems = core.validate_new_psk(
            {"name": "", "ssid": "Nope", "passphrase": "short", "vlan": "5000", "expiry": "2001-01-01"}, wlans, [])
        self.assertEqual(len(problems), 5)

    def test_png(self):
        png = core.qr_card_png(core.wifi_qr_payload("S", "P" * 12, False), "S", "P" * 12, "Site - Name")
        self.assertTrue(png.startswith(b"\x89PNG"))


class TestFlow(Base):
    def test_requires_login(self):
        self.assertEqual(self.c.get("/").status_code, 302)
        self.assertEqual(self.c.get("/manifest.webmanifest").status_code, 200)
        self.assertEqual(self.c.get("/sw.js").status_code, 200)

    def test_wrong_password_and_lockout(self):
        for _ in range(5):
            self.assertEqual(self.c.post("/login", data={"username": "philip", "password": "x"}).status_code, 401)
        self.assertEqual(self.c.post("/login", data={"username": "philip", "password": "correct-horse"}).status_code, 429)

    def test_sites_and_site(self):
        self.login()
        r = self.c.get("/")
        self.assertIn(b"Acme Retail", r.data)
        self.assertIn(b"Leeds Warehouse", r.data)
        self.assertNotIn(b"Someone Else", r.data)
        r = self.c.get("/site/s1")
        self.assertIn(b"Reception iPad", r.data)
        self.assertIn(b"expired", r.data)
        self.assertIn(b"Cloud-Keys", r.data)       # warned about
        self.assertNotIn(b"Corp-1X", r.data)       # 802.1X not shown
        self.assertEqual(self.c.get("/site/x9").status_code, 502)   # other org's site refused

    def test_create_view_delete(self):
        self.login()
        r = self.c.get("/site/s1/new")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b'value="Cloud-Keys"', r.data)
        r = self.c.post("/site/s1/new", data={"csrf": self.csrf, "name": "Till 4", "ssid": "Corp-PSK",
                                              "passphrase": "GoodKey12345", "vlan": "120", "expiry": "date",
                                              "expiry_date": "2099-12-31", "usage": "multi"})
        self.assertEqual(r.status_code, 302, r.data[:500])
        created = self.fake.psks["s1"][-1]
        self.assertEqual(created["vlan_id"], 120)
        self.assertEqual(created["name"], "Till 4")
        self.assertIn("expire_time", created)
        r = self.c.get(r.headers["Location"])
        self.assertIn(b"<svg", r.data)
        self.assertIn(b"GoodKey12345", r.data)
        png = self.c.get(f"/site/s1/psk/{created['id']}/qr.png")
        self.assertEqual(png.mimetype, "image/png")
        # duplicate name on same SSID refused
        r = self.c.post("/site/s1/new", data={"csrf": self.csrf, "name": "till 4", "ssid": "Corp-PSK",
                                              "passphrase": "OtherKey1234", "usage": "multi"})
        self.assertIn(b"already a PSK", r.data)
        # delete needs the name typed
        r = self.c.post(f"/site/s1/psk/{created['id']}/delete", data={"csrf": self.csrf, "confirm_name": "nope"})
        self.assertEqual(r.status_code, 302)
        self.assertIn(created, self.fake.psks["s1"])
        self.c.post(f"/site/s1/psk/{created['id']}/delete", data={"csrf": self.csrf, "confirm_name": "Till 4"})
        self.assertNotIn(created, self.fake.psks["s1"])
        log = self.log()
        self.assertIn("event=created", log)
        self.assertIn("event=deleted", log)
        self.assertNotIn("GoodKey12345", log)

    def test_csrf(self):
        self.login()
        r = self.c.post("/site/s1/new", data={"name": "x", "ssid": "Corp-PSK", "passphrase": "abcdefghij"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(any(m == "POST" for m, _ in self.fake.calls))

    def test_no_usable_ssid(self):
        self.login()
        self.assertEqual(self.c.get("/site/s3/new").status_code, 409)

    def test_qr_escaping_in_page(self):
        self.login()
        r = self.c.post("/site/s2/new", data={"csrf": self.csrf, "name": "Guest", "ssid": "Store;Guest",
                                              "passphrase": "abc:def;ghij", "usage": "multi"})
        self.assertEqual(r.status_code, 302)


class TestReadOnly(Base):
    read_only = True

    def test_cannot_create(self):
        self.login()
        r = self.c.get("/site/s1")
        self.assertNotIn(b"New PSK", r.data)
        self.assertNotIn(b"Delete this PSK", self.c.get("/site/s1/psk/p1").data)
        self.assertEqual(self.c.get("/site/s1/new").status_code, 403)
        r = self.c.post("/site/s1/psk/p1/delete", data={"csrf": self.csrf, "confirm_name": "Reception iPad"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(len(self.fake.psks["s1"]), 2)


class TestSsidAllowList(Base):
    extra_config = "\n[app]\nssids = IoT-PSK\nname = Acme Test\n"

    def test_only_listed(self):
        self.login()
        r = self.c.get("/site/s1")
        self.assertIn(b"Old Printer", r.data)
        self.assertNotIn(b"Reception iPad", r.data)
        self.assertEqual(self.c.get("/site/s1/psk/p1").status_code, 404)
        self.assertIn(b"Acme Test", self.c.get("/").data)


class TestWebConf(unittest.TestCase):
    def test_parse(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            write_config(d)
            (d / "web.conf").write_text(f"[web]\nport = 5040\n[user a]\npassword_hash = {HASH}\nconfigs = acme.ini\n")
            conf = webapp.load_web_conf(d / "web.conf", d)
            self.assertEqual(conf["port"], 5040)
            (d / "web.conf").write_text(f"[user a]\npassword_hash = {HASH}\nconfigs = missing.ini\n")
            with self.assertRaises(webapp.WebConfError):
                webapp.load_web_conf(d / "web.conf", d)


if __name__ == "__main__":
    unittest.main()
