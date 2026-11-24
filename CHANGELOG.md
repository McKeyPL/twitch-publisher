# Changelog

## v3.1.1

- **CDA uploads first.** The publisher attempts CDA for every ready recording
  before starting YouTube or Rumble. After each YouTube or Rumble transfer,
  including split parts, it checks for newly ready recordings and gives CDA
  priority before the next transfer.
- **Finish the current upload on Ctrl+C.** The first press keeps progress
  logging active and lets the current transfer finish before exiting. The
  second press interrupts it. Playwright runs separately from the terminal
  so the first press leaves CDA and Rumble browser sessions open.
- **Accept seven-digit timestamps.** Recording metadata with seven fractional
  digits in `Started` or `Ended` now parses on Python 3.10 with its timezone
  offset preserved.
- **Identify waiting recordings in the log file.** Each scan summary on disk
  includes the MKV path and wait reason for each pending recording. The
  console keeps its concise summary.

## v3.0.0

> [!CAUTION]
> **Breaking change. Back up your config.yaml before updating.**
>
> Copy the active config.yaml outside the repository before running git pull,
> switching to this release, or unpacking it over an existing installation.
> Git may remove the old tracked file during an update. Restore your backup as
> config.yaml in the repository root before restarting the publisher or
> Copyright Guard. A committed copy can be recovered with
> git show v2.1.0:config.yaml. That copy contains only the settings that were
> committed, so preserve your local file before upgrading.

- Git now tracks `config.example.yaml`. The active `config.yaml` stays local.
- New installations start with all upload platforms and Copyright Guard disabled.
- Launchers stop with a configuration error if the required file is missing.
