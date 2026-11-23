# Mist MPSK Mobile — Deployment Guide

Version 1.4.0

This guide installs Mist MPSK Mobile on a **Raspberry Pi** (or any Debian or Ubuntu machine), connects it to one or more Mist orgs, runs it as a service and sets up the first users. Allow about 20 minutes.

---

## 1. What you need

| Item | Notes |
|---|---|
| A Raspberry Pi or small Linux box | Raspberry Pi OS / Debian 12 or 13, or Ubuntu. **Python 3.10+**, which current Pi OS has. 512 MB RAM is plenty |
| A fixed address | A DHCP reservation or static IP, so phones can always find it, e.g. `192.168.1.50` |
| Network access | **Outbound HTTPS (443)** to your Mist API host, e.g. `api.eu.mist.com`. **Inbound TCP 5040** from the phones that will use it |
| A Mist API token per org | It needs **write** access to the sites: an Org Admin token, or Site Admin on every site you'll use. Create it in Mist under *My Account → API Token*, or as an org token |
| The org ID | Shown in Mist under *Organization → Settings* |
| A suitable Wi-Fi network | **Site keys** need a WLAN with **WPA2/WPA3 Personal** and **local multiple passphrases** (not RADIUS, not Cloud PSKs). **Org-level keys** need a WLAN with dynamic PSK from **Cloud PSKs** |

---

## 2. Install

Sign in to the Pi (with SSH, for example) and run:

```bash
sudo apt update && sudo apt install -y git python3-venv
```

```bash
git clone https://github.com/keeleyp/mist-mpsk-mobile.git ~/mist-mpsk-mobile
```

```bash
cd ~/mist-mpsk-mobile && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

To check the install, run the self-tests. They use a fake Mist API and need no network:

```bash
.venv/bin/python3 -m unittest discover -s tests
```

The last line should read `OK`.

---

## 3. Add an org

Each Mist org is one `.ini` file in the app folder. Copy the template and edit it:

```bash
cp mist_mpsk_mobile.ini.example acme.ini && chmod 600 acme.ini && nano acme.ini
```

```ini
[mist]
cloud = api.eu.mist.com          ; or api.mist.com, api.gc3.mist.com, or a short name: eu, gc3 ...
api_token = YOUR_API_TOKEN
org_id = 00000000-0000-0000-0000-000000000000

[psk]
length = 12                      ; generated key length, 8-63

[app]
company = Acme Retail Ltd        ; shown at the top of every page and on the kiosk
;ssids = Corp-PSK, IoT-PSK       ; optional: only offer these SSIDs
```

Notes:

- An existing **mist_mpsk_generator.ini** works as it is. Its extra sections are ignored.
- The file holds your API token, so keep it `chmod 600`. All `*.ini` files are git-ignored.
- To add more orgs, add more `.ini` files. Admins pick between them with **Change org**.

### Behind Zscaler or another proxy

If the Pi reaches the internet through a TLS-inspecting proxy, add this section:

```ini
[network]
ca_bundle = /home/pi/certs/zscaler_root_ca.pem
;https_proxy = http://proxy.example.com:8080
```

---

## 4. First run

```bash
.venv/bin/python3 app.py
```

On first start the app:

- creates **`users.json`** with the user **admin**, password **admin**, which must be changed at first sign-in;
- creates **`.secret_key`**, which signs sign-in sessions so a restart doesn't sign phones out;
- listens on **port 5040** on all addresses.

You should see:

```
Created user admin (admin, password 'admin', must be changed at first sign-in)
Mist MPSK Mobile 1.4.0: http://0.0.0.0:5040  (1 user(s), configs in /home/pi/mist-mpsk-mobile)
```

On your phone, browse to `http://<pi-address>:5040`, sign in as **admin / admin** and **choose a new password straight away**. Then press **Ctrl+C** on the Pi to stop the app, and set it up as a service (next section).

---

## 5. Run as a service

The included systemd unit assumes the user `pi` and the folder `/home/pi/mist-mpsk-mobile`. If your user is different, edit the unit as you copy it:

```bash
sed "s#/home/pi/#$HOME/#g; s#^User=pi#User=$USER#" deploy/mist-mpsk-mobile.service | sudo tee /etc/systemd/system/mist-mpsk-mobile.service
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now mist-mpsk-mobile
```

Check it:

```bash
systemctl status mist-mpsk-mobile
```

The service starts at boot and restarts itself if it stops.

| Task | Command |
|---|---|
| Restart (after editing an `.ini` or `web.conf`) | `sudo systemctl restart mist-mpsk-mobile` |
| Service log | `journalctl -u mist-mpsk-mobile -f` |
| Activity log (sign-ins, keys, users) | `tail -f ~/mist-mpsk-mobile/mpsk_activity.log` |

---

## 6. Set up users

Sign in as **admin** and open **Users → + Add user**. See the User Guide for each screen.

