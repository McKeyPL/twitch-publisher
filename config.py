"""Wczytywanie, podstawianie zmiennych i walidacja ``config.yaml``."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Mapping

import yaml
from dotenv import load_dotenv


class ConfigError(ValueError):
    """Konfiguracja jest niekompletna albo ma niepoprawny format."""


_ENV_PATTERN = re.compile(
    r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}"
)


def expand_environment_variables(
    value: Any,
    *,
    environ: Mapping[str, str] | None = None,
    location: str = "config",
) -> Any:
    """Rekurencyjnie rozwija ``${VAR}`` i ``${VAR:-default}`` w dict/list/string."""
    environment = os.environ if environ is None else environ

    if isinstance(value, dict):
        return {
            key: expand_environment_variables(
                item,
                environ=environment,
                location=f"{location}.{key}",
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            expand_environment_variables(
                item,
                environ=environment,
                location=f"{location}[{index}]",
            )
            for index, item in enumerate(value)
        ]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        default = match.group("default")
        env_value = environment.get(name)

        if default is not None:
            return env_value if env_value else default
        if env_value is None:
            raise ConfigError(
                f"Brak wymaganej zmiennej srodowiskowej {name!r} w {location}"
            )
        return env_value

    return _ENV_PATTERN.sub(replace, value)


@dataclass(frozen=True, slots=True)
class PathsConfig:
    recordings_root: Path
    ffprobe: str
    database: Path
    log_directory: Path
    auth_directory: Path


@dataclass(frozen=True, slots=True)
class WatcherConfig:
    poll_interval_seconds: float
    size_stability_seconds: float


@dataclass(frozen=True, slots=True)
class YouTubeConfig:
    enabled: bool
    client_secrets_file: Path | None
    token_file: Path
    privacy_status: str
    max_duration_hours: float
    title_limit: int
    category_id: str
    captions_language: str
    captions_name: str
    daily_upload_limit: int
    daily_quota_units: int
    upload_quota_units: int
    captions_quota_units: int
    srt_max_size_mb: float
    playlists: dict[str, str]


@dataclass(frozen=True, slots=True)
class BrowserPlatformConfig:
    enabled: bool
    upload_url: str
    storage_state_file: Path
    title_limit: int | None
    max_duration_hours: float | None
    primary_category: str | None = None
    license_option: str | None = None
    max_file_size_gb: float | None = None


@dataclass(frozen=True, slots=True)
class PlatformsConfig:
    youtube: YouTubeConfig
    cda: BrowserPlatformConfig
    rumble: BrowserPlatformConfig


@dataclass(frozen=True, slots=True)
class BrowserConfig:
    firefox_profile_path: Path | None
    headless: bool
    interactive_login_headless: bool
    debug: bool = False
    debug_directory: Path = Path("logs/browser_debug")
    debug_screenshot_interval_seconds: float = 300.0


@dataclass(frozen=True, slots=True)
class MetadataConfig:
    title_template: str
    remove_bang_tags: bool


@dataclass(frozen=True, slots=True)
class RetryConfig:
    max_attempts: int
    initial_backoff_seconds: float
    multiplier: float
    max_backoff_seconds: float


@dataclass(frozen=True, slots=True)
class MovingConfig:
    uploaded_directory_name: str


@dataclass(frozen=True, slots=True)
class CleanupConfig:
    retention_days: int
    dry_run: bool


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    level: str
    console_colors: bool
    file_name: str


@dataclass(frozen=True, slots=True)
class Config:
    paths: PathsConfig
    watcher: WatcherConfig
    platforms: PlatformsConfig
    browser: BrowserConfig
    metadata: MetadataConfig
    retry: RetryConfig
    moving: MovingConfig
    cleanup: CleanupConfig
    logging: LoggingConfig


def _mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{location} musi byc mapa YAML")
    return value


def _required(section: Mapping[str, Any], key: str, location: str) -> Any:
    if key not in section:
        raise ConfigError(f"Brak wymaganego pola {location}.{key}")
    return section[key]


def _string(value: Any, location: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{location} musi byc tekstem")
    result = value.strip()
    if not allow_empty and not result:
        raise ConfigError(f"{location} nie moze byc puste")
    return result


def _boolean(value: Any, location: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{location} musi miec wartosc true albo false")
    return value


def _positive_float(value: Any, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"{location} musi byc liczba wieksza od zera")
    return float(value)


def _positive_int(value: Any, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{location} musi byc liczba calkowita wieksza od zera")
    return value


def _optional_positive_float(value: Any, location: str) -> float | None:
    return None if value is None else _positive_float(value, location)


def _optional_positive_int(value: Any, location: str) -> int | None:
    return None if value is None else _positive_int(value, location)


def _optional_string(value: Any, location: str) -> str | None:
    if value is None:
        return None
    result = _string(value, location, allow_empty=True)
    return result or None


def _path(value: Any, location: str, *, allow_empty: bool = False) -> Path | None:
    text = _string(value, location, allow_empty=allow_empty)
    if not text and allow_empty:
        return None
    if "\x00" in text:
        raise ConfigError(f"{location} zawiera niedozwolony znak NUL")
    return Path(text)


def _windows_absolute_path(value: Any, location: str) -> Path:
    text = _string(value, location)
    if "\x00" in text:
        raise ConfigError(f"{location} zawiera niedozwolony znak NUL")

    windows_path = PureWindowsPath(text)
    if not windows_path.is_absolute():
        raise ConfigError(
            f"{location} musi byc absolutna sciezka Windows, np. "
            r"E:\TwitchRecordings lub \\server\share\TwitchRecordings"
        )

    invalid_characters = set('<>"|?*')
    for part in windows_path.parts[1:]:
        if invalid_characters.intersection(part) or ":" in part:
            raise ConfigError(f"{location} zawiera niedozwolone znaki: {text!r}")
    return Path(text)


def _browser_platform(
    raw: Any,
    location: str,
) -> BrowserPlatformConfig:
    section = _mapping(raw, location)
    return BrowserPlatformConfig(
        enabled=_boolean(_required(section, "enabled", location), f"{location}.enabled"),
        upload_url=_string(
            _required(section, "upload_url", location), f"{location}.upload_url"
        ),
        storage_state_file=_path(
            _required(section, "storage_state_file", location),
            f"{location}.storage_state_file",
        ),
        title_limit=_optional_positive_int(
            section.get("title_limit"), f"{location}.title_limit"
        ),
        max_duration_hours=_optional_positive_float(
            section.get("max_duration_hours"), f"{location}.max_duration_hours"
        ),
        primary_category=_optional_string(
            section.get("primary_category"), f"{location}.primary_category"
        ),
        license_option=_optional_string(
            section.get("license_option"), f"{location}.license_option"
        ),
        max_file_size_gb=_optional_positive_float(
            section.get("max_file_size_gb"), f"{location}.max_file_size_gb"
        ),
    )


def config_from_dict(raw: Mapping[str, Any]) -> Config:
    """Buduje i waliduje typowana konfiguracje z rozwinietej mapy YAML."""
    root = _mapping(raw, "config")
    paths = _mapping(_required(root, "paths", "config"), "paths")
    watcher = _mapping(_required(root, "watcher", "config"), "watcher")
    platforms = _mapping(_required(root, "platforms", "config"), "platforms")
    youtube = _mapping(_required(platforms, "youtube", "platforms"), "platforms.youtube")
    browser = _mapping(_required(root, "browser", "config"), "browser")
    metadata = _mapping(_required(root, "metadata", "config"), "metadata")
    retry = _mapping(_required(root, "retry", "config"), "retry")
    moving = _mapping(_required(root, "moving", "config"), "moving")
    cleanup = _mapping(_required(root, "cleanup", "config"), "cleanup")
    logging = _mapping(_required(root, "logging", "config"), "logging")

    youtube_enabled = _boolean(
        _required(youtube, "enabled", "platforms.youtube"),
        "platforms.youtube.enabled",
    )
    secrets_text = _string(
        _required(youtube, "client_secrets_file", "platforms.youtube"),
        "platforms.youtube.client_secrets_file",
        allow_empty=True,
    )
    if youtube_enabled and not secrets_text:
        raise ConfigError(
            "platforms.youtube.client_secrets_file musi byc ustawione, "
            "gdy platforms.youtube.enabled=true"
        )

    playlists_raw = _mapping(
        _required(youtube, "playlists", "platforms.youtube"),
        "platforms.youtube.playlists",
    )
    playlists = {
        _string(nick, "platforms.youtube.playlists.<nick>"): _string(
            playlist_id,
            f"platforms.youtube.playlists.{nick}",
            allow_empty=True,
        )
        for nick, playlist_id in playlists_raw.items()
    }

    multiplier = _positive_float(
        _required(retry, "multiplier", "retry"), "retry.multiplier"
    )
    if multiplier < 1:
        raise ConfigError("retry.multiplier musi byc co najmniej 1")

    retry_config = RetryConfig(
        max_attempts=_positive_int(
            _required(retry, "max_attempts", "retry"), "retry.max_attempts"
        ),
        initial_backoff_seconds=_positive_float(
            _required(retry, "initial_backoff_seconds", "retry"),
            "retry.initial_backoff_seconds",
        ),
        multiplier=multiplier,
        max_backoff_seconds=_positive_float(
            _required(retry, "max_backoff_seconds", "retry"),
            "retry.max_backoff_seconds",
        ),
    )

    return Config(
        paths=PathsConfig(
            recordings_root=_windows_absolute_path(
                _required(paths, "recordings_root", "paths"), "paths.recordings_root"
            ),
            ffprobe=_string(_required(paths, "ffprobe", "paths"), "paths.ffprobe"),
            database=_path(_required(paths, "database", "paths"), "paths.database"),
            log_directory=_path(
                _required(paths, "log_directory", "paths"), "paths.log_directory"
            ),
            auth_directory=_path(
                _required(paths, "auth_directory", "paths"), "paths.auth_directory"
            ),
        ),
        watcher=WatcherConfig(
            poll_interval_seconds=_positive_float(
                _required(watcher, "poll_interval_seconds", "watcher"),
                "watcher.poll_interval_seconds",
            ),
            size_stability_seconds=_positive_float(
                _required(watcher, "size_stability_seconds", "watcher"),
                "watcher.size_stability_seconds",
            ),
        ),
        platforms=PlatformsConfig(
            youtube=YouTubeConfig(
                enabled=youtube_enabled,
                client_secrets_file=Path(secrets_text) if secrets_text else None,
                token_file=_path(
                    _required(youtube, "token_file", "platforms.youtube"),
                    "platforms.youtube.token_file",
                ),
                privacy_status=_string(
                    _required(youtube, "privacy_status", "platforms.youtube"),
                    "platforms.youtube.privacy_status",
                ),
                max_duration_hours=_positive_float(
                    _required(youtube, "max_duration_hours", "platforms.youtube"),
                    "platforms.youtube.max_duration_hours",
                ),
                title_limit=_positive_int(
                    _required(youtube, "title_limit", "platforms.youtube"),
                    "platforms.youtube.title_limit",
                ),
                category_id=_string(
                    _required(youtube, "category_id", "platforms.youtube"),
                    "platforms.youtube.category_id",
                ),
                captions_language=_string(
                    _required(youtube, "captions_language", "platforms.youtube"),
                    "platforms.youtube.captions_language",
                ),
                captions_name=_string(
                    _required(youtube, "captions_name", "platforms.youtube"),
                    "platforms.youtube.captions_name",
                ),
                daily_upload_limit=_positive_int(
                    _required(youtube, "daily_upload_limit", "platforms.youtube"),
                    "platforms.youtube.daily_upload_limit",
                ),
                daily_quota_units=_positive_int(
                    _required(youtube, "daily_quota_units", "platforms.youtube"),
                    "platforms.youtube.daily_quota_units",
                ),
                upload_quota_units=_positive_int(
                    _required(youtube, "upload_quota_units", "platforms.youtube"),
                    "platforms.youtube.upload_quota_units",
                ),
                captions_quota_units=_positive_int(
                    _required(youtube, "captions_quota_units", "platforms.youtube"),
                    "platforms.youtube.captions_quota_units",
                ),
                srt_max_size_mb=_positive_float(
                    _required(youtube, "srt_max_size_mb", "platforms.youtube"),
                    "platforms.youtube.srt_max_size_mb",
                ),
                playlists=playlists,
            ),
            cda=_browser_platform(
                _required(platforms, "cda", "platforms"), "platforms.cda"
            ),
            rumble=_browser_platform(
                _required(platforms, "rumble", "platforms"), "platforms.rumble"
            ),
        ),
        browser=BrowserConfig(
            firefox_profile_path=_path(
                _required(browser, "firefox_profile_path", "browser"),
                "browser.firefox_profile_path",
                allow_empty=True,
            ),
            headless=_boolean(
                _required(browser, "headless", "browser"), "browser.headless"
            ),
            interactive_login_headless=_boolean(
                _required(browser, "interactive_login_headless", "browser"),
                "browser.interactive_login_headless",
            ),
            debug=_boolean(browser.get("debug", False), "browser.debug"),
            debug_directory=_path(
                browser.get("debug_directory", "logs/browser_debug"),
                "browser.debug_directory",
            ),
            debug_screenshot_interval_seconds=_positive_float(
                browser.get("debug_screenshot_interval_seconds", 300),
                "browser.debug_screenshot_interval_seconds",
            ),
        ),
        metadata=MetadataConfig(
            title_template=_string(
                _required(metadata, "title_template", "metadata"),
                "metadata.title_template",
            ),
            remove_bang_tags=_boolean(
                _required(metadata, "remove_bang_tags", "metadata"),
                "metadata.remove_bang_tags",
            ),
        ),
        retry=retry_config,
        moving=MovingConfig(
            uploaded_directory_name=_string(
                _required(moving, "uploaded_directory_name", "moving"),
                "moving.uploaded_directory_name",
            )
        ),
        cleanup=CleanupConfig(
            retention_days=_positive_int(
                _required(cleanup, "retention_days", "cleanup"),
                "cleanup.retention_days",
            ),
            dry_run=_boolean(
                _required(cleanup, "dry_run", "cleanup"), "cleanup.dry_run"
            ),
        ),
        logging=LoggingConfig(
            level=_string(_required(logging, "level", "logging"), "logging.level"),
            console_colors=_boolean(
                _required(logging, "console_colors", "logging"),
                "logging.console_colors",
            ),
            file_name=_string(
                _required(logging, "file_name", "logging"), "logging.file_name"
            ),
        ),
    )


def load_config(
    config_path: str | Path | None = None,
    *,
    dotenv_path: str | Path | None = None,
) -> Config:
    """Laduje .env, YAML, rozwija zmienne i zwraca zwalidowany obiekt ``Config``."""
    yaml_path = Path(config_path) if config_path else Path(__file__).with_name("config.yaml")
    env_path = Path(dotenv_path) if dotenv_path else yaml_path.with_name(".env")

    # Zmienne procesu maja pierwszenstwo przed wartosciami z .env.
    load_dotenv(dotenv_path=env_path, override=False)

    try:
        with yaml_path.open("r", encoding="utf-8") as stream:
            raw = yaml.safe_load(stream)
    except OSError as exc:
        raise ConfigError(f"Nie mozna odczytac konfiguracji {yaml_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Niepoprawny YAML w {yaml_path}: {exc}") from exc

    if raw is None:
        raise ConfigError(f"Plik konfiguracji {yaml_path} jest pusty")
    expanded = expand_environment_variables(raw)
    return config_from_dict(_mapping(expanded, "config"))
