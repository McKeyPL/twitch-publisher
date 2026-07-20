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
|-- duration_check.py           # file stability and ffprobe
|-- state.py                    # SQLite/WAL, upload status, quota
|-- mover.py                    # safe movement into _uploaded
|-- cleanup.py                  # separate manual dry-run-first CLI
|-- main.py                     # application orchestration
|-- start.ps1                   # manual Windows launcher and restart loop
|-- start.sh                    # manual Linux launcher and restart loop
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
- `state.py` is the only module that writes upload status and local quota usage.
- `main.py` composes dependencies, retains the tracker between cycles, and
  isolates exceptions per recording and per platform.
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
