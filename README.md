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

The application reserves local quota before video, caption, and playlist API
operations. Local counters protect the configured budget, while Google Cloud
Console remains the authoritative source. Counters reset using Pacific Time
calendar days.

Non-empty SRT files up to the configured size limit are uploaded through
`captions.insert`. SRT input must be valid UTF-8 SubRip. The OAuth token includes
both `youtube.upload` and `youtube.force-ssl`, the latter being required for
captions and playlists. Expired tokens are refreshed and persisted; an unusable
token falls back to the interactive desktop-app OAuth flow.

A successful video is never uploaded again just because captions or playlist
insertion failed. Source files stay in place and the next watcher cycle retries
only the missing finalization operation. If a channel's playlist ID is empty, the
uploader creates a playlist and logs the environment variable that must be saved
to avoid creating a duplicate playlist on a later process start.

## Oversized recordings and lossless parts

Splitting is enabled by default:

- YouTube's hard limits are 12 hours and 256 GB, whichever is reached first.
  Parts target 11 hours 45 minutes and 250 GB.
- Rumble's configured hard limit is 15 GB. Parts target 14.5 GB.
- CDA continues to receive the original recording.

YouTube and Rumble use separate plans. For example, a 10-hour, 20 GB recording is
uploaded once to YouTube but split only for Rumble. FFmpeg uses stream copy
(`-c copy`), so video/audio are not re-encoded. Segment boundaries move to valid
keyframes; the publisher reads FFmpeg's actual CSV boundaries instead of assuming
the requested time. Only video, audio, and subtitle streams are mapped to the
Matroska parts; transport metadata such as `timed_id3` is intentionally excluded
because Matroska cannot store it.

Chat captions are split from those actual boundaries. A cue crossing a boundary
is clipped into both neighboring parts, timestamps are reset to zero, UTF-8 and
emoji text are preserved, and indexes are regenerated. Each YouTube part gets its
own matching caption track and playlist entry.

The temporary parts and `manifest.json` are stored beside the source under:

```text
<channel>/_publisher_work/<source-signature>/<platform>/
```

The watcher ignores this directory. SQLite tracks every part separately, so after
a restart only failed or unfinished parts are retried. Before splitting, the
publisher requires free space approximately equal to 105% of the source size.
Every generated part is checked against hard limits using its real byte size and
an individual `ffprobe` duration measurement. If keyframe placement creates an
oversized part, the target duration is reduced and the plan is regenerated up to
`splitting.max_replans`.

The original MKV/SRT/TXT set is moved to `_uploaded` only after all required
platforms and all parts are `SUCCESS` or a legal `SKIPPED`. Work parts are then
removed unless `splitting.keep_parts_after_success` is enabled.

Relevant configuration:

```yaml
paths:
  ffmpeg: ffmpeg
  ffprobe: ffprobe

splitting:
  enabled: true
  work_directory_name: _publisher_work
  youtube_target_duration_hours: 11.75
  youtube_target_size_gb: 250
  rumble_target_size_gb: 14.5
  max_replans: 3
  disk_space_multiplier: 1.05
  keep_parts_after_success: false
```

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
- accepts a blocking CDA consent dialog before selecting the file;
- detects HTTP failures from both upload initialization and resumable transfer;
- aborts and retries when no upload card appears or progress stops changing;
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
confirms ownership and terms, limits titles to a safe 90 characters, enforces the
configured 15 GB file limit, and waits for the server-side upload token after
chunk transfer and merge before submitting the final form.

If a recording is over Rumble's configured 15 GB limit, it is losslessly split
before the browser uploader is called. Old size-limit `FAILED`/`SKIPPED` rows are
reopened automatically when multipart support can process the source. If Rumble
explicitly returns `The video file has no video track`, that recording or part is
marked `SKIPPED` instead of being retried forever. Legal skips count as processed
for file movement after every other enabled platform has reached `SUCCESS` or
`SKIPPED`.

## Debugging and cancellation

Use `--browser-debug` or `-BrowserDebug` to display the browser and collect safe
diagnostics. Screenshots and Playwright traces are written under
`logs/browser_debug`. Traces may contain session details and request URLs; never
publish or commit them.

