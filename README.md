# Mist MPSK Mobile

A phone-friendly web app for creating **Juniper Mist PSKs** (multi-PSK keys), at site level or org level. It runs on a Raspberry Pi or any small Linux box. You open it on an **iPhone or Android phone** and add it to the home screen, so it looks and behaves like an app, with no App Store involved.

- Sign in, pick an org (if you have more than one), then search for a site.
- See the site's PSKs: name, SSID, VLAN, single-device keys, and expired keys flagged.
- **Create a PSK**: name, SSID, a generated passphrase (tap ↻ for another, or type your own), optional VLAN (a number or a site variable such as `{{guest_vlan}}`), an expiry (never, 1/7/30/90 days, or a date) and any number of devices or the first device only.
- **Show or share it**: a Wi-Fi QR code that guests scan with their camera, a copy button for the password, and a share button that sends a QR image with the Wi-Fi name and password through Messages, WhatsApp, email and so on.
- **Delete a PSK**: you type its name to confirm.
- **Org Level Keys** (admins only) sits at the top of the site list. It works the same way, for org-level keys (Cloud PSKs).
- **App users with roles**: admin, site admin and guest (see below). Admins add users in the app. Every sign-in, create, view, share, delete and user change goes to an activity log. Passwords and passphrases are never logged.

