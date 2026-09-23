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

## Commit-aware guard

On Windows, `memory_guard.py` calls `GetPerformanceInfo` and evaluates:

- committed bytes (`CommitTotal * PageSize`),
- commit limit (`CommitLimit * PageSize`),
- physical pages currently available.

This is intentionally not based on Task Manager's Available RAM alone. Before
and during an upload/split, the process requires the configured fixed headroom
plus an operation-specific transient reserve. The reserve never scales with VOD
size. Linux uses `/proc/meminfo` (`Committed_AS`, `CommitLimit`, and
`MemAvailable`) for equivalent CI and server behavior.

If counters cannot be read, the guard logs one warning and fails open so an
unsupported operating system is not permanently blocked.

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