Ctrl+C is checked during long waits and uploads. If a platform accepted a form but
the final result cannot be confirmed, the status receives `[NO_AUTO_RETRY]` and
requires manual dashboard verification to prevent accidental duplicates.

## YouTube Copyright Guard

Copyright Guard is a separate manually launched process. It is not imported or
started by `main.py`, so a Studio authentication or UI failure cannot stop the
recording publisher. It scans at the configured two-hour interval and remediates
videos that are blocked worldwide or unavailable in Poland (`PL`) or Germany
(`DE`). Restrictions affecting only other countries are accepted.

The supported policy is deliberately narrow:

- audio claims: erase the song first, then mute the complete claimed segment if
  the protected restriction remains after processing;
- visual or audiovisual claims: trim the exact claimed segment when Studio offers
  that operation;
- strikes, takedowns, disputes, and appeals: never automated;
- at most one edit per video while YouTube reports editing in progress.

Studio edits cannot be reverted through the original Studio restore feature.
Before trimming, the guard downloads the owned `Twitch Chat` caption track. Once
the edit finishes it verifies the duration delta, retimes the SRT, updates the
same caption track, and waits for `serving` before marking the video resolved.

### Initial Studio login

Google rejects account sign-in from browsers controlled by automation. Install a
current stable Google Chrome (or select `msedge` in `config.yaml`), close any
previous guard process, and create the dedicated Studio profile:

```powershell
.\start-copyright-guard.ps1 -Login -BrowserDebug
```

Linux equivalent:

```bash
./start-copyright-guard.sh --login --browser-debug
```

`-Login` opens the installed browser directly, without Playwright or remote
debugging. Complete Google login, MFA, or CAPTCHA and wait until YouTube Studio
itself has loaded. Then close that browser window completely. The launcher
asks you to press Enter, waits for Chrome to release the profile, and reopens the
same dedicated profile through Playwright to verify the session. Do not try to
sign in in this second, automation-controlled window.

The profile is stored in `auth/youtube_studio_profile`; its storage-state backup
is `auth/youtube_studio_state.json`. Both are ignored by Git. Never copy either
file to an untrusted machine. If the browser is installed in a non-standard
location, set its absolute executable path in `.env`:

```dotenv
YOUTUBE_STUDIO_BROWSER_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe
```

For Microsoft Edge use `channel: msedge` under `youtube_copyright.browser` and,
only when auto-detection fails, point the variable to `msedge.exe`.

### Burn-in against known videos

Run the three supplied reference videos without the final irreversible click:

```powershell
.\start-copyright-guard.ps1 -Once -DryRun -BrowserDebug `
  -VideoId b7uH35WAR2U,Z__dHxFC0PQ,xWmvEX0oCj4
```

Expected API classification is:

- `b7uH35WAR2U`: global restriction, action required;
- `Z__dHxFC0PQ`: regional result depends on its current `allowed`/`blocked` list;
  action is required when Poland or Germany is unavailable;
- `xWmvEX0oCj4`: no protected restriction, no Studio action.

Inspect `logs/youtube_copyright/<run-id>/<video-id>` after the dry run. It contains
the API decision, parsed claims, screenshots, the action modal, and Playwright
trace. Traces can contain authenticated session details and must not be attached
to public bug reports without review.

Once selectors have been verified on the server, execute one real remediation:

```powershell
.\start-copyright-guard.ps1 -Once -BrowserDebug -VideoId b7uH35WAR2U
```

Start the normal long-running two-hour process in its own console only after the
single-video action has produced the expected Studio result:

```powershell
.\start-copyright-guard.ps1 -BrowserDebug
```

During the initial burn-in `trace_mode: always` and `headless: false` are expected.
After the Studio selectors are confirmed, change them to `on_error` and `true` if
unattended headless operation works with the dedicated profile. If Google expires
the session, affected videos receive `AUTH_REQUIRED`; run `-Login` again.

Only one guard instance can own `data/youtube_copyright_guard.lock`. Ctrl+C uses an
interruptible event and does not wait for Studio processing. An interrupted browser
action is marked uncertain, blocking automatic retries. Playwright cleanup is
skipped on SIGINT, and a five-second watchdog forces exit code 130 if a synchronous
browser call does not unwind; both launchers treat 130 as a user stop and never
restart it. Submitted edits are rechecked in a later cycle. Full architecture and
state details are documented in
[`docs/YOUTUBE_COPYRIGHT_GUARD.md`](docs/YOUTUBE_COPYRIGHT_GUARD.md).

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

## Normalizing recording filenames on Windows

`normalize-recording-names.ps1` removes chat-command suffixes, emoji, diacritics,
mojibake, and punctuation from recording names outside `_uploaded`. It gives the
MKV, optional chat SRT, and metadata TXT one identical cleaned base name. The
default mode is a read-only preview:

```powershell
.\normalize-recording-names.ps1 -RootPath "E:\TwitchRecordings"
```

Test one recording first:

```powershell
.\normalize-recording-names.ps1 `
  -RootPath "E:\TwitchRecordings" `
  -VideoPath "E:\TwitchRecordings\mrozopl\20260714_170854_mrozopl_[Daj Sobie Szansę] Arduino, emulujemy pilot radiowy 📻 !dss.mkv"
