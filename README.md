# Twitch Publisher

A manually operated, long-running Python watcher that publishes completed Twitch
recordings to YouTube, CDA, and Rumble. It stores per-platform state in SQLite and
moves fully processed recording sets into each channel's `_uploaded` directory.

The application never uses cron, systemd timers, or Windows Task Scheduler. Start
and stop it explicitly with the platform launcher.

## Supported environments

- Python 3.11 or newer.
- Windows Server 2019 or newer.
- Ubuntu 22.04 or newer and Debian 12 or newer.
- RHEL 8+ and CentOS Stream 8+ are supported by the installer on a best-effort
  basis. Playwright officially supports current Debian and Ubuntu releases, not
  RHEL/CentOS. Browser automation on RPM distributions must be verified after
  installation.
- FFmpeg/ffprobe available in `PATH`.
- Read, write, create-directory, and move permissions for the recordings root.

## Input layout

```text
<recordings_root>/
  <channel>/
    <timestamp>_<channel>_<title>.mkv
    <timestamp>_<channel>_<title>_chat.srt
    <timestamp>_<channel>_<title>_meta.txt
```

The `_meta.txt` file is the primary completion marker. An MKV is eligible only
when metadata exists, `Ended` is populated, and two non-blocking size samples
confirm that the MKV is stable. Missing or zero-byte SRT files are allowed.

## Windows installation

1. Install Python 3.11+ and FFmpeg, including ffprobe, and add both to `PATH`.
2. Install Firefox and sign in to cda.pl and rumble.com in the Windows account
   that will run the publisher.
3. Open PowerShell in the project directory and run:

```powershell
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
playwright install firefox chromium
Copy-Item .env.example .env
```

4. Edit `.env` and `config.yaml`, then start manually:

```powershell
.\start.ps1
.\start.ps1 -Once
.\start.ps1 -BrowserDebug
```

`start.ps1` activates the existing virtual environment, warns when `.env` is
missing, logs launcher events to `logs/start_ps1.log`, and restarts `main.py`
after unexpected failures. A normal exit, `-Once`, or Ctrl+C does not restart it.

## Linux automatic installation

Run the installer as a normal user with sudo access:

```bash
chmod +x install.sh start.sh
./install.sh --recordings-root /srv/twitch-recordings
```

Useful variants:

```bash
./install.sh --with-dev --recordings-root /mnt/recordings
./install.sh --skip-system
./install.sh --skip-browsers
```

The installer detects `apt`, `dnf`, or `yum`, installs Python 3.11+, FFmpeg,
Firefox, creates `.venv`, installs Python dependencies and Playwright Firefox and
Chromium, and creates a protected `.env` when one does not exist.

Important repository notes:

- Ubuntu 22.04 uses the third-party deadsnakes PPA to obtain Python 3.11.
- RHEL/CentOS uses EPEL and RPM Fusion to obtain required packages such as
  FFmpeg. Review these repository additions before running the installer in a
  restricted production environment.
- Playwright's `install-deps` path is used on Debian/Ubuntu. RPM distributions
  receive an explicit compatibility package set because Playwright does not
  officially support them.
- Interactive browser fallback requires a graphical session or X forwarding.
  Headless browser reuse through saved storage state does not require a visible
  desktop.

Start and stop the Linux process manually:

```bash
./start.sh
./start.sh --once
./start.sh --browser-debug
./start.sh --config config.yaml --restart-delay 15
```

Launcher events are written to `logs/start_sh.log`. SIGINT and SIGTERM are
forwarded to the active Python process and do not trigger an automatic restart.

## Configuration and environment

Copy `.env.example` to `.env` and configure at least:

```dotenv
RECORDINGS_ROOT=/srv/twitch-recordings
YOUTUBE_CLIENT_SECRETS_FILE=auth/credentials.json
FIREFOX_PROFILE_PATH=
RUMBLE_LICENSE_OPTION=6
```