| Role | Typical use |
|---|---|
| Admin | IT team: every org and site, org-level keys, all users |
| Site admin | Site or store manager: all keys at one site, and adds guest and kiosk users there |
| Guest | Reception staff: create visitor keys on one fixed SSID |
| Self service | A reception tablet where visitors create their own key |

Put the app on each phone with **Add to Home Screen** (Safari) or **Install app / Add to Home screen** (Chrome).

### Kiosk tablet checklist

1. Add a **Self service** user for the site, choosing the **SSID** and how long visitor keys last.
2. On the tablet, sign in as that user and add the app to the home screen.
3. Optionally lock the tablet to the app: *Guided Access* on iPad, or *App pinning* on Android.
4. To sign it out, use **Staff: sign out**. It asks for the user's password again.

---

## 7. Settings (optional): `web.conf`

The app works without a `web.conf`. To change a setting:

```bash
cp web.conf.example web.conf && nano web.conf
```

| Setting | Default | Meaning |
|---|---|---|
| `host` | `0.0.0.0` | `127.0.0.1` makes the app reachable from the Pi only |
| `port` | `5040` | TCP port |
| `session_hours` | `12` | How long a phone stays signed in after its last use |
| `certfile`, `keyfile` | — | Turn on HTTPS (see section 8) |

Restart the service after any change.

---

## 8. HTTPS (optional)

The app runs over plain HTTP by default, which is fine on a trusted network. With HTTPS, the **Share** button can send the QR image, Android can install the app properly, and passwords are encrypted on the network.

**Step 1: get a certificate and key** for the Pi's name or IP. You can use Let's Encrypt with a domain you own (trusted by every phone), a certificate from your internal CA, or a self-signed one:

```bash
mkdir -p certs && openssl req -x509 -newkey rsa:2048 -nodes -days 825 -keyout certs/mpsk.key -out certs/mpsk.crt -subj "/CN=MPSK" -addext "subjectAltName=IP:192.168.1.50"
```

**Step 2: add both files** to `web.conf` under `[web]`:

```ini
certfile = certs/mpsk.crt
keyfile = certs/mpsk.key
```

**Step 3: restart the service** and browse to `https://<pi-address>:5040`.

With a self-signed certificate, each phone has to trust it once:

- **iPhone:** send it `certs/mpsk.crt` (by AirDrop or email). Go to **Settings → Profile Downloaded → Install**, then **Settings → General → About → Certificate Trust Settings** and turn on **MPSK**.
- **Android:** go to **Settings → Security → Encryption & credentials → Install a certificate → CA certificate** and choose the `.crt` file.

---

## 9. Updating

Your settings, users, keys and logs are git-ignored, so an update never touches them.

```bash
cd ~/mist-mpsk-mobile && git pull && .venv/bin/pip install -r requirements.txt && sudo systemctl restart mist-mpsk-mobile
```

---

## 10. Backup, recovery and security

**Back up** these files from the app folder: `*.ini`, `users.json`, `web.conf` (if you made one), `.secret_key` and `certs/` (if you use HTTPS).

**Locked out of admin?** On the Pi, run:

```bash
cd ~/mist-mpsk-mobile && .venv/bin/python3 app.py --reset-admin && sudo systemctl restart mist-mpsk-mobile
```

This sets **admin**'s password back to `admin`, and it must be changed at the next sign-in.

**Sign every phone out:** delete `.secret_key` and restart the service.

**Security summary:**

- API tokens stay on the Pi. Phones never see them.
- Passwords are stored only as salted hashes in `users.json` (mode 600). After 5 wrong passwords, that user and address are locked out for 15 minutes.
- Every form is protected against cross-site requests. Pages are never cached, because they contain live keys.
- The activity log never contains passwords or Wi-Fi passphrases. It does contain kiosk visitors' names, companies and emails.
- Don't expose port 5040 directly to the internet. Keep it on the internal network or VPN, and use HTTPS where you can.

---

## 11. Troubleshooting

| Symptom | Check |
|---|---|
| The service won't start | `journalctl -u mist-mpsk-mobile -n 50`. Look for a missing Python package or a `web.conf` error |
| "HTTP 401" from Mist | `api_token` and `cloud` in the `.ini` |
| "HTTP 403" from Mist | The token has no write access to that org or site |
| "HTTP 404" from Mist | `org_id` and `cloud` |
| "TLS certificate check failed" | You're behind a proxy: set `[network] ca_bundle` |
| Phones can't connect to the app | The Pi's firewall or the network allows TCP 5040, and the phone is on the right network or VPN |
| "No SSID at this site can use site PSKs" | The site's WLAN must be WPA2/WPA3 Personal with local multiple passphrases |
| The Org Level Keys page has no **+ New PSK** | The org has no WLAN with dynamic PSK from Cloud PSKs |
