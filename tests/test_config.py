from __future__ import annotations

import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from config import ConfigError, config_from_dict, expand_environment_variables, load_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"


def valid_raw_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)
    raw["platforms"]["youtube"]["client_secrets_file"] = "auth/credentials.json"
    raw["browser"]["firefox_profile_path"] = ""
    raw["platforms"]["youtube"]["playlists"] = {"mrozopl": ""}
    return raw


class EnvironmentExpansionTests(unittest.TestCase):
    def test_expands_recursively_in_dicts_and_lists(self) -> None:
        value = {
            "secret": "${SECRET}",
            "items": ["prefix-${NAME}", {"fallback": "${EMPTY:-default-value}"}],
        }
        expanded = expand_environment_variables(
            value,
            environ={"SECRET": "abc", "NAME": "streamer", "EMPTY": ""},
        )
        self.assertEqual(expanded["secret"], "abc")
        self.assertEqual(expanded["items"][0], "prefix-streamer")
        self.assertEqual(expanded["items"][1]["fallback"], "default-value")

    def test_missing_required_variable_has_clear_error(self) -> None:
        with self.assertRaisesRegex(ConfigError, "MISSING.*config.secret"):
            expand_environment_variables(
                {"secret": "${MISSING}"}, environ={}, location="config"
            )

    def test_dotenv_is_loaded_before_yaml_expansion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            env_file = Path(temporary_directory) / ".env"
            env_file.write_text(
                "YOUTUBE_CLIENT_SECRETS_FILE=auth/from-dotenv.json\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                config = load_config(CONFIG_PATH, dotenv_path=env_file)

        self.assertEqual(
            config.platforms.youtube.client_secrets_file,
            Path("auth/from-dotenv.json"),
        )


class ConfigValidationTests(unittest.TestCase):
    def test_returns_typed_config(self) -> None:
        config = config_from_dict(valid_raw_config())
        self.assertEqual(config.watcher.poll_interval_seconds, 30.0)
        self.assertEqual(config.paths.recordings_root, Path(r"E:\TwitchRecordings"))
        self.assertEqual(config.platforms.youtube.category_id, "20")
        self.assertEqual(config.platforms.youtube.captions_language, "pl")
        self.assertEqual(config.platforms.youtube.captions_name, "Czat Twitch")
        self.assertEqual(config.platforms.youtube.daily_upload_limit, 100)
        self.assertEqual(config.platforms.rumble.primary_category, "Gaming")
        self.assertEqual(config.platforms.rumble.max_file_size_gb, 15.0)
        self.assertFalse(hasattr(config.platforms.youtube, "retry"))

    def test_loads_rumble_license_from_environment(self) -> None:
        with patch.dict(
            os.environ,
            {
                "YOUTUBE_CLIENT_SECRETS_FILE": "auth/credentials.json",
                "RUMBLE_LICENSE_OPTION": "6",
            },
            clear=True,
        ):
            config = load_config(CONFIG_PATH, dotenv_path=PROJECT_ROOT / "missing.env")

        self.assertEqual(config.platforms.rumble.license_option, "6")

    def test_youtube_credentials_required_when_enabled(self) -> None:
        raw = valid_raw_config()
        raw["platforms"]["youtube"]["client_secrets_file"] = ""
        with self.assertRaisesRegex(ConfigError, "client_secrets_file"):
            config_from_dict(raw)

        disabled = copy.deepcopy(raw)
        disabled["platforms"]["youtube"]["enabled"] = False
        config = config_from_dict(disabled)
        self.assertIsNone(config.platforms.youtube.client_secrets_file)

    def test_rejects_non_positive_numeric_value(self) -> None:
        raw = valid_raw_config()
        raw["watcher"]["poll_interval_seconds"] = 0
        with self.assertRaisesRegex(ConfigError, "poll_interval_seconds"):
            config_from_dict(raw)

    def test_rejects_non_positive_browser_file_size_limit(self) -> None:
        raw = valid_raw_config()
        raw["platforms"]["rumble"]["max_file_size_gb"] = 0
        with self.assertRaisesRegex(ConfigError, "max_file_size_gb"):
            config_from_dict(raw)

    def test_rejects_empty_youtube_category_id(self) -> None:
        raw = valid_raw_config()
        raw["platforms"]["youtube"]["category_id"] = "   "
        with self.assertRaisesRegex(ConfigError, "category_id"):
            config_from_dict(raw)

    def test_rejects_empty_youtube_caption_fields(self) -> None:
        for field_name in ("captions_language", "captions_name"):
            with self.subTest(field=field_name):
                raw = valid_raw_config()
                raw["platforms"]["youtube"][field_name] = ""
                with self.assertRaisesRegex(ConfigError, field_name):
                    config_from_dict(raw)

    def test_rejects_non_positive_daily_upload_limit(self) -> None:
        for invalid_value in (0, -1):
            with self.subTest(value=invalid_value):
                raw = valid_raw_config()
                raw["platforms"]["youtube"]["daily_upload_limit"] = invalid_value
                with self.assertRaisesRegex(ConfigError, "daily_upload_limit"):
                    config_from_dict(raw)

    def test_rejects_relative_recordings_root(self) -> None:
        raw = valid_raw_config()
        raw["paths"]["recordings_root"] = "recordings"
        with self.assertRaisesRegex(ConfigError, "absolutna sciezka Windows"):
            config_from_dict(raw)


if __name__ == "__main__":
    unittest.main()
