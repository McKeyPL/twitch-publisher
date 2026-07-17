from __future__ import annotations

import unittest
from datetime import date, datetime
from pathlib import Path

from meta_parser import StreamMetadata
from title_cleaner import TitleError, build_final_title, clean_title, title_from_metadata


TEMPLATE = "{tytul_czysty} | {nick} | {data_YYYY-MM-DD}"


class CleanTitleTests(unittest.TestCase):
    def test_real_mrozopl_title_with_emoji(self) -> None:
        raw = (
            "[Daj Sobie Szansę] Dzisiaj relaks - sprzątanie biblioteki. 📖 "
            "Koniec pierwszego tygodnia DSS! Arduino we wtorek ⚙️ !dss"
        )
        self.assertEqual(
            clean_title(raw),
            "[Daj Sobie Szansę] Dzisiaj relaks - sprzątanie biblioteki. 📖 "
            "Koniec pierwszego tygodnia DSS! Arduino we wtorek ⚙️",
        )

    def test_real_arduino_title_with_underscore(self) -> None:
        raw = "[DSS] Arduino - emulowanie pilotów radiowych_na podczerwień !dss"
        self.assertEqual(
            clean_title(raw),
            "[DSS] Arduino - emulowanie pilotów radiowych na podczerwień",
        )

    def test_real_jellysketch_title_with_commands(self) -> None:
        raw = (
            "Jak było na Fantasy & Magic Conie_ _ Rysowanie komiszy _ "
            "[PL_ENG] !dc !koniobaba"
        )
        self.assertEqual(
            clean_title(raw),
            "Jak było na Fantasy & Magic Conie Rysowanie komiszy [PL ENG]",
        )

    def test_command_in_middle_or_at_end_is_removed(self) -> None:
        self.assertEqual(
            clean_title("Oceny wystawione! Teraz PVP, a wkrótce !dds"),
            "Oceny wystawione! Teraz PVP, a wkrótce",
        )
        self.assertEqual(clean_title("Początek !dc dalsza część"), "Początek dalsza część")

    def test_channel_only_title_uses_fallback(self) -> None:
        self.assertEqual(
            clean_title("mrozopl", nick="mrozopl", stream_date=date(2026, 7, 12)),
            "Stream mrozopl 2026-07-12",
        )


class BuildFinalTitleTests(unittest.TestCase):
    def test_none_limit_does_not_truncate(self) -> None:
        result = build_final_title("Bardzo długi tytuł", "mrozopl", date(2026, 7, 12), TEMPLATE, None)
        self.assertEqual(result, "Bardzo długi tytuł | mrozopl | 2026-07-12")

    def test_truncates_only_clean_title_to_at_most_100_characters(self) -> None:
        result = build_final_title("Bardzo długi tytuł " * 20, "mrozopl", date(2026, 7, 12), TEMPLATE, 100)
        self.assertLessEqual(len(result), 100)
        self.assertTrue(result.endswith(" | mrozopl | 2026-07-12"))
        self.assertIn("… | mrozopl | 2026-07-12", result)

    def test_raises_if_fixed_template_part_exceeds_limit(self) -> None:
        with self.assertRaisesRegex(TitleError, "nie mieszcza sie"):
            build_final_title("Tytuł", "bardzo_dlugi_nick", date(2026, 7, 12), TEMPLATE, 10)

    def test_builds_title_from_stream_metadata(self) -> None:
        metadata = StreamMetadata(
            channel="mrozopl",
            title="mrozopl",
            game="Just Chatting",
            started=datetime(2026, 7, 12, 17, 24, 22),
            ended=datetime(2026, 7, 12, 20, 36, 32),
            quality="best",
            source_path=Path("stream_meta.txt"),
        )
        self.assertEqual(
            title_from_metadata(metadata, TEMPLATE, 100),
            "Stream mrozopl 2026-07-12 | mrozopl | 2026-07-12",
        )


if __name__ == "__main__":
    unittest.main()
