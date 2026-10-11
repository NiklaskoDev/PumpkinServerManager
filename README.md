# Pumpkin Server Manager

A web dashboard in a single Docker container that **creates, runs, backs up and automatically updates
[Pumpkin](https://pumpkinmc.org) Minecraft servers** - built to be added to Unraid.

Pumpkin is a Minecraft server written in Rust. This manager downloads it from the official GitHub releases, sets up
as many servers as you like (each with its own port, world and settings), keeps them running, checks for new
Pumpkin versions on a schedule and **never applies an update without first making a verified backup**.

![The dashboard with three servers](docs/dashboard.jpg)

## Features

| | |
|---|---|
| **One-click servers** | Pick a name, a Pumpkin channel and a few settings; the manager downloads the binary, writes `pumpkin.toml`, assigns ports and starts it. |
| **Dashboard** | Live status, players, uptime, CPU and memory per server; live console with colours, command history and filter; start/stop/restart; file browser and editor with upload/download; player lists (whitelist, operators with individual levels, bans). |
| **Automatic updates** | Checks GitHub for new Pumpkin builds (stable releases, plus the rolling *nightly* and *canary* builds), per server: install automatically, notify only, or off. Waits until nobody is online. |
| **Safe by construction** | Download and checksum first, stop gracefully, **verified backup**, switch version, start, watch for crashes. If anything fails the version *and the world* roll back automatically. |
| **Backups** | Daily/interval schedule, retention, hot backups without downtime (or stop-copy-start), download/upload, protect, one-click restore with an automatic safety backup. |
| **Accounts for friends** | Give friends their own sign-in with a role per server (player, moderator or admin). They only see the servers you assign, start with a temporary password and must choose their own. See [Accounts for friends](#accounts-for-friends). |
| **Notifications** | Optional Discord/Slack/generic webhook for updates, failed backups and crashes. |
| **Unraid-ready** | PUID/PGID, template XML, graceful shutdown on array stop, separate backup path. No Docker socket needed, nothing runs privileged. |

## Contents

- [Install on Unraid](#install-on-unraid) · [Docker Compose](#docker-compose) · [First steps](#first-steps)
- [Ports and networking](#ports-and-networking) · [Configuration](#configuration)
- [How updates work](#how-updates-work) · [How backups work](#how-backups-work) · [Accounts for friends](#accounts-for-friends)
- [Security](#security) · [Where things are stored](#where-things-are-stored) · [Troubleshooting](#troubleshooting)
- [Pumpkin notes](#notes-about-pumpkin) · [Development](#development)

---

## Install on Unraid

Unraid needs the container image from somewhere. Both ways below end with the same template step. **A** works
immediately from the files in this repository; **B** sets up automatic builds on GitHub.

### A. Build the image on the Unraid server (fastest, nothing to set up)

Open the Unraid terminal (top-right `>_` icon) and run:

```bash
mkdir -p /mnt/user/appdata/pumpkin-manager-src && cd /mnt/user/appdata/pumpkin-manager-src
rm -rf PumpkinServerManager-main
wget -qO src.zip https://github.com/NiklaskoDev/PumpkinServerManager/archive/refs/heads/main.zip
unzip -q src.zip && cd PumpkinServerManager-main
docker build -t pumpkin-manager:local .
```

This takes a minute or two. Use the repository name `pumpkin-manager:local` in the template (see below). Unraid
cannot check a locally built image for updates; run the commands again when this project changes. (Pumpkin updates
are handled inside the dashboard and never need a new image.)

### B. Let GitHub build and publish the image (automatic)

1. **Get every file into the repository, including the hidden ones.** Dragging a folder into GitHub's web uploader
   *silently skips* names that start with a dot, which loses `.github/workflows/ci.yml` (the workflow that builds the
   image), `.gitignore`, `.dockerignore` and `.gitattributes`. Use git instead:

   ```bash
   # new, empty repository:
   git init -b main && git add -A && git commit -m "Pumpkin Server Manager"
   git remote add origin https://github.com/<you>/<repository>.git && git push -u origin main

   # repository that already has files from a web upload (keeps its history, adds what is missing):
   git init -b main && git remote add origin https://github.com/<you>/<repository>.git
   git fetch origin && git reset origin/main
   git add -A && git commit -m "Add the hidden files (CI workflow and friends)" && git push -u origin main
   ```
2. Open the repository's **Actions** tab. The *CI* run builds the image, starts it, signs in over HTTP and then
   publishes a multi-arch image (amd64 + arm64) to `ghcr.io/<you>/pumpkin-manager:latest`. The first run takes
   about ten minutes. (The unit tests run alongside; a red test job is worth a look but does not block the image.)
3. **Check that the package is public.** A package published from a public repository normally is. If Unraid
   still fails with `Head "https://ghcr.io/v2/.../manifests/latest": denied`, change it: GitHub → your profile →
   **Packages** → `pumpkin-manager` → *Package settings* → *Danger Zone* → **Change visibility** → Public.
   (Or keep it private and run `docker login ghcr.io` on the Unraid terminal with a personal access token that has
   `read:packages`.)

### Add the container in Unraid

1. Put the template on the flash drive (Unraid terminal):
   ```bash
   wget -qO /boot/config/plugins/dockerMan/templates-user/my-PumpkinManager.xml \
     https://raw.githubusercontent.com/NiklaskoDev/PumpkinServerManager/main/unraid/pumpkin-manager.xml
   ```
   (or copy `unraid/pumpkin-manager.xml` there by hand; from Windows the folder is
   `\\tower\flash\config\plugins\dockerMan\templates-user\`).
2. **Docker** → **Add Container** → *Template*: pick **my-PumpkinManager** from the *User templates* group. This is
   what fills in the ports, folders and variables: creating the container by typing only a repository name gives you a
   container with no ports and no persistent storage.
3. Using route A? Change **Repository** to `pumpkin-manager:local`. Using route B, leave it as is.
4. Review the settings below and click **Apply**.

### Settings to review in the template

| Setting | What to do |
|---|---|
| **AppData** `/data` | Servers, downloaded Pumpkin binaries and the settings database. A path on your cache pool is best. |
| **Backups** `/backups` | Put this on a **different disk** than AppData (for example an array share) so one failed drive cannot take both. |
| **Game ports** | Every server gets the next free port from this range. Keep host and container side identical, and equal to the *Port range* variables. Add more ports if you want more servers. |
| **Admin password** | Initial password for the user `admin` (min. 8 characters). Leave empty to have one generated; it is printed once in the container log. |
| **Time zone** | Used for "daily at 04:00" backups. |

Apply, then click the container's **WebUI** entry (port 8420). Port forwarding for friends: forward the **TCP**
port of each server from your router to the Unraid IP.

> Prefer the command line? This is equivalent:
> ```bash
> docker run -d --name pumpkin-manager --restart unless-stopped --stop-timeout 120 \
>   -p 8420:8420 -p 25565-25575:25565-25575/tcp -p 19132-19142:19132-19142/udp \
>   -e PUID=99 -e PGID=100 -e TZ=Europe/Berlin -e BACKUP_DIR=/backups -e ADMIN_PASSWORD='choose-a-password' \
>   -v /mnt/user/appdata/pumpkin-manager:/data -v /mnt/user/backups/pumpkin-manager:/backups \
>   ghcr.io/niklaskodev/pumpkin-manager:latest
> ```

## Docker Compose

```bash
docker compose up -d --build        # builds from this folder
docker compose logs pumpkin-manager # first start: shows the generated admin password if you did not set one
```

See [`docker-compose.yml`](docker-compose.yml) for the full example.

## First steps

1. Open `http://<server-ip>:8420` and sign in as `admin`.
2. **New server** → give it a name → *Create server*. The first creation downloads Pumpkin (about 120 MB); after
   that servers are created in seconds.
3. The card shows the address players use (`host:port`) with a copy button. In Minecraft: *Multiplayer → Add server*.
4. Open the server for the console, settings, files, players, backups and version tabs.

---

## Ports and networking

* **Dashboard**: TCP 8420 (`WEB_PORT`).
* **Java game ports**: the manager hands out one TCP port per server from `PORT_RANGE` (default `25565-25575`,
  so up to 11 servers). The same range is mapped 1:1 by the container, because Docker publishes exactly the ports
  you list.
* **Bedrock** (optional, experimental in Pumpkin): each Bedrock-enabled server needs **two** UDP ports from
  `BEDROCK_PORT_RANGE` (`19132-19142` = up to five servers). Pumpkin also tries a fixed UDP port 7551 for LAN
  discovery; only the first Bedrock server gets it and the others log a harmless warning.
* **Query** (optional): UDP on the server's Java port.
* **Management API ports** (`MGMT_PORT_RANGE`, default `29000-29999`) are used only on `127.0.0.1` inside the
  container and are never published.
* **Host networking** also works (`--network host`): no port mapping needed, but ports must be free on the host.
  The manager checks that a port is free before assigning it.
* The dashboard warns on a server whose port lies outside the published range.

## Configuration

Everything has a sensible default; the template sets the ones you usually want.

| Variable | Default | Purpose |
|---|---|---|
| `PUID` / `PGID` | `99` / `100` | User and group the manager and all servers run as (Unraid's *nobody:users*). Files stay accessible through your shares. |
| `UMASK` | `022` | File creation mask (`002` lets your group edit server files). |
| `TZ` | `UTC` | Time zone for schedules and timestamps. |
| `ADMIN_USER` / `ADMIN_PASSWORD` | `admin` / generated | Account created on the very first start. Change the password in *Settings* afterwards. |
| `DATA_DIR` | `/data` | Servers, binaries, logs and the database. |
| `BACKUP_DIR` | `$DATA_DIR/backups` | Backup folder (map it to a separate disk). |
| `PORT_RANGE` | `25565-25575` | Java ports available to servers. |
| `BEDROCK_PORT_RANGE` | `19132-19142` | UDP ports for Bedrock-enabled servers (two per server). |
| `MGMT_PORT_RANGE` | `29000-29999` | Loopback-only ports for the management API. Must not overlap `PORT_RANGE`. |
| `WEB_PORT` / `WEB_HOST` | `8420` / `0.0.0.0` | Where the dashboard listens. |
| `GITHUB_TOKEN` | - | Optional. Only needed if GitHub's anonymous rate limit (60 requests/hour) is hit. No scopes required. |
| `PUBLIC_ADDRESS` | - | Host name shown to players (for example a DynDNS name) and used in the sign-in address handed out with new friend accounts. Defaults to the address in your browser. Can also be set under *Settings*, which takes precedence. |
| `UPDATE_CHECK_MINUTES` | `360` | Initial check interval; changeable in the dashboard. |
| `SESSION_HOURS` | `168` | Sign-in lifetime. |
| `MAX_UPLOAD_MB` | `4096` | Largest file the file manager accepts. |
| `SHUTDOWN_TIMEOUT` | `90` | Seconds to wait for servers when the container stops. |
| `COOKIE_SECURE` | `auto` | `auto`, `always` or `never` for the session cookie's *Secure* flag. |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Trusted reverse-proxy addresses (see [Security](#security)). |
| `PUMPKIN_REPO` | `Pumpkin-MC/Pumpkin` | GitHub repository releases are taken from. |
| `LOG_LEVEL` | `INFO` | Manager log verbosity. |

---

## How updates work

**Channels.** Each server follows one channel:

* **Stable**: tagged releases such as `0.2.0+26.3-26.51` (Pumpkin version + Minecraft version). Recommended.
* **Nightly** and **Canary**: *rolling* prereleases. Their tag never changes - the binary behind it is replaced.
  The manager therefore identifies builds by the **SHA-256 digest GitHub reports**, not by the tag, and keeps the
  previous binary on disk (GitHub no longer serves it once it was replaced) so a rollback is always possible.

**Checks.** Every `UPDATE_CHECK_MINUTES` (and on demand with *Check for updates*) the manager asks GitHub which
build each channel offers. Requests are conditional (ETag), so most checks cost nothing against the rate limit.

**Per-server policy** (Version tab): *install automatically*, *notify only*, or *off*. A server can also be
**pinned** to one release. Automatic installs wait until nobody is online (up to a configurable number of hours;
after that players get an in-game warning first). Switching a server to another *channel* is never automatic.

**The update procedure** - the order is the safety guarantee:

1. Download the new binary and verify its SHA-256 - **while the server keeps running** (no downtime for slow downloads).
2. Warn online players, then stop the server gracefully (the world is saved).
3. Create a **verified backup** (full archive read-back plus checksum). **If this fails the update is aborted**,
   nothing is changed and the server is started again on the old version.
4. Switch to the new binary and start the server.
5. Wait for it to accept connections and watch it for 15 seconds.
6. If it fails to start or crashes: stop it, **restore the world from the backup taken in step 3**, switch back to the
   old binary and start that. The failed build is remembered and not retried; a newer build is.

Two failure modes happen before anything is stopped, and the server keeps running through both: a download that is
**unusable** (not a Linux executable, or the wrong checksum) is refused and that exact build is skipped from then on,
while a **temporary** problem (network, disk space) is retried automatically with a growing delay: 30 minutes, then
60, 120, ... up to 12 hours between attempts, starting over after the next success.

Every step is visible in the job log, in *Activity* and (optionally) as a webhook message. Afterwards the **Version
tab** keeps a history and a **Roll back** button (version only, or version *and* the pre-update world).

What this does *not* protect against: a bug in a new version that only shows up hours later, or world damage that
you notice after playing for a while. That is what the pre-update backup and the Roll back button are for.

## How backups work

* **Schedule**: daily at a time you choose, or every N hours, per server. **Retention**: keep the newest N;
  backups you **protect**, and the one belonging to your latest update, are never deleted automatically.
* **Hot backups** (default) don't stop the server: the manager flushes the world (`save-all flush`), pauses
  autosaving (`save-off`), copies the files, then resumes (`save-on`, always, even on errors). If the management API
  is available it is used for the flush. This is "crash-consistent at worst". Choose **cold** in the Backups tab to
  stop the server, copy and restart for a perfect copy. Backups before updates/restores/deletion are always cold.
* **Format**: `.tar.gz` with a small manifest. Each backup is read back and checksum-verified before it counts.
* **Restore**: stops the server, makes a *safety backup* of the current state, extracts the archive into a temporary
  folder and only then swaps it in, so a failed restore never damages what you have. Network settings (ports,
  management secret) stay the manager's.
* **Download / upload**: move backups to another machine or restore an archive from elsewhere.
* A backup needs free space about the size of the world; the manager checks first and says so if there is not enough.

## Accounts for friends

The account created on the first start is the **administrator**: it can do everything, including creating and deleting
servers, changing the global settings and managing accounts. Under **Accounts** the administrator can give friends their
own sign-in. A friend **only sees the servers assigned to them**, and what they may do there depends on the role they
have *on that server* (a friend can be an Admin on one server and only a Player on another):

| | Player | Moderator | Admin |
|---|:---:|:---:|:---:|
| See the server's status: running or not, who is online, CPU, memory, how to connect | yes | yes | yes |
| Start the server | | yes | yes |
| Read the console (live output) | | yes | yes |
| Kick, ban and pardon players and IPs, manage the whitelist | | yes | yes |
| Stop, restart or kill the server | | | yes |
| Type console commands | | | yes |
| Make or remove operators and set their level | | | yes |
| Files, settings, backups, versions and updates | | | yes |
| **Delete the server**, create servers, global settings, accounts | | | |

The last row stays with the administrator only.

**Temporary passwords.** Creating an account generates a temporary password and shows it **once** (the manager only keeps a
hash, so it cannot show it again; if it gets lost, use *New password*). Hand it over, together with the address of the
dashboard. When the friend signs in with it they see nothing but a *Choose a new password* screen; no other part of the
dashboard or API works until they have chosen their own (at least 8 characters, different from the temporary one, not
their username). *New password* in the accounts list gives an account a fresh temporary password and signs it out
everywhere; deleting an account ends its sessions at once. A friend's password can also be reset from the container with
`python -m pumpkin_manager reset-password --user <name>`, which makes it temporary as well.

The sign-in address shown with a new temporary password is the address you are using, with the host replaced by your public address (`PUBLIC_ADDRESS` or *Settings → Public address*), so friends get a name that works from outside. Friends outside your home network need a way to reach the dashboard: use a
VPN or an HTTPS reverse proxy as described under [Security](#security), not a plain port forward of the dashboard port.

What an account may do is enforced by the server on every request, and the live updates pushed to the browser are
filtered the same way, so a friend's browser never even receives events about other servers. A server the account has
no role on answers exactly like one that does not exist. Role changes take effect on the next request.

> **Give the Admin role only to people you trust.** Admins can edit the server's files, upload things and run console
> commands, and all servers run inside this one container under the same user: Pumpkin plugins or other files an admin
> adds run with the container's access to every server's data and to the manager. Moderators and players cannot do any of that.
## Security

* Sign-in with a password (scrypt-hashed), server-side sessions in `HttpOnly`, `SameSite=Strict` cookies,
  a CSRF token on every state-changing request, rate-limited login attempts and a strict Content-Security-Policy.
  The WebSocket verifies the browser's `Origin`.
* Browsers share cookies between ports of the same host name, so other web apps on your Unraid host *could*
  see (not forge) this one's cookie; the cookie is therefore named uniquely and requests need the CSRF header.
* The manager needs **no Docker socket** and no privileges; servers run as `PUID:PGID` with a minimal environment
  (your `ADMIN_PASSWORD` and `GITHUB_TOKEN` are not passed on to Minecraft processes).
* The management API of each server listens on loopback only and uses a per-server random secret.
* **Do not expose the dashboard directly to the internet.** The dashboard can run console commands and edit files.
  If you need remote access, put it behind a reverse proxy with HTTPS (and set `FORWARDED_ALLOW_IPS` to the proxy's
  address) or use a VPN such as WireGuard.
* Forgot the password? In the container console (Unraid: container icon → *Console*):
  `python -m pumpkin_manager reset-password`. It signs every session out.

## Where things are stored

```
/data
├── manager.db                    settings, users, server records, history (SQLite)
├── servers/<id>/                 one folder per server: pumpkin.toml, world/, data/ (whitelist, ops and ban lists), ...
├── binaries/<build>/pumpkin      downloaded Pumpkin binaries, shared by all servers
└── logs/<id>/console.log         console output of the last run (+ console.prev.log)
/backups/<id>/<date>_<kind>.tar.gz
```

Editing `pumpkin.toml`: the settings form, the *Advanced* editor and the file manager all validate the file before
saving. This matters because **Pumpkin silently falls back to default settings when it cannot read its config**.
A few keys (ports, the management secret, console mode) belong to the manager and are re-applied before each start.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `docker: ... ghcr.io/v2/.../manifests/latest: denied` | GHCR answers *denied* both when the image does not exist yet and when it is private. Check the repository's **Actions** tab for a green *CI* run (no run at all usually means `.github/workflows/ci.yml` never reached GitHub, see *Install* route B), then check that the package is public. Or skip GHCR: use route A. |
| *"Port 25565 is already in use by another program"* | Another container (or the host) uses it. Choose another port in the server's settings, or remove the other service. |
| Friends cannot connect | Forward the server's **TCP** port on your router to the Unraid IP. The card on the dashboard shows the exact port and warns when it is not published by the container. |
| *"No free port left in 25565-25575"* | Widen `PORT_RANGE` **and** the template's port mapping (they must match). |
| Update check says *rate limit* | Add a `GITHUB_TOKEN` (Settings or environment variable). |
| Server stops right after starting | Open the console tab for the output. After editing `pumpkin.toml` by hand, use the Advanced editor, which validates it. |
| Adding a player to the whitelist, operators or bans changes nothing | Those buttons type the command into the running server's console, and the Players tab shows what the server then wrote to its `data/` folder. The dashboard tells you when a list did not change; the console tab has the server's answer (for example *No player was found* when an account name does not exist and online mode is on). The server must be running. |
| Operators have too few (or too many) rights | New servers give operators **level 1** (moderator: can build inside the spawn protection, almost no commands); levels work as in vanilla (2 game master, 3 admin, 4 owner). Set the default for future operators under *Settings → Operator permission level*, and a single operator's level in the *Players* tab. Pumpkin reads levels only at startup, so changing one on a **running** server restarts it (the dashboard asks first); on a stopped server nothing restarts. Servers created before this default keep their old setting (4) until you change it, and so do operators who already exist. |
| The difficulty is not the one I chose | Pumpkin ignores `default_difficulty` in `pumpkin.toml`, so the manager sets it with the `/difficulty` command when a server starts for the first time and whenever you change it in *Settings* (a running server switches at once). A difficulty an operator sets in-game stays until you change the setting here. |
| The *Management API* shows "not available" | Normal for stable Pumpkin 0.2.0, which does not have it yet. The manager then uses console commands and server pings. |
| A friend forgot their password, or cannot sign in | Open *Accounts* and use *New password* for them: they get a new temporary password and choose their own again. After a few wrong attempts a name is locked out for a few minutes. |
| A friend sees "No servers yet" or is missing a server | Servers have to be assigned per account: *Accounts → Access*. The change shows up in their browser without a reload. |
| Backups fill the disk | Lower *Keep the newest*, or put `/backups` on a bigger share. |
| Dashboard unreachable after Unraid reboots | Check that the container is set to autostart; servers come back in the state they were in. |

## Notes about Pumpkin

Pumpkin is in **early development**; its own site warns that worlds can break between versions and that config keys
change. This manager reduces the blast radius (verified backups, rollback, a "stay on this version" pin) but cannot
make an unfinished server stable. Switching between channels, or to an older release, is the riskiest thing you
can do - the dashboard asks for confirmation and backs up first.

Pumpkin's own telemetry heartbeat (to `market.pumpkinmc.org`) is **off for servers created by this manager** unless you
enable it in *Settings → Defaults* or per server.

Three behaviours of the current Pumpkin builds that the manager works around (all found by running the real server):
the whitelist, operator and ban lists are kept in a `data/` folder inside the server folder (not next to `pumpkin.toml`,
as in vanilla Minecraft); the `default_difficulty` setting is not applied to new worlds, which always start on Normal;
and `/op` always gives the level from the server's `op_permission_level` setting while `data/ops.json` is read only at
startup, which is why a per-operator level needs a restart.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt      # Windows: .venv\Scripts\...
.venv/bin/pytest                                  # archives, updates, rollback, API, security, ...
.venv/bin/python scripts/dev_server.py            # dashboard on :8420 with a fake GitHub + fake Pumpkin (admin / devpassword)
.venv/bin/python scripts/real_binary_check.py /path/to/pumpkin   # end-to-end check against a REAL Pumpkin binary
```

* `scripts/real_binary_check.py` drives the manager's own code against a real Pumpkin executable: create, start,
  ping, console, whitelist/operator/ban lists, difficulty, hot backup, settings change, a real update, a download
  that is not an executable (refused up front), a build that cannot start (rolled back), three servers side by side
  (one with Bedrock) and graceful shutdown. It is part of the image, so you can validate
  the exact Linux build on your own server in about 30 seconds:
  `docker exec -it pumpkin-manager python /app/scripts/real_binary_check.py --download`
  (it downloads the official release to a temporary folder, tests it on loopback ports and cleans up; it does not
  touch your servers).
* Layout: `pumpkin_manager/` (backend: `process.py` supervision, `updater.py` the update procedure,
  `backups.py`, `releases.py` GitHub, `pumpkin_config.py` TOML handling, `api/` routers, `static/` the dashboard -
  plain ES modules, no build step), `tests/` (including `fake_pumpkin.py`, a stand-in server), `docker/`, `unraid/`.
* The frontend has no dependencies and never uses inline scripts or styles, so the strict CSP stays strict.
