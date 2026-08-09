# YouTube Copyright Guard architecture

YouTube Copyright Guard is a standalone, manually launched process. It shares
the publisher's typed configuration, OAuth credentials, quota ledger, and SQLite
database, but it is never imported or started by `main.py`. A guard failure must
not stop Twitch recording discovery or uploads to any platform.

## Scope

The guard remediates a restriction when a video is blocked worldwide or when it
is unavailable in Poland (`PL`) or Germany (`DE`). Restrictions affecting only
other regions are intentionally accepted. Monetization-only claims, copyright
strikes, takedowns, disputes, and appeals are outside the automated scope.

Region restrictions can use either API representation:

- `allowed: []` is worldwide blocking.
- A non-empty `allowed` list is actionable when `PL` or `DE` is absent.
- A `blocked` list is actionable when it contains `PL` or `DE`.
- Other regional restrictions are recorded without opening Studio.

## Remediation policy

For an actionable audio claim, erase only the song first. If processing finishes
and the protected-region restriction remains, mute all audio in the claimed
segment. For visual or audiovisual claims, trim the claimed segment when Studio
offers that operation. Only one edit may be submitted for a video at a time.

Studio can require one acknowledgement checkbox before the irreversible final
button. The executor checks it only when exactly one checkbox is visible; multiple
checkboxes are treated as an ambiguous UI and no edit is submitted.

Trimming is rejected when claim ranges are ambiguous, the edit would remove more
than 90 percent of the video, or fewer than 60 seconds would remain. The guard
never deletes or reuploads a complete video and never submits a legal statement.

## Processing model

The normal interval is two hours. After submitting a Studio edit the browser is
closed; the process never waits for hours in a page. The next cycle checks whether
editing is still in progress and whether worldwide, Polish, and German access has
been restored. Blocking in any other remaining country does not prevent RESOLVED.

## Browser and diagnostics

YouTube Studio uses a dedicated browser profile and storage state under `auth/`.
Google may reject sign-in when a browser is controlled by automation. Therefore
`--login` starts a regular installed Chrome or Edge process without Playwright or
remote debugging. The user completes login/MFA, waits for Studio, and closes that
browser, then confirms closure in the console. The guard waits for the profile lock
to be released before launching Playwright with the same dedicated profile and
verifying authentication. Normal remediation never types Google credentials.

The initial burn-in runs headful and stores traces, screenshots, browser console
messages, failed requests, parsed claim data, decisions, and confirmation evidence
under `logs/youtube_copyright`. These files can contain session data and must never
be committed or shared without review.

## Captions

Erase and mute operations do not alter the timeline. Before a trim, the guard
downloads and backs up the owned Twitch Chat caption track. After YouTube finishes
processing, it verifies the duration change, removes or clips cues inside deleted
ranges, shifts later cues, updates the existing track, and waits for it to return
to serving status. Automatic ASR tracks are not modified.

## State and audit

Dedicated SQLite tables store videos, synthetic claim fingerprints, append-only
actions, and runs. Every irreversible click is tied to a run ID, video ID,
restriction reason, selected action, claim range, screenshots, trace, timestamps,
and final verification. WAL and short transactions allow the publisher and guard
to run as separate processes.

Ctrl+C marks an in-flight action uncertain and skips new synchronous Playwright
cleanup calls. If the browser operation still does not unwind within five seconds,
a watchdog forces exit code 130. Windows and Linux launchers recognize that code as
an operator stop and do not restart the guard.
