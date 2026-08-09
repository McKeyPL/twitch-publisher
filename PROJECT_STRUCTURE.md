# Project structure

```text
twitch-publisher/
|-- .github/
|   |-- release.yml             # generated release-note categories
|   `-- workflows/
|       `-- release.yml         # manual tested release and source ZIP
|-- auth/
|   |-- __init__.py
|   `-- browser_session.py      # storage state -> Firefox -> manual login
|-- data/                       # runtime SQLite data, ignored by Git
|-- logs/                       # runtime logs and traces, ignored by Git
|-- youtube_copyright/          # standalone restriction monitor and Studio automation
|   |-- api_client.py           # uploads inventory and 50-ID videos.list batches
|   |-- detector.py             # worldwide/PL/DE region classification
|   |-- state.py                # claim/action/run/caption audit tables
|   |-- browser_session.py      # dedicated persistent Chromium profile
|   |-- studio_parser.py        # resilient claim extraction
|   |-- policy.py               # erase -> mute -> trim decision rules
|   |-- studio_executor.py      # guarded irreversible confirmations
|   |-- captions.py             # backup, retiming, captions.update
|   `-- service.py              # independent two-hour cycle
|-- uploaders/
|   |-- __init__.py
|   |-- base.py                 # UploadResult/BaseUploader contract and retry
|   |-- youtube.py              # OAuth2, resumable upload, SRT, playlists
|   |-- browser_form.py         # shared Playwright form operations
|   |-- cda.py                  # CDA form uploader
|   `-- rumble.py               # 15 GB, chunks, license, confirmations, URL
|-- tests/                      # offline unit and integration tests
|-- .env.example
|-- .gitignore
|-- config.yaml
|-- config.py                   # YAML, dotenv expansion, validation
|-- watcher.py                  # one non-blocking scan cycle
|-- meta_parser.py              # multiline metadata parser
|-- title_cleaner.py            # title normalization and limits
|-- recording_name_normalizer.py # legacy CDA-safe MKV/SRT/TXT renaming
|-- duration_check.py           # file stability and ffprobe
|-- srt_splitter.py             # UTF-8 SRT parsing and boundary-aligned parts
|-- media_splitter.py           # lossless FFmpeg parts, limits, manifest/replan
|-- state.py                    # SQLite/WAL, parent/part status, quota
|-- mover.py                    # safe movement into _uploaded
|-- cleanup.py                  # separate manual dry-run-first CLI
|-- normalize-recording-names.ps1 # safe filename cleanup with SQLite migration
|-- main.py                     # application orchestration
|-- copyright_guard.py          # standalone copyright CLI
|-- start.ps1                   # manual Windows launcher and restart loop
|-- start.sh                    # manual Linux launcher and restart loop
|-- start-copyright-guard.ps1   # independent Windows copyright launcher
|-- start-copyright-guard.sh    # independent Linux copyright launcher
|-- install.sh                  # Debian/Ubuntu/RHEL/CentOS installer
|-- requirements.txt
|-- requirements-dev.txt
`-- README.md
```

## Module boundaries

- `watcher.py` scans once using a caller-owned tracker. It never sleeps and
  returns every readiness status.
- `meta_parser.py` owns metadata format and domain validation.
- `duration_check.py` qualifies completed recordings and reads container duration
  through `ffprobe`.
- `srt_splitter.py` clips chat cues to actual FFmpeg segment boundaries and
  resets each part to a zero-based timeline.
- `media_splitter.py` owns lossless stream-copy segmentation, disk preflight,
  manifest reuse, hard-limit verification, replanning, and cancellation.
- `recording_name_normalizer.py` applies the legacy CDA filename profile and
  atomically migrates existing SQLite statuses after renaming a recording set.
- `state.py` is the only module that writes parent/part upload status and local
  quota usage.
- `main.py` composes dependencies, retains the tracker between cycles, and
  isolates exceptions per recording/platform/part. YouTube and Rumble use
  independent split plans.
- `youtube_copyright/` shares the OAuth token, quota ledger, and WAL database but
  never runs inside `main.py`. It accepts worldwide and PL/DE restrictions only,
  keeps irreversible Studio actions in an append-only audit trail, and closes the
  browser instead of waiting for hours while YouTube processes an edit.
- `uploaders/rumble.py` does not treat the visible second step as transfer
  completion. It waits for the `#video[]` token set after all chunks are uploaded
  and merged.
- Browser uploaders save traces only after failures and check cancellation at
  intervals no longer than one second during long waits.
- `mover.py` operates only after every required platform reaches a terminal
  successful status.
- `cleanup.py` is an independent, manually invoked command with dry-run enabled by
  default. `main.py` never calls it.
- `start.ps1` and `start.sh` are manual launchers. They restart only after a
  non-zero unexpected process exit.
- `install.sh` installs system and Python dependencies but never enables a
  scheduler or background service.
- `.github/workflows/release.yml` runs only on manual dispatch, validates the
  project, packages tracked sources, and publishes a release with generated notes.