On Windows, `RECORDINGS_ROOT` may use a drive or UNC path such as
`E:\TwitchRecordings` or `\\server\share\TwitchRecordings`. On Linux it must be
an absolute POSIX path. Process environment variables override `.env` values.

The default title template is:

```yaml
metadata:
  title_template: '{clean_title} | {channel} | {date_YYYY-MM-DD}'
```

YouTube titles are limited to 100 characters by shortening only `clean_title`.
CDA and Rumble title/duration limits remain configurable when verified.

## YouTube OAuth, quota, captions, and playlists

In Google Cloud Console:

1. Create or select a project.
2. Enable YouTube Data API v3.
3. Configure the OAuth consent screen.
4. Create an OAuth Client ID of type **Desktop app**.
5. Save the downloaded file as `auth/credentials.json`, or update
   `YOUTUBE_CLIENT_SECRETS_FILE`.

The first upload opens a browser for the supported OAuth flow. The refreshed token
is stored in `auth/youtube_token.json`. YouTube uploads are resumable and retried
for transient HTTP/network failures.

The application reserves local quota before upload and captions insertion. Local
counters protect the configured budget, while Google Cloud Console remains the
authoritative source. Counters reset using Pacific Time calendar days. Videos over
12 hours are skipped only for YouTube.

Non-empty SRT files up to the configured size limit are uploaded through
`captions.insert`. If a channel's playlist ID is empty, the uploader creates a
playlist and logs the environment variable that must be saved to avoid creating a
duplicate playlist on a later process start.

## CDA and Rumble authentication

Authentication is attempted in this order:

1. Saved Playwright `storage_state` JSON.
2. Cookies imported from the configured local Firefox profile.
3. Visible interactive login followed by Enter in the terminal.

Close Firefox before cookie import because its SQLite cookie database may be
locked. After interactive login, storage state is saved for later runs. Storage
state and OAuth files under `auth/` are ignored by Git and must never be committed.

CDA and Rumble currently have no automated SRT field in the supported forms. The
publisher logs the SRT path for manual addition.

## CDA behavior

The CDA uploader:

- removes stale completed/failed cards before selecting another file;
- waits for real transfer completion and logs progress, speed, and panel text;
- fills title, description, tags, terms, ownership, and all content declarations;
- defaults content declarations to No and accepts required terms/ownership;
- clicks the current publication button while retaining support for the older
  button label;
- records SUCCESS only after reading the generated CDA video URL from the matching
  success card;
- treats a confirmed duplicate URL as SUCCESS rather than uploading indefinitely.

Polish strings in `uploaders/cda.py` are intentional selectors for the Polish CDA
interface and must not be translated without updating the target form.

## Rumble behavior

Rumble requires an explicit license selection:

```text
0 = Personal Use
5 = Video Management (exclusive)
6 = Rumble Only (non-exclusive)
7 = Video Management (excluding YouTube)
```

The uploader sets the primary category, attempts a matching game category,
confirms ownership and terms, enforces the configured 15 GB file limit, and waits
for the server-side upload token after chunk transfer and merge before submitting
the final form.

## Debugging and cancellation

Use `--browser-debug` or `-BrowserDebug` to display the browser and collect safe
diagnostics. Screenshots and Playwright traces are written under
`logs/browser_debug`. Traces may contain session details and request URLs; never
publish or commit them.

Ctrl+C is checked during long waits and uploads. If a platform accepted a form but
the final result cannot be confirmed, the status receives `[NO_AUTO_RETRY]` and
requires manual dashboard verification to prevent accidental duplicates.

## Manual cleanup

Cleanup is never called by `main.py`. Preview or execute it manually:

```bash
.venv/bin/python cleanup.py --config config.yaml --dry-run
.venv/bin/python cleanup.py --config config.yaml --no-dry-run --retention-days 30
```

Windows equivalent:

```powershell
.\.venv\Scripts\python.exe cleanup.py --config config.yaml --dry-run
```

Only recording sets inside `<recordings_root>/<channel>/_uploaded` are eligible.
Retention age is calculated from the MKV modification time.

## Tests

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```
