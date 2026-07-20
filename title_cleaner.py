"""Clean Twitch titles and build titles for destination platforms."""

from __future__ import annotations

import re
from datetime import date
from string import Formatter

from meta_parser import StreamMetadata


class TitleError(ValueError):
    """A title or its template cannot produce a valid result."""


# The regex recognizes a token starting with "!" at the beginning of the text or
# after whitespace. ``(?<!\S)`` does not match an exclamation mark inside a word
# (for example "DSS!"), while ``[^\s!]+`` consumes the command until whitespace.
# It therefore handles standalone commands in the middle, not only at the end.
TWITCH_COMMAND_RE = re.compile(r"(?<!\S)![^\s!]+", flags=re.UNICODE)
UNDERSCORES_RE = re.compile(r"_+")
WHITESPACE_RE = re.compile(r"\s+")

_ALLOWED_TEMPLATE_FIELDS = {"clean_title", "channel", "date_YYYY-MM-DD"}


def _fallback_title(nick: str | None, stream_date: date | None) -> str:
    parts = ["Stream"]
    if nick and nick.strip():
        parts.append(nick.strip())
    if stream_date is not None:
        if not isinstance(stream_date, date):
            raise TypeError("stream_date must be a datetime.date instance")
        parts.append(stream_date.isoformat())
    return " ".join(parts)


def clean_title(
    raw_title: str,
    *,
    nick: str | None = None,
    stream_date: date | None = None,
) -> str:
    """Remove Twitch commands and normalize a title without removing emoji or brackets.

    Optional ``nick`` and ``stream_date`` values provide a fallback when cleaning
    leaves no content or the title contains only the channel name.
    """
    if not isinstance(raw_title, str):
        raise TypeError("raw_title must be a string")

    cleaned = TWITCH_COMMAND_RE.sub(" ", raw_title)
    cleaned = UNDERSCORES_RE.sub(" ", cleaned)
    cleaned = WHITESPACE_RE.sub(" ", cleaned).strip()

    equals_channel = bool(nick) and cleaned.casefold() == nick.strip().casefold()
    if not cleaned or equals_channel:
        return _fallback_title(nick, stream_date)
    return cleaned


def _validate_template(template: str) -> None:
    if not isinstance(template, str) or not template:
        raise TitleError("The title template cannot be empty")

    try:
        parsed = list(Formatter().parse(template))
    except ValueError as exc:
        raise TitleError(f"Invalid title template: {exc}") from exc

    fields = [field_name for _, field_name, _, _ in parsed if field_name is not None]
    unknown = sorted(set(fields) - _ALLOWED_TEMPLATE_FIELDS)
    if unknown:
        raise TitleError(f"Unknown template fields: {', '.join(unknown)}")
    if fields.count("clean_title") != 1:
        raise TitleError("The template must contain exactly one {clean_title} field")


def build_final_title(
    clean_title: str,
    nick: str,
    stream_date: date,
    template: str,
    max_length: int | None,
) -> str:
    """Fill the template, shortening only the cleaned title when necessary."""
    if not isinstance(clean_title, str) or not clean_title:
        raise TitleError("clean_title cannot be empty")
    if not isinstance(nick, str) or not nick.strip():
        raise TitleError("nick cannot be empty")
    if not isinstance(stream_date, date):
        raise TypeError("stream_date must be a datetime.date instance")
    if max_length is not None and (
        isinstance(max_length, bool) or not isinstance(max_length, int) or max_length <= 0
    ):
        raise TitleError("max_length must be a positive integer or None")

    _validate_template(template)
    values = {
        "clean_title": clean_title,
        "channel": nick.strip(),
        "date_YYYY-MM-DD": stream_date.isoformat(),
    }
    try:
        result = template.format(**values)
    except (KeyError, ValueError) as exc:
        raise TitleError(f"Cannot fill the title template: {exc}") from exc

    if max_length is None or len(result) <= max_length:
        return result

    without_title = template.format(**{**values, "clean_title": ""})
    available_for_title = max_length - len(without_title)
    if available_for_title < 1:
        raise TitleError(
            "The channel, date, and fixed template text exceed max_length; "
            "the title cannot be shortened without changing fixed elements"
        )

    prefix_length = available_for_title - 1  # reserve one character for "…"
    shortened = clean_title[:prefix_length].rstrip() + "…"
    result = template.format(**{**values, "clean_title": shortened})
    if len(result) > max_length:  # safeguard against future template changes
        raise TitleError("Could not fit the title within max_length")
    return result


def title_from_metadata(
    metadata: StreamMetadata,
    template: str,
    max_length: int | None,
) -> str:
    """Build a platform title directly from parsed ``StreamMetadata``."""
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