```

Stop Twitch Publisher before applying changes, then add `-Apply`. The script also
migrates matching `upload_status.video_path` rows in `data/upload_state.sqlite3`,
preventing successful YouTube or Rumble uploads from being duplicated under the
new filename. File changes are rolled back if SQLite migration fails. Use
`-AllowMissingDatabase` only on an installation that genuinely has no existing
state database.

When CDA is enabled, the publisher also performs a narrower automatic
normalization immediately before creating upload statuses. The default CDA
configuration keeps ordinary BMP Unicode letters and numbers, including Polish
diacritics, while removing emoji, non-BMP characters, variation selectors,
private/control characters, chat commands, and unusual symbols. The MKV,
optional chat SRT, and metadata TXT are renamed as one set. Existing SQLite
statuses are migrated atomically so a partly processed recording is not uploaded
again to a platform that already succeeded.

```yaml
platforms:
  cda:
    normalize_filename: true
    filename_max_stem_length: 140
```

Set `normalize_filename: false` only to disable this CDA compatibility step.

## Tests

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

The suite mocks Google and browser services, but exercises cached-token loading,
token refresh and persistence, missing-scope/refresh-error OAuth fallback,
`captions.insert` request/response handling, caption-only retry, per-part restart
behavior, SRT boundary clipping, manifest reuse, and oversized-part replanning.
It also covers global/PL/DE restriction classification, 50-ID batching, copyright
state transitions, Studio claim parsing, guarded confirmations, single-instance
locking, trim safety limits, caption backup, retiming, update, and final serving
verification.
An additional local verification was performed against a real FFmpeg executable
using a generated Matroska source; the resulting CSV boundaries and manifest
reuse were validated.

## Manual GitHub releases

The `Manual release` GitHub Actions workflow creates a release only when started
from the Actions page. It does not run on pushes or on a schedule.

1. Open **Actions** in the GitHub repository.
2. Select **Manual release**.
3. Select **Run workflow** and enter a new semantic version tag such as `v1.0.0`.
4. Optionally choose draft or prerelease mode.

Before creating the release, the workflow runs the test suite, compiles the Python
sources, and validates both Bash launchers. It then creates a clean source ZIP with
a SHA-256 checksum. Runtime data, credentials, logs, virtual environments, and
recordings are excluded because the archive is built only from Git-tracked files.

The release description contains two parts:

- a commit summary generated from commits since the latest published release;
- GitHub's automatically generated release notes, including categorized pull
  requests, contributors, and a full changelog link when those data are available.

GitHub Copilot release-note generation is not invoked by the workflow. The stable,
repository-native GitHub Release Notes API is used instead. Categories can be
customized in `.github/release.yml` by changing label mappings.

The workflow needs the repository's default `GITHUB_TOKEN` with `contents: write`.
If repository settings restrict this token to read-only access, enable read and
write workflow permissions under **Settings > Actions > General > Workflow
permissions**.
