"""Czyszczenie tytulow Twitcha i budowanie tytulow platform docelowych."""

from __future__ import annotations

import re
from datetime import date
from string import Formatter

from meta_parser import StreamMetadata


class TitleError(ValueError):
    """Tytul albo jego szablon nie pozwala zbudowac poprawnego wyniku."""


# Regex rozpoznaje token zaczynajacy sie od "!" na poczatku tekstu albo po bialym
# znaku. ``(?<!\S)`` nie dopasuje wykrzyknika wewnatrz slowa (np. "DSS!"), a
# ``[^\s!]+`` usuwa tresc komendy do kolejnego bialego znaku. Dziala zatem takze
# dla tagow w srodku tytulu, o ile sa osobnymi tokenami, nie tylko na jego koncu.
TWITCH_COMMAND_RE = re.compile(r"(?<!\S)![^\s!]+", flags=re.UNICODE)
UNDERSCORES_RE = re.compile(r"_+")
WHITESPACE_RE = re.compile(r"\s+")

_ALLOWED_TEMPLATE_FIELDS = {"tytul_czysty", "nick", "data_YYYY-MM-DD"}


def _fallback_title(nick: str | None, stream_date: date | None) -> str:
    parts = ["Stream"]
    if nick and nick.strip():
        parts.append(nick.strip())
    if stream_date is not None:
        if not isinstance(stream_date, date):
            raise TypeError("stream_date musi byc obiektem datetime.date")
        parts.append(stream_date.isoformat())
    return " ".join(parts)


def clean_title(
    raw_title: str,
    *,
    nick: str | None = None,
    stream_date: date | None = None,
) -> str:
    """Usuwa komendy Twitcha i normalizuje tytul bez usuwania emoji ani nawiasow.

    Opcjonalne ``nick`` i ``stream_date`` pozwalaja utworzyc fallback, gdy po
    czyszczeniu nic nie zostanie albo tytul jest tylko nazwa kanalu.
    """
    if not isinstance(raw_title, str):
        raise TypeError("raw_title musi byc tekstem")

    cleaned = TWITCH_COMMAND_RE.sub(" ", raw_title)
    cleaned = UNDERSCORES_RE.sub(" ", cleaned)
    cleaned = WHITESPACE_RE.sub(" ", cleaned).strip()

    equals_channel = bool(nick) and cleaned.casefold() == nick.strip().casefold()
    if not cleaned or equals_channel:
        return _fallback_title(nick, stream_date)
    return cleaned


def _validate_template(template: str) -> None:
    if not isinstance(template, str) or not template:
        raise TitleError("Szablon tytulu nie moze byc pusty")

    try:
        parsed = list(Formatter().parse(template))
    except ValueError as exc:
        raise TitleError(f"Niepoprawny szablon tytulu: {exc}") from exc

    fields = [field_name for _, field_name, _, _ in parsed if field_name is not None]
    unknown = sorted(set(fields) - _ALLOWED_TEMPLATE_FIELDS)
    if unknown:
        raise TitleError(f"Nieznane pola szablonu: {', '.join(unknown)}")
    if fields.count("tytul_czysty") != 1:
        raise TitleError("Szablon musi zawierac dokladnie jedno pole {tytul_czysty}")


def build_final_title(
    clean_title: str,
    nick: str,
    stream_date: date,
    template: str,
    max_length: int | None,
) -> str:
    """Wypelnia szablon, skracajac w razie potrzeby tylko czysty tytul."""
    if not isinstance(clean_title, str) or not clean_title:
        raise TitleError("clean_title nie moze byc pusty")
    if not isinstance(nick, str) or not nick.strip():
        raise TitleError("nick nie moze byc pusty")
    if not isinstance(stream_date, date):
        raise TypeError("stream_date musi byc obiektem datetime.date")
    if max_length is not None and (
        isinstance(max_length, bool) or not isinstance(max_length, int) or max_length <= 0
    ):
        raise TitleError("max_length musi byc dodatnia liczba calkowita albo None")

    _validate_template(template)
    values = {
        "tytul_czysty": clean_title,
        "nick": nick.strip(),
        "data_YYYY-MM-DD": stream_date.isoformat(),
    }
    try:
        result = template.format(**values)
    except (KeyError, ValueError) as exc:
        raise TitleError(f"Nie mozna wypelnic szablonu tytulu: {exc}") from exc

    if max_length is None or len(result) <= max_length:
        return result

    without_title = template.format(**{**values, "tytul_czysty": ""})
    available_for_title = max_length - len(without_title)
    if available_for_title < 1:
        raise TitleError(
            "Nick, data i stala czesc szablonu nie mieszcza sie w max_length; "
            "nie mozna skrocic tytulu bez naruszenia stalych elementow"
        )

    prefix_length = available_for_title - 1  # jedno miejsce rezerwujemy na "…"
    shortened = clean_title[:prefix_length].rstrip() + "…"
    result = template.format(**{**values, "tytul_czysty": shortened})
    if len(result) > max_length:  # zabezpieczenie przy przyszlych zmianach szablonu
        raise TitleError("Nie udalo sie dopasowac tytulu do max_length")
    return result


def title_from_metadata(
    metadata: StreamMetadata,
    template: str,
    max_length: int | None,
) -> str:
    """Buduje tytul platformy bezposrednio z parsed ``StreamMetadata``."""
    stream_date = metadata.started.date()
    cleaned = clean_title(
        metadata.title,
        nick=metadata.channel,
        stream_date=stream_date,
    )
    return build_final_title(
        cleaned,
        metadata.channel,
        stream_date,
        template,
        max_length,
    )

