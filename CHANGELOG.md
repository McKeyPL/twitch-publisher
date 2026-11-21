# Changelog

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