Configs use the same `.ini` format as [mist-mpsk-generator](https://github.com/keeleyp/mist-mpsk-generator), so an existing `mist_mpsk_generator.ini` works without changes.

## Users and roles

On first start the app creates the user **admin** with password **admin**. Whoever signs in first with it has to choose a new password before doing anything else, and so does every new user and every user whose password is reset.

| Role | Scope | Can |
|---|---|---|
| **Admin** | Every org config, every site | Everything: site keys, **Org Level Keys**, and adding, resetting and deleting any user |
| **Site admin** | One org and one site, chosen when the user is added | See, create, share and delete every key at that site. Add, reset and delete **guest** and **self service** users for that site |
| **Guest** | One org, one site or **all sites**, and **one SSID**, all chosen when the user is added | Create keys on their SSID (they never pick one) and see **only keys with the Mist PSK role `guest`**. Every key a guest creates is saved with role `guest`. Guests can't delete keys |
| **Self service** | One org, one site and one SSID, plus how long visitors' keys last (default 1 day) | A **kiosk screen** (see below) |

- **SSIDs for guest and self-service users** come from the chosen site's derived WLANs (`/sites/{id}/wlans/derived`). For an all-sites guest they come from the org's WLAN templates, because Mist has no org-level `derived` call. If a site doesn't broadcast a guest's SSID, the guest gets a clear message there.
- **Users** (in the ⋮ menu and the bar at the bottom of every page) lists users and adds new ones. A first password is generated for each new user, shown once, and must be changed at first sign-in.
- Every page has **Sign out** at the bottom, and **Change password** is in the ⋮ menu.
- Users live in `users.json` (git-ignored, mode 600), which holds salted password hashes only. A user's role and site take effect at their next page load, and a deleted user is signed out at once.
- You can't delete the last admin. If you're locked out, run this on the Pi to set **admin**'s password back to `admin` (it must be changed at the next sign-in):

  ```bash
  .venv/bin/python3 app.py --reset-admin
  ```

### Self-service kiosk

Sign a tablet or phone at reception in as a **self service** user. It then only shows a form asking visitors for their **name, company and email**. When a visitor submits it:

- A key is created on the user's SSID with role `self_service`. It is named "Name - Company" (with "(2)", "(3)" and so on added for repeats), the visitor's email is saved on the Mist key, and it expires as set for the user.
- The screen shows the Wi-Fi QR code, the password and the expiry, with a **Finished** button that clears it for the next visitor. The screen also clears itself after 3 minutes.
- There's no menu or other page to reach. **Staff: sign out** asks for the user's password again before it signs out.
- Admins and the site admin see these keys in the site list. Guests don't. Each visitor sign-up (name, company and email) is written to the activity log.

Upgrading from 1.0: on its first start, 1.1 copies any `[user NAME]` logins from `web.conf` into `users.json` as admins, keeping their passwords, and also creates **admin/admin**.

## Which SSIDs can be used

Site PSKs only work on a WLAN that uses **WPA2/WPA3 Personal (PSK)** with **local multiple passphrases**. The app checks each site's derived WLANs:

| WLAN | In the app |
|---|---|
| PSK, local keys | Offered in the SSID list |
| PSK with dynamic PSK from **Cloud PSKs** (org level) or **RADIUS** | Warning shown, not offered (site PSKs would be ignored) |
| Open, 802.1X, etc. | Not shown |

**Org Level Keys** offers only the org's WLANs that use **WPA2/WPA3 Personal with dynamic PSK from Cloud PSKs**. Those are the WLANs that read org-level keys. If the org has none, the page says so.

To limit the app to particular SSIDs, set `[app] ssids = ...` in the config.

## Install on the Pi

```bash
git clone https://github.com/keeleyp/mist-mpsk-mobile.git ~/mist-mpsk-mobile
```

```bash
cd ~/mist-mpsk-mobile && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

1. **Configs.** Copy `mist_mpsk_mobile.ini.example` to one `.ini` per org in this folder (for example `customer-a.ini`) and fill in `cloud`, `api_token` and `org_id`. The token needs write access to the sites.
2. **Settings (optional).** The defaults are port 5040 on all addresses, sessions of 12 hours, and plain HTTP. To change them, copy `web.conf.example` to `web.conf`.
3. **Try it.**

   ```bash
   .venv/bin/python3 app.py
   ```

   Then browse to `http://<pi-address>:5040` from your phone, sign in as **admin** / **admin**, and choose a new password straight away.
4. **Run it as a service.** Edit `deploy/mist-mpsk-mobile.service` so the user and paths match your Pi (`sed -i "s/pi/$USER/g"` also works if your home is `/home/$USER`), then:

   ```bash
   sudo cp deploy/mist-mpsk-mobile.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now mist-mpsk-mobile
   ```

   Logs are in `journalctl -u mist-mpsk-mobile` and `mpsk_activity.log`.

Options: `--reset-admin`, `--users PATH`, `--web-conf PATH`, `--config-dir PATH`, `--host`, `--port`.

## Put it on the phone

1. Open `http(s)://<pi-address>:5040` in **Safari** (iPhone) or **Chrome** (Android) and sign in.
2. **iPhone:** tap Share, then **Add to Home Screen**.
   **Android:** tap the ⋮ menu, then **Add to Home screen** or **Install app**.
3. Launch it from the new **MPSK** icon. It opens full screen with no browser bars, and you stay signed in for `session_hours` (12 by default) after you last used it.

The phone has to reach the Pi: same Wi-Fi, a VPN, or a port you publish yourself. **Don't** expose the app straight to the internet without HTTPS and a reverse proxy you trust.

## HTTPS (recommended)

Plain HTTP works, and the app adjusts to it:

| Feature | HTTP | HTTPS |
|---|---|---|
| Create, view, delete PSKs, QR code | ✅ | ✅ |
| Home-screen icon, full-screen launch on iPhone | ✅ | ✅ |
| **Share** button (QR image to Messages, WhatsApp…) | Falls back to "Copy Wi-Fi details" | ✅ |
| Android "Install app" (true app window) | Shortcut opens in a browser tab | ✅ |
| Offline page when the Pi can't be reached | — | ✅ |
| Password and session cookie encrypted on the network | ❌ | ✅ |

To use your own certificate (for example from an internal CA, or one made with [mkcert](https://github.com/FiloSottile/mkcert)), set `certfile` and `keyfile` in `[web]`. To make a self-signed one on the Pi, replace `192.168.1.50` and `mpsk.local` below with the Pi's address and name:

```bash
mkdir -p certs && openssl req -x509 -newkey rsa:2048 -nodes -days 825 -keyout certs/mpsk.key -out certs/mpsk.crt -subj "/CN=MPSK" -addext "subjectAltName=IP:192.168.1.50,DNS:mpsk.local"
```

Phones only trust a self-signed certificate after you install it:

- **iPhone:** AirDrop or email `certs/mpsk.crt` to the phone, then go to **Settings → Profile Downloaded → Install**. Next, go to **Settings → General → About → Certificate Trust Settings** and turn on full trust for **MPSK**.
- **Android:** go to **Settings → Security → More security settings → Encryption & credentials → Install a certificate → CA certificate** and choose the `.crt` file. Menu names vary by phone maker.

## Config reference

`*.ini`, one per org:

| Section | Key | Meaning |
|---|---|---|
| `[mist]` | `cloud` | API host (`api.mist.com`, `api.eu.mist.com`, `api.gc3.mist.com` …) or short name (`eu`, `gc3`) |
| | `api_token` | Mist API token with write access to the sites |
| | `org_id` | Org ID |
| `[psk]` | `allowed_characters` | Characters generated keys use (default leaves out look-alikes and QR-escaped characters) |
| | `length` | Generated key length, 8–63 (default 12) |
| `[app]` | `name` | Label shown in the app (default: the org name) |
| | `ssids` | Comma-separated SSIDs to offer (default: all eligible) |
| `[network]` | `ca_bundle`, `verify_ssl`, `http_proxy`, `https_proxy` | Only needed behind Zscaler or another TLS-inspecting proxy |

`web.conf` (optional): `[web]` takes `host`, `port`, `session_hours`, `certfile` and `keyfile`. Users are managed in the app, not here.

## Security notes

- API tokens stay on the Pi. Phones never see them.
- Passwords are stored only as salted hashes in `users.json`. The default admin/admin must be changed at first sign-in. After 5 wrong passwords, that user and address are locked out for 15 minutes.
- Every form has a CSRF token. Pages are sent with `Cache-Control: no-store`, and the service worker never caches pages, because they contain live keys.
- `.secret_key` (created on first run, mode 600) signs the session cookies. Delete it and restart to sign every phone out.
- `*.ini`, `web.conf`, `users.json`, `.secret_key`, `certs/` and logs are all git-ignored.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The tests use a fake Mist API (`tests/fake_mist.py`) and make no network calls. `tools/make_icons.py` redraws the app icons.

## License

MIT
