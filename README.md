# Mist MPSK Mobile

A phone-friendly web app for creating **Juniper Mist site PSKs** (multi-PSK keys). It runs on a Raspberry Pi or any small Linux box. You open it on an **iPhone or Android phone** and add it to the home screen, so it looks and behaves like an app, with no App Store involved.

- Sign in, pick an org (if you have more than one), then search for a site.
- See the site's PSKs: name, SSID, VLAN, single-device keys, and expired keys flagged.
- **Create a PSK**: name, SSID, a generated passphrase (tap ↻ for another, or type your own), optional VLAN (a number or a site variable such as `{{guest_vlan}}`), an expiry (never, 1/7/30/90 days, or a date) and any number of devices or the first device only.
- **Show or share it**: a Wi-Fi QR code that guests scan with their camera, a copy button for the password, and a share button that sends a QR image with the Wi-Fi name and password through Messages, WhatsApp, email and so on.
- **Delete a PSK**: you type its name to confirm.
- Logins come with per-user org access and read-only users. Every sign-in, create, view, share and delete goes to an activity log. Passphrases are never logged.

Configs use the same `.ini` format as [mist-mpsk-generator](https://github.com/keeleyp/mist-mpsk-generator), so an existing `mist_mpsk_generator.ini` works without changes.

## Which SSIDs can be used

Site PSKs only work on a WLAN that uses **WPA2/WPA3 Personal (PSK)** with **local multiple passphrases**. The app checks each site's derived WLANs:

| WLAN | In the app |
|---|---|
| PSK, local keys | Offered in the SSID list |
| PSK with dynamic PSK from **Cloud PSKs** (org level) or **RADIUS** | Warning shown, not offered (site PSKs would be ignored) |
| Open, 802.1X, etc. | Not shown |

To limit the app to particular SSIDs, set `[app] ssids = ...` in the config.

## Install on the Pi

```bash
git clone https://github.com/keeleyp/mist-mpsk-mobile.git ~/mist-mpsk-mobile
```

```bash
cd ~/mist-mpsk-mobile && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

1. **Configs.** Copy `mist_mpsk_mobile.ini.example` to one `.ini` per org in this folder (for example `customer-a.ini`) and fill in `cloud`, `api_token` and `org_id`. The token needs write access to the sites.
2. **Logins.** Copy `web.conf.example` to `web.conf`. For each user, run the command below and paste its output into that user's section:

   ```bash
   .venv/bin/python3 app.py --hash-password
   ```

3. **Try it.**

   ```bash
   .venv/bin/python3 app.py
   ```

   Then browse to `http://<pi-address>:5040` from your phone.
4. **Run it as a service.** Edit `deploy/mist-mpsk-mobile.service` so the user and paths match your Pi (`sed -i "s/pi/$USER/g"` also works if your home is `/home/$USER`), then:

   ```bash
   sudo cp deploy/mist-mpsk-mobile.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now mist-mpsk-mobile
   ```

   Logs are in `journalctl -u mist-mpsk-mobile` and `mpsk_activity.log`.

Options: `--read-only` (nobody can create or delete), `--web-conf PATH`, `--config-dir PATH`, `--host`, `--port`.

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

`web.conf`: `[web]` takes `host`, `port`, `session_hours`, `certfile` and `keyfile`. Add one `[user NAME]` section per login, with `password_hash`, `configs` (file names, or `*` for all) and `read_only`.

## Security notes

- API tokens stay on the Pi. Phones never see them.
- Passwords are stored only as salted hashes in `web.conf`. After 5 wrong passwords, that user and address are locked out for 15 minutes.
- Every form has a CSRF token. Pages are sent with `Cache-Control: no-store`, and the service worker never caches pages, because they contain live keys.
- `.secret_key` (created on first run, mode 600) signs the session cookies. Delete it and restart to sign every phone out.
- `*.ini`, `web.conf`, `.secret_key`, `certs/` and logs are all git-ignored.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The tests use a fake Mist API (`tests/fake_mist.py`) and make no network calls. `tools/make_icons.py` redraws the app icons.

## License

MIT
