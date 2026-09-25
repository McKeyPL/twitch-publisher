# Upload memory audit

## Verified data paths

The publisher does not intentionally load a complete VOD into the Python
interpreter:

- YouTube uses `MediaFileUpload(..., chunksize=50 MiB, resumable=True)` and
  repeatedly calls `next_chunk()`. A retry reuses the same resumable request; it
  does not create a second VOD-sized `bytes` object.
- CDA and Rumble call Playwright `set_input_files()` with a local filesystem
  path. No `read()`, `read_bytes()`, `BytesIO`, or base64 conversion is performed
  by the publisher. The separate browser process performs the form transfer.
- Oversized recordings are split by an FFmpeg child process with `-c copy`.
  Parts are second copies on disk, not in RAM. FFmpeg output is consumed line by
  line and its inter-thread queue is bounded.
- Metadata files are small. YouTube SRT validation now scans line by line instead
  of retaining the complete decoded subtitle file.

The installed `google-api-python-client` implementation was also inspected:
`MediaFileUpload` opens the file in binary mode and resumable `next_chunk()` wraps
only the configured stream slice. The HTTP layer can make temporary copies of a
chunk, so the guard reserves more than the nominal 50 MiB chunk.

## Retry and cleanup

Every Playwright attempt is enclosed in the authenticated-session context
manager, which closes its page, context, browser, and Playwright driver when the
attempt exits. `MemoryError` and `MemoryPressureError` are explicitly
non-retriable inside the immediate exponential-backoff loop. The current scan is
stopped, cyclic exception objects are collected, and SQLite retains `FAILED` so
a later polling cycle can retry after pressure falls.

Browser debug and Playwright tracing are separate. Debug keeps bounded event/DOM
logging and periodic screenshots; tracing is off by default in both the publisher
and Copyright Guard. Legacy `on_error` is treated as `off`, because Playwright
would otherwise need to record the pre-error history continuously. Only an
explicit short `always`/`--browser-trace` reproduction starts the recorder.

## Commit-aware guard

On Windows, `memory_guard.py` calls `GetPerformanceInfo` and evaluates:

- committed bytes (`CommitTotal * PageSize`),
- commit limit (`CommitLimit * PageSize`),
- physical pages currently available.

This is intentionally not based on Task Manager's Available RAM alone. Before
and during an upload/split, the process requires the larger of a configured
absolute floor and a percentage of host capacity, plus an operation-specific
transient reserve. The reserve never scales with VOD size. The default commit
floor is `max(0.75 GiB, 10% of CommitLimit)` and the physical floor is
`max(0.75 GiB, 2% of physical RAM)`. Linux uses `/proc/meminfo`
(`Committed_AS`, `CommitLimit`, `MemAvailable`, and `MemTotal`) for equivalent CI
and server behavior.

If counters cannot be read, the guard logs one warning and fails open so an
unsupported operating system is not permanently blocked.

## Measured fixed browser cost

A Windows snapshot with Playwright 1.61.0, headless Firefox 151, and the CDA
uploader page loaded measured the complete newly-created Python/Node/Firefox tree
at approximately 736 MiB working set and 667 MiB private bytes (10 processes).
The publisher import without a browser measured approximately 65 MiB working set
and 45 MiB private bytes. These are one-machine snapshots, not hard upper bounds;
active page JavaScript, browser updates, authentication state, and diagnostics can
increase them. The default 1.5 GiB browser reserve is intentionally above the
observed idle-uploader footprint.

The browser must remain alive during CDA/Rumble transfer because the HTML file
input, page JavaScript, progress state, and final publication controls are owned
by that browser context. Replacing it with direct HTTP would require depending on
undocumented platform endpoints, CSRF/session details, and resumable-upload
semantics. It is not necessary to achieve file-size-independent memory use: local
Playwright `set_input_files(path)` passes `localPaths` to the local browser rather
than converting the file to a payload. Only a remote Playwright connection uses
the file-stream-copy branch.

## 8 GiB operating boundary

The code path is bounded by a fixed process/browser budget rather than VOD size,
so there is no RAM-based maximum such as "works up to a 4 GiB file". A 50 GiB VOD
still needs platform acceptance and sufficient disk space/time, but not 50 GiB of
RAM. With the default adaptive policy, an 8 GiB/no-pagefile host must have about
2.3 GiB commit headroom before CDA/Rumble starts. If the operating system and
other services leave less than that, the guard intentionally refuses the upload.

Eight GiB is therefore a supported low-memory target for one publisher process,
not an unconditional guarantee for an arbitrary loaded host. Avoid explicit
Playwright tracing, do not run Copyright Guard concurrently, and provide a modest
pagefile for commit elasticity. The publisher itself handles only one recording
and one platform upload at a time. A single-instance lock in the database
directory also prevents an accidental second publisher from doubling the browser
cost.

## Temporary files

FFmpeg `-c copy` segmentation writes normal files below `_publisher_work`; it does
not use `SpooledTemporaryFile`, a RAM disk, or an in-memory pipe for media bytes.
The disk-space preflight requires roughly the source size times the configured
multiplier. Parts are retained after a failed/incomplete multipart upload so it
can resume without splitting again, then removed after every required platform
and YouTube finalization succeeds unless `keep_parts_after_success` is enabled.

## Process isolation conclusion

`start.ps1` launches only `main.py`. This repository contains no Twitch recorder
and the publisher does not start one. The standalone copyright guard has its own
launcher and process as well. Therefore a recorder that died at the same time as
the publisher was not sharing this Python interpreter; the evidence is
consistent with system-wide commit exhaustion or with an external supervisor
that grouped both processes.

A fixed 1 GiB pagefile leaves very little elasticity on a Hyper-V host. The
software guard reduces risk but cannot create commit capacity. A system-managed
or deliberately capacity-planned pagefile remains the host-level protection
against sudden allocations and delayed Dynamic Memory ballooning.
