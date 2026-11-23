import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as webapp          # noqa: E402
import mpsk_core as core      # noqa: E402
from fake_mist import FakeMist, write_config   # noqa: E402
from users import UserStore, UserError        # noqa: E402

ORIGINAL_REQUEST = core.Mist.request
CONFIG = "acme.ini"


class Base(unittest.TestCase):
    extra_config = ""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        write_config(self.dir, extra=self.extra_config)
        self.fake = FakeMist().install()
        self.store = UserStore(self.dir / "users.json")
        self.store.bootstrap()
        self.store.set_password("admin", "admin-pass-1")       # past first sign-in for most tests
        conf = {"host": "127.0.0.1", "port": 0, "session_hours": 1, "tls": None, "legacy_users": {}}
        self.app = webapp.create_app(conf, self.store, config_folder=self.dir,
                                     activity_log=self.dir / "act.log", secret_key="x" * 64)
        self.app.testing = True
        self.c = self.app.test_client()

    def tearDown(self):
        core.Mist.request = ORIGINAL_REQUEST
        self.tmp.cleanup()

    def login(self, name="admin", password="admin-pass-1", client=None):
        c = client or self.c
        r = c.post("/login", data={"username": name, "password": password})
        self.assertEqual(r.status_code, 302, r.data[:300])
        with c.session_transaction() as s:
            self.csrf = s["csrf"]
        return r

    def add(self, name, role, site_id=None, site_name=None, password="first-pass-1", ssid=None, key_expiry=None):
        if role in ("guest", "self_service") and ssid is None:
            ssid = "Corp-PSK"
        self.store.add(name, role, CONFIG, site_id, site_name, password, created_by="test",
                       ssid=ssid, key_expiry=key_expiry if key_expiry is not None else "1d")
        self.store.set_password(name, password)      # skip the forced change
        return name

    def log(self):
        return (self.dir / "act.log").read_text()


class TestCore(unittest.TestCase):
    def test_qr_payload_escapes(self):
        self.assertEqual(core.wifi_qr_payload("A;B", 'p:w"d', True), r'WIFI:T:WPA;S:A\;B;P:p\:w\"d;H:true;;')

    def test_validate(self):
        wlans = [{"ssid": "Corp", "usable": True, "hidden": False}]
        body, problems = core.validate_new_psk(
            {"name": "x", "ssid": "Corp", "passphrase": "abcdefgh", "vlan": "{{ guest_vlan }}", "expiry": "7d"},
            wlans, [], role="guest")
        self.assertEqual(problems, [])
        self.assertEqual(body["vlan_id"], "{{guest_vlan}}")
        self.assertEqual(body["role"], "guest")
        self.assertIn("expire_time", body)
        _, problems = core.validate_new_psk(
            {"name": "", "ssid": "Nope", "passphrase": "short", "vlan": "5000", "expiry": "2001-01-01"}, wlans, [])
        self.assertEqual(len(problems), 5)

    def test_png(self):
        png = core.qr_card_png(core.wifi_qr_payload("S", "P" * 12, False), "S", "P" * 12, "Site - Name")
        self.assertTrue(png.startswith(b"\x89PNG"))


class TestBootstrap(unittest.TestCase):
    def test_first_start_and_forced_change(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            write_config(d)
            FakeMist().install()
            try:
                store = UserStore(d / "users.json")
                created = store.bootstrap({"philip": "scrypt:fake:hash"})
                self.assertEqual(len(created), 2)
                self.assertEqual(store.get("philip")["role"], "admin")
                self.assertTrue(store.get("admin")["must_change"])
                self.assertEqual(oct((d / "users.json").stat().st_mode & 0o777), "0o600")
                self.assertEqual(store.bootstrap(), [])          # only once
                conf = {"session_hours": 1, "tls": None}
                c = webapp.create_app(conf, store, config_folder=d, activity_log=d / "a.log",
                                      secret_key="y" * 64).test_client()
                r = c.post("/login", data={"username": "admin", "password": "admin"})
                self.assertTrue(r.headers["Location"].endswith("/password"))
                self.assertTrue(c.get("/").headers["Location"].endswith("/password"))
                with c.session_transaction() as s:
                    csrf = s["csrf"]
                r = c.post("/password", data={"csrf": csrf, "current": "admin", "new": "admin", "repeat": "admin"})
                self.assertIn(b"too easy", r.data)
                r = c.post("/password", data={"csrf": csrf, "current": "admin", "new": "better-pass",
                                              "repeat": "better-pass"})
                self.assertEqual(r.status_code, 302)
                self.assertFalse(store.get("admin")["must_change"])
                self.assertEqual(c.get("/").status_code, 200)
                store.reset_admin()
                self.assertTrue(store.get("admin")["must_change"])
            finally:
                core.Mist.request = ORIGINAL_REQUEST

    def test_last_admin(self):
        with tempfile.TemporaryDirectory() as d:
            store = UserStore(Path(d) / "users.json")
            store.bootstrap()
            with self.assertRaises(UserError):
                store.delete("admin")


class TestAdmin(Base):
    def test_requires_login(self):
        self.assertEqual(self.c.get("/").status_code, 302)
        self.assertEqual(self.c.get("/manifest.webmanifest").status_code, 200)

    def test_lockout(self):
        for _ in range(5):
            self.assertEqual(self.c.post("/login", data={"username": "admin", "password": "x"}).status_code, 401)
        self.assertEqual(self.c.post("/login", data={"username": "admin", "password": "admin-pass-1"}).status_code, 429)

    def test_sites_and_org_row(self):
        self.login()
        r = self.c.get("/")
        self.assertIn(b"Org Level Keys", r.data)
        self.assertIn(b"Sign out", r.data)
        self.assertNotIn(b"Someone Else", r.data)
        r = self.c.get("/site/s1")
        for name in (b"Reception iPad", b"Old Printer", b"Visitor Bob"):
            self.assertIn(name, r.data)
        self.assertEqual(self.c.get("/site/x9").status_code, 502)

    def test_create_view_delete_site_key(self):
        self.login()
        r = self.c.post("/site/s1/new", data={"csrf": self.csrf, "name": "Till 4", "ssid": "Corp-PSK",
                                              "passphrase": "GoodKey12345", "vlan": "120", "expiry": "date",
                                              "expiry_date": "2099-12-31", "usage": "multi"})
        self.assertEqual(r.status_code, 302, r.data[:500])
        created = self.fake.psks["s1"][-1]
        self.assertEqual(created["vlan_id"], 120)
        self.assertNotIn("role", created)
        self.assertIn(b"<svg", self.c.get(r.headers["Location"]).data)
        self.assertEqual(self.c.get(f"/site/s1/psk/{created['id']}/qr.png").mimetype, "image/png")
        self.c.post(f"/site/s1/psk/{created['id']}/delete", data={"csrf": self.csrf, "confirm_name": "nope"})
        self.assertIn(created, self.fake.psks["s1"])
        self.c.post(f"/site/s1/psk/{created['id']}/delete", data={"csrf": self.csrf, "confirm_name": "Till 4"})
        self.assertNotIn(created, self.fake.psks["s1"])
        self.assertNotIn("GoodKey12345", self.log())

    def test_org_keys(self):
        self.login()
        r = self.c.get("/org")
        self.assertIn(b"Exec Laptop", r.data)
        r = self.c.get("/org/new")
        self.assertIn(b'value="Cloud-Keys"', r.data)
        self.assertNotIn(b'value="Corp-PSK"', r.data)       # local-key WLAN isn't for org keys
        r = self.c.post("/org/new", data={"csrf": self.csrf, "name": "CEO Phone", "ssid": "Cloud-Keys",
                                          "passphrase": "OrgKey123456", "usage": "multi"})
        self.assertEqual(r.status_code, 302, r.data[:500])
        new = self.fake.org_psks[-1]
        self.assertEqual(new["name"], "CEO Phone")
        self.assertIn(b"<svg", self.c.get(f"/org/psk/{new['id']}").data)
        self.c.post(f"/org/psk/{new['id']}/delete", data={"csrf": self.csrf, "confirm_name": "CEO Phone"})
        self.assertNotIn(new, self.fake.org_psks)

    def test_add_users(self):
        self.login()
        r = self.c.get(f"/users/new?config={CONFIG}&name=bob&role=guest")
        self.assertIn(b"All sites", r.data)
        self.assertIn(b"London Office", r.data)
        r = self.c.get(f"/users/new?config={CONFIG}&name=bob&role=guest&site_id=*")
        self.assertIn(b'value="Corp-PSK"', r.data)            # org template SSID with local keys
        self.assertNotIn(b'value="Cloud-Keys"', r.data)       # cloud PSK WLAN isn't for site keys
        r = self.c.post("/users/new", data={"csrf": self.csrf, "name": "bob", "role": "guest", "config": CONFIG,
                                            "site_id": "*", "password": "bobs-first-1"})
        self.assertIn(b"Pick the SSID", r.data)
        r = self.c.post("/users/new", data={"csrf": self.csrf, "name": "bob", "role": "guest", "config": CONFIG,
                                            "site_id": "*", "ssid": "Corp-PSK", "password": "bobs-first-1"})
        self.assertIn(b"bobs-first-1", r.data)
        bob = self.store.get("bob")
        self.assertEqual((bob["role"], bob["site_id"], bob["ssid"], bob["must_change"]), ("guest", "*", "Corp-PSK", True))
        r = self.c.post("/users/new", data={"csrf": self.csrf, "name": "sam", "role": "site_admin",
                                            "config": CONFIG, "site_id": "*", "password": "sams-first-1"})
        self.assertIn(b"Pick a site", r.data)
        self.c.post("/users/new", data={"csrf": self.csrf, "name": "sam", "role": "site_admin", "config": CONFIG,
                                        "site_id": "s1", "password": "sams-first-1"})
        self.assertEqual(self.store.get("sam")["site_name"], "London Office")
        r = self.c.post("/users/bob/reset", data={"csrf": self.csrf})
        self.assertIn(b"Password reset", r.data)
        self.c.post("/users/bob/delete", data={"csrf": self.csrf})
        self.assertIsNone(self.store.get("bob"))
        self.assertEqual(self.c.post("/users/admin/delete", data={"csrf": self.csrf}).status_code, 403)
        self.assertNotIn("bobs-first-1", self.log())

    def test_csrf(self):
        self.login()
        self.assertEqual(self.c.post("/site/s1/new", data={"name": "x"}).status_code, 400)
        self.assertFalse(any(m == "POST" for m, _ in self.fake.calls))


class TestSiteAdmin(Base):
    def setUp(self):
        super().setUp()
        self.add("sam", "site_admin", "s1", "London Office")
        self.login("sam", "first-pass-1")

    def test_scope(self):
        r = self.c.get("/")
        self.assertTrue(r.headers["Location"].endswith("/site/s1"))
        r = self.c.get("/site/s1")
        self.assertIn(b"Reception iPad", r.data)
        self.assertIn(b"Visitor Bob", r.data)
        self.assertNotIn(b"Org Level Keys", r.data)
        self.assertEqual(self.c.get("/site/s2").status_code, 403)
        self.assertEqual(self.c.get("/org").status_code, 403)
        self.assertIn(b"Delete this PSK", self.c.get("/site/s1/psk/p1").data)

    def test_adds_only_guests_for_own_site(self):
        r = self.c.get("/users/new")
        self.assertIn(b'value="IoT-PSK"', r.data)            # London's SSIDs
        self.assertNotIn(b'value="admin"', r.data)           # can't offer admin roles
        r = self.c.post("/users/new", data={"csrf": self.csrf, "name": "gail", "role": "admin", "config": "x",
                                            "site_id": "s2", "ssid": "IoT-PSK", "password": "gails-first-1"})
        self.assertIn(b"User added", r.data)
        gail = self.store.get("gail")
        self.assertEqual((gail["role"], gail["site_id"], gail["config"], gail["ssid"]), ("guest", "s1", CONFIG, "IoT-PSK"))
        r = self.c.post("/users/new", data={"csrf": self.csrf, "name": "desk", "role": "self_service",
                                            "ssid": "Corp-PSK", "key_expiry": "7d", "password": "desk-first-1"})
        self.assertIn(b"User added", r.data)
        self.assertEqual(self.store.get("desk")["key_expiry"], "7d")
        self.add("other", "guest", "s2", "Manchester Store")
        self.assertEqual(self.c.post("/users/other/delete", data={"csrf": self.csrf}).status_code, 403)
        self.assertEqual(self.c.post("/users/admin/reset", data={"csrf": self.csrf}).status_code, 403)
        self.c.post("/users/gail/delete", data={"csrf": self.csrf})
        self.assertIsNone(self.store.get("gail"))


class TestGuest(Base):
    def test_single_site_guest(self):
        self.add("gail", "guest", "s1", "London Office")
        self.login("gail", "first-pass-1")
        self.assertTrue(self.c.get("/").headers["Location"].endswith("/site/s1"))
        r = self.c.get("/site/s1")
        self.assertIn(b"Visitor Bob", r.data)
        self.assertNotIn(b"Reception iPad", r.data)
        self.assertNotIn(b"Users", r.data)
        self.assertEqual(self.c.get("/site/s1/psk/p1").status_code, 404)
        r = self.c.get("/site/s1/psk/p3")
        self.assertIn(b"GuestKey77", r.data)
        self.assertNotIn(b"Delete this PSK", r.data)
        self.assertEqual(self.c.post("/site/s1/psk/p3/delete",
                                     data={"csrf": self.csrf, "confirm_name": "Visitor Bob"}).status_code, 403)
        form = self.c.get("/site/s1/new").data
        self.assertNotIn(b'name="ssid"', form)
        self.assertIn(b"Corp-PSK", form)
        r = self.c.post("/site/s1/new", data={"csrf": self.csrf, "name": "Visitor Ann", "ssid": "IoT-PSK",
                                              "passphrase": "AnnKey123456", "usage": "multi"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.fake.psks["s1"][-1]["role"], "guest")
        self.assertEqual(self.fake.psks["s1"][-1]["ssid"], "Corp-PSK")   # the assigned SSID wins
        self.assertEqual(self.c.get("/site/s2").status_code, 403)
        self.assertEqual(self.c.get("/org").status_code, 403)
        self.assertEqual(self.c.get("/users").status_code, 403)

    def test_all_sites_guest(self):
        self.add("gus", "guest", "*", "All sites")
        self.login("gus", "first-pass-1")
        r = self.c.get("/")
        self.assertIn(b"Manchester Store", r.data)
        self.assertNotIn(b"Org Level Keys", r.data)
        self.assertEqual(self.c.get("/site/s1/new").status_code, 200)
        r = self.c.get("/site/s2/new")                     # Manchester doesn't broadcast Corp-PSK
        self.assertEqual(r.status_code, 409)
        self.assertIn(b"Corp-PSK", r.data)


class TestSelfService(Base):
    def setUp(self):
        super().setUp()
        self.add("desk", "self_service", "s1", "London Office", ssid="Corp-PSK", key_expiry="1d")
        self.login("desk", "first-pass-1")

    def test_kiosk_flow(self):
        self.assertTrue(self.c.get("/").headers["Location"].endswith("/kiosk"))
        self.assertTrue(self.c.get("/site/s1").headers["Location"].endswith("/kiosk"))
        self.assertTrue(self.c.get("/users").headers["Location"].endswith("/kiosk"))
        r = self.c.get("/kiosk")
        self.assertIn(b"Get Wi-Fi access", r.data)
        self.assertNotIn(b"Sign out</button>", r.data)       # no plain sign-out
        r = self.c.post("/kiosk", data={"csrf": self.csrf, "visitor": "Ann Lee", "company": "Acme", "email": "bad"})
        self.assertIn(b"valid email", r.data)
        for _ in range(2):
            r = self.c.post("/kiosk", data={"csrf": self.csrf, "visitor": "Ann  Lee", "company": "Acme",
                                            "email": "ann@example.com"})
            self.assertIn(b"Finished", r.data)
            self.assertIn(b"<svg", r.data)
        keys = [p for p in self.fake.psks["s1"] if p.get("role") == "self_service"]
        self.assertEqual([k["name"] for k in keys], ["Ann Lee - Acme", "Ann Lee - Acme (2)"])
        self.assertEqual(keys[0]["email"], "ann@example.com")
        self.assertEqual(keys[0]["ssid"], "Corp-PSK")
        self.assertIn("expire_time", keys[0])
        self.assertIn(keys[1]["passphrase"].encode(), r.data)
        self.assertNotEqual(keys[0]["passphrase"], keys[1]["passphrase"])

    def test_signout_needs_password(self):
        r = self.c.post("/logout", data={"csrf": self.csrf})
        self.assertTrue(r.headers["Location"].endswith("/kiosk"))
        self.assertEqual(self.c.get("/kiosk").status_code, 200)
        r = self.c.post("/kiosk/signout", data={"csrf": self.csrf, "password": "wrong"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.c.get("/kiosk").status_code, 200)
        r = self.c.post("/kiosk/signout", data={"csrf": self.csrf, "password": "first-pass-1"})
        self.assertTrue(r.headers["Location"].endswith("/login"))
        self.assertEqual(self.c.get("/kiosk").status_code, 302)

    def test_admins_see_self_service_keys_guests_dont(self):
        self.c.post("/kiosk", data={"csrf": self.csrf, "visitor": "Zed", "company": "Co", "email": "z@co.uk"})
        admin = self.app.test_client()
        self.login(client=admin)
        self.assertIn(b"Zed - Co", admin.get("/site/s1").data)
        self.add("gail", "guest", "s1", "London Office")
        guest = self.app.test_client()
        self.login("gail", "first-pass-1", client=guest)
        self.assertNotIn(b"Zed - Co", guest.get("/site/s1").data)


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
    def test_optional_and_legacy(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            conf = webapp.load_web_conf(d / "web.conf")
            self.assertEqual(conf["port"], 5040)
            (d / "web.conf").write_text("[web]\nport = 5041\n[user philip]\npassword_hash = scrypt:a:b\nconfigs = *\n")
            conf = webapp.load_web_conf(d / "web.conf")
            self.assertEqual((conf["port"], conf["legacy_users"]), (5041, {"philip": "scrypt:a:b"}))


if __name__ == "__main__":
    unittest.main()
