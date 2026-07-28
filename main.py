"""Orchestrate the watcher, uploaders, SQLite state, and file movement."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Callable, Mapping, Sequence

from config import Config, load_config
from duration_check import (
    FileSizeStabilityTracker,
    ReadinessResult,
    ReadinessStatus,
    exceeds_duration_limit,
    probe_duration_seconds,
)
from meta_parser import StreamMetadata
from media_splitter import MediaSplitter, SplitConstraints, SplitPlan
from mover import move_processed_recording
from recording_name_normalizer import normalize_recording_set_for_cda
from state import (
    StateStore,
    UploadPartSpec,
    UploadPartStatusRecord,
    UploadStatus,
)
from title_cleaner import title_from_metadata, title_from_metadata_part
from uploaders.base import BaseUploader
from uploaders.cda import CDAUploader
from uploaders.rumble import RumbleUploader, _is_file_size_limit_error
from uploaders.youtube import YouTubeUploader
from watcher import scan_cycle


logger = logging.getLogger(__name__)
DurationProbe = Callable[[Path], float]
NO_AUTO_RETRY_PREFIX = "[NO_AUTO_RETRY] "


def _request_stop(
    stop_event: threading.Event,
    signum: int,
    frame: object,
) -> None:
    """SIGINT interrupts the active operation; SIGTERM stops after this step."""
    if signum == getattr(signal, "SIGINT", None):
        stop_event.set()
        logger.info("Received SIGINT; interrupting the active operation")
        raise KeyboardInterrupt
    logger.info("Received signal %s; stopping after the current step", signum)
    stop_event.set()


class _ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: "\033[36m",
        logging.INFO: "\033[32m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[35m",
    }

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        color = self.COLORS.get(record.levelno, "")
        return f"{color}{text}\033[0m" if color else text


def configure_logging(config: Config) -> None:
    level = getattr(logging, config.logging.level.upper(), logging.INFO)
    config.paths.log_directory.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console = logging.StreamHandler()
    console.setFormatter(_ColorFormatter(formatter._fmt, datefmt=formatter.datefmt) if config.logging.console_colors else formatter)
    file_handler = logging.FileHandler(
        config.paths.log_directory / config.logging.file_name,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(console)
    root.addHandler(file_handler)


def build_uploaders(
    config: Config,
    state_store: StateStore,
    cancel_event: threading.Event | None = None,
) -> dict[str, BaseUploader]:
    uploaders: dict[str, BaseUploader] = {}
    if config.platforms.youtube.enabled:
        uploaders["youtube"] = YouTubeUploader(
            config.platforms.youtube,
            config.retry,
            state_store,
            cancel_event=cancel_event,
        )
    if config.platforms.cda.enabled:
        uploaders["cda"] = CDAUploader(
            config.platforms.cda,
            config.browser,
            config.retry,
            cancel_event=cancel_event,
        )
    if config.platforms.rumble.enabled:
        uploaders["rumble"] = RumbleUploader(
            config.platforms.rumble,
            config.browser,
            config.retry,
            cancel_event=cancel_event,
        )
    return uploaders


def _video_path_from_metadata(metadata: StreamMetadata) -> Path:
    suffix = "_meta.txt"
    name = metadata.source_path.name
    if not name.endswith(suffix):
        raise ValueError(f"Invalid metadata filename: {metadata.source_path}")
    return metadata.source_path.with_name(f"{name[:-len(suffix)]}.mkv")


def _srt_path(video_path: Path) -> Path | None:
    path = video_path.with_name(f"{video_path.stem}_chat.srt")
    return path if path.is_file() and path.stat().st_size > 0 else None


def _format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def build_description(metadata: StreamMetadata, duration_seconds: float) -> str:
    lines = [
        f"Stream recording: {metadata.channel}",
        f"Started: {metadata.started.isoformat(sep=' ')}",
        f"Duration: {_format_duration(duration_seconds)}",
    ]
    if metadata.game:
        lines.append(f"Game/category: {metadata.game}")
    return "\n".join(lines)


def build_part_description(
    metadata: StreamMetadata,
    source_duration_seconds: float,
    part_index: int,
    total_parts: int,
    start_seconds: float,
    end_seconds: float,
) -> str:
    return "\n".join(
        (
            build_description(metadata, source_duration_seconds),
            f"Part: {part_index}/{total_parts}",
            (
                f"Source time range: {_format_duration(start_seconds)} - "
                f"{_format_duration(end_seconds)}"
            ),
        )
    )


def build_tags(metadata: StreamMetadata) -> list[str]:
    tags = [metadata.channel, "Twitch", "stream"]
    if metadata.game:
        tags.append(metadata.game)
    return tags


def _platform_limit(config: Config, platform: str) -> float | None:
    return getattr(config.platforms, platform).max_duration_hours


def _platform_title_limit(config: Config, platform: str) -> int | None:
    return getattr(config.platforms, platform).title_limit


def _decimal_gigabytes(value: float) -> int:
    return int(value * 1_000_000_000)


def _requires_multipart(
    video_path: Path,
    duration_seconds: float,
    config: Config,
    platform: str,
) -> bool:
    if not config.splitting.enabled:
        return False
    size = video_path.stat().st_size
    if platform == "youtube":
        youtube = config.platforms.youtube
        return (
            duration_seconds > youtube.max_duration_hours * 3600
            or size > _decimal_gigabytes(youtube.max_file_size_gb)
        )
    if platform == "rumble":
        limit = config.platforms.rumble.max_file_size_gb
        return limit is not None and size > _decimal_gigabytes(limit)
    return False


def _split_constraints(config: Config, platform: str) -> SplitConstraints:
    if platform == "youtube":
        return SplitConstraints(
            hard_max_duration_seconds=(
                config.platforms.youtube.max_duration_hours * 3600
            ),
            target_duration_seconds=(
                config.splitting.youtube_target_duration_hours * 3600
            ),
            hard_max_size_bytes=_decimal_gigabytes(
                config.platforms.youtube.max_file_size_gb
            ),
            target_size_bytes=_decimal_gigabytes(
                config.splitting.youtube_target_size_gb
            ),
        )
    if platform == "rumble":
        hard_limit = config.platforms.rumble.max_file_size_gb
        if hard_limit is None:
            raise ValueError("Rumble max_file_size_gb is not configured")
        return SplitConstraints(
            hard_max_size_bytes=_decimal_gigabytes(hard_limit),
            target_size_bytes=_decimal_gigabytes(
                config.splitting.rumble_target_size_gb
            ),
        )
    raise ValueError(f"Multipart upload is not supported for {platform}")


def _mark_exception_failed(
    state_store: StateStore,
    video_path: Path,
    platform: str,
    exc: Exception,
) -> None:
    message = f"Unexpected uploader error: {exc}"
    try:
        state_store.mark_failed(video_path, platform, message)
    except Exception:
        logger.exception("Could not store FAILED status for %s/%s", video_path, platform)
    logger.exception("%s: %s", platform, message)


def _youtube_finalization_complete(
    video_path: Path,
    metadata: StreamMetadata,
    srt_path: Path,
    config: Config,
    state_store: StateStore,
    uploader: BaseUploader,
    *,
    retry_missing: bool,
) -> bool:
    """Finish captions/playlist without ever re-uploading a successful video."""
    record = state_store.get_status(video_path, "youtube")
    if record is None or record.status is not UploadStatus.SUCCESS:
        return False
    video_id = record.platform_video_id or ""
    if not video_id:
        logger.error(
            "youtube: SUCCESS has no video ID; captions and playlist cannot be finalized"
        )
        return False

    captions_required = getattr(uploader, "captions_required", None)
    upload_captions = getattr(uploader, "upload_captions", None)
    needs_captions = bool(
        callable(captions_required) and captions_required(srt_path)
    )
    if needs_captions and not record.captions_uploaded:
        if not retry_missing:
            return False
        if not callable(upload_captions):
            logger.error("youtube: uploader cannot retry missing captions")
            return False
        caption_result = upload_captions(video_id, srt_path)
        if not caption_result.success:
            logger.error(
                "youtube: video is uploaded, but captions still require retry: %s",
                caption_result.error_message or "unknown error",
            )
            return False
        state_store.mark_captions_uploaded(video_path, "youtube")
        record = state_store.get_status(video_path, "youtube")

    playlist_configured = metadata.channel in config.platforms.youtube.playlists
    if playlist_configured and record is not None and not record.playlist_added:
        if not retry_missing:
            return False
        playlist_id = config.platforms.youtube.playlists[metadata.channel]
        if not uploader.add_to_playlist(
            video_id,
            playlist_id,
            playlist_title=metadata.channel,
        ):
            logger.error(
                "youtube: video is uploaded, but adding it to the playlist "
                "still requires retry"
            )
            return False
        state_store.mark_playlist_added(video_path, "youtube")

    return True


def _youtube_part_finalization_complete(
    video_path: Path,
    metadata: StreamMetadata,
    record: UploadPartStatusRecord,
    config: Config,
    state_store: StateStore,
    uploader: BaseUploader,
    *,
    retry_missing: bool,
) -> tuple[bool, str | None]:
    video_id = record.platform_video_id or ""
    if not video_id:
        return False, f"YouTube part {record.part_index} has no video ID"

    captions_required = getattr(uploader, "captions_required", None)
    upload_captions = getattr(uploader, "upload_captions", None)
    needs_captions = bool(
        record.srt_path
        and callable(captions_required)
        and captions_required(record.srt_path)
    )
    if needs_captions and not record.captions_uploaded:
        if not retry_missing:
            return False, "YouTube part captions require a later retry"
        if not callable(upload_captions):
            return False, "YouTube uploader cannot retry multipart captions"
        caption_result = upload_captions(video_id, record.srt_path)
        if not caption_result.success:
            return (
                False,
                caption_result.error_message
                or f"Captions failed for YouTube part {record.part_index}",
            )
        state_store.mark_part_captions_uploaded(
            video_path,
            "youtube",
            record.part_index,
        )
        refreshed = state_store.get_part_status(
            video_path,
            "youtube",
            record.part_index,
        )
        if refreshed is not None:
            record = refreshed

    playlist_configured = metadata.channel in config.platforms.youtube.playlists
    if playlist_configured and not record.playlist_added:
        if not retry_missing:
            return False, "YouTube part playlist addition requires a later retry"
        playlist_id = config.platforms.youtube.playlists[metadata.channel]
        if not uploader.add_to_playlist(
            video_id,
            playlist_id,
            playlist_title=metadata.channel,
        ):
            return (
                False,
                f"Could not add YouTube part {record.part_index} to playlist",
            )
        state_store.mark_part_playlist_added(
            video_path,
            "youtube",
            record.part_index,
        )
    return True, None


def _multipart_specs(plan: SplitPlan) -> list[UploadPartSpec]:
    total = len(plan.parts)
    return [
        UploadPartSpec(
            index=part.index,
            total_parts=total,
            part_path=part.path,
            srt_path=part.srt_path,
            start_seconds=part.start_seconds,
            end_seconds=part.end_seconds,
        )
        for part in plan.parts
    ]


def _multipart_parent_identifier(
    records: Sequence[UploadPartStatusRecord],
) -> str:
    return json.dumps(
        [
            {
                "part": record.part_index,
                "id_or_url": record.platform_video_id,
            }
            for record in records
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _process_multipart_platform(
    video_path: Path,
    metadata: StreamMetadata,
    duration_seconds: float,
    config: Config,
    state_store: StateStore,
    platform: str,
    uploader: BaseUploader,
    splitter: MediaSplitter,
    srt_path: Path | None,
) -> tuple[bool, SplitPlan]:
    """Create/reuse parts and upload only unfinished ones for one platform."""
    plan = splitter.create_plan(
        video_path,
        platform,
        duration_seconds,
        _split_constraints(config, platform),
        srt_path=srt_path,
    )
    state_store.sync_upload_parts(
        video_path,
        platform,
        _multipart_specs(plan),
    )
    parent = state_store.get_status(video_path, platform)
    if parent is not None and parent.status is UploadStatus.SKIPPED:
        state_store.reopen_for_multipart(video_path, platform)

    total = len(plan.parts)
    failure_message: str | None = None
    for part in plan.parts:
        record = state_store.get_part_status(
            video_path,
            platform,
            part.index,
        )
        if record is None:  # pragma: no cover - sync safeguard
            raise RuntimeError(f"Missing state for {platform} part {part.index}")

        if record.status is UploadStatus.SUCCESS:
            if platform == "youtube":
                complete, error = _youtube_part_finalization_complete(
                    video_path,
                    metadata,
                    record,
                    config,
                    state_store,
                    uploader,
                    retry_missing=True,
                )
                if not complete:
                    failure_message = error
                    logger.error(
                        "youtube: part %d/%d finalization failed: %s",
                        part.index,
                        total,
                        error,
                    )
            continue
        if record.status is UploadStatus.SKIPPED:
            continue
        if (
            record.status is UploadStatus.FAILED
            and (record.last_error or "").startswith(NO_AUTO_RETRY_PREFIX)
        ):
            logger.warning(
                "%s: part %d/%d requires manual verification; not retrying: %s",
                platform,
                part.index,
                total,
                record.last_error,
            )
            continue

        title = title_from_metadata_part(
            metadata,
            config.metadata.title_template,
            _platform_title_limit(config, platform),
            part.index,
            total,
        )
        description = build_part_description(
            metadata,
            duration_seconds,
            part.index,
            total,
            part.start_seconds,
            part.end_seconds,
        )
        logger.info(
            "%s: processing part %d/%d (%s, %d bytes, %s-%s)",
            platform,
            part.index,
            total,
            part.path,
            part.size_bytes,
            _format_duration(part.start_seconds),
            _format_duration(part.end_seconds),
        )
        state_store.mark_part_in_progress(
            video_path,
            platform,
            part.index,
        )
        result = uploader.upload(
            part.path,
            title,
            description,
            build_tags(metadata),
            part.srt_path,
        )
        if result.skipped:
            reason = (
                result.error_message
                or f"{platform} intentionally skipped part {part.index}"
            )
            state_store.mark_part_skipped(
                video_path,
                platform,
                part.index,
                reason,
            )
            logger.warning(
                "%s: part %d/%d skipped: %s",
                platform,
                part.index,
                total,
                reason,
            )
            continue
        if not result.success:
            error = result.error_message or "Uploader returned success=False"
            if not result.retry_allowed:
                error = f"{NO_AUTO_RETRY_PREFIX}{error}"
            state_store.mark_part_failed(
                video_path,
                platform,
                part.index,
                error,
            )
            failure_message = error
            logger.error(
                "%s: part %d/%d failed: %s",
                platform,
                part.index,
                total,
                error,
            )
            continue

        stored_identifier = (
            result.platform_url
            if platform in {"cda", "rumble"} and result.platform_url
            else result.platform_video_id
        )
        state_store.mark_part_success(
            video_path,
            platform,
            part.index,
            stored_identifier,
        )
        if result.captions_uploaded:
            state_store.mark_part_captions_uploaded(
                video_path,
                platform,
                part.index,
            )
        if (
            platform == "youtube"
            and metadata.channel in config.platforms.youtube.playlists
        ):
            playlist_id = config.platforms.youtube.playlists[metadata.channel]
            if uploader.add_to_playlist(
                result.platform_video_id or "",
                playlist_id,
                playlist_title=metadata.channel,
            ):
                state_store.mark_part_playlist_added(
                    video_path,
                    platform,
                    part.index,
                )
            else:
                failure_message = (
                    f"Could not add YouTube part {part.index} to playlist"
                )

    require_playlist = (
        platform == "youtube"
        and metadata.channel in config.platforms.youtube.playlists
    )
    complete = state_store.are_parts_fully_processed(
        video_path,
        platform,
        require_captions=platform == "youtube",
        require_playlist=require_playlist,
    )
    if not complete:
        if failure_message:
            current_parent = state_store.get_status(video_path, platform)
            if (
                current_parent is None
                or current_parent.status is not UploadStatus.SUCCESS
            ):
                state_store.mark_failed(
                    video_path,
                    platform,
                    failure_message,
                )
        return False, plan

    records = state_store.get_part_statuses(video_path, platform)
    state_store.mark_success(
        video_path,
        platform,
        _multipart_parent_identifier(records),
    )
    if platform == "youtube":
        nonempty_srt_records = [
            record
            for record in records
            if record.srt_path
            and record.srt_path.is_file()
            and record.srt_path.stat().st_size > 0
        ]
        if (
            nonempty_srt_records
            and all(record.captions_uploaded for record in nonempty_srt_records)
        ):
            state_store.mark_captions_uploaded(video_path, platform)
        if require_playlist:
            state_store.mark_playlist_added(video_path, platform)
    logger.info(
        "%s: all %d multipart uploads completed",
        platform,
        total,
    )
    return True, plan


def process_ready_recording(
    video_path: Path,
    metadata: StreamMetadata,
    duration_seconds: float,
    config: Config,
    state_store: StateStore,
    uploaders: Mapping[str, BaseUploader],
    *,
    media_splitter: MediaSplitter | None = None,
) -> None:
    """Process one ready recording while isolating each platform."""
    if (
        "cda" in uploaders
        and config.platforms.cda.normalize_filename
    ):
        normalized = normalize_recording_set_for_cda(
            video_path,
            state_store,
            max_stem_length=config.platforms.cda.filename_max_stem_length,
        )
        if normalized.renamed:
            video_path = normalized.video_path
            metadata = replace(metadata, source_path=normalized.metadata_path)

    required_platforms = list(uploaders)
    description = build_description(metadata, duration_seconds)
    tags = build_tags(metadata)
    srt_path = _srt_path(video_path)
    if media_splitter is None:
        cancel_event = next(
            (
                uploader.cancel_event
                for uploader in uploaders.values()
                if uploader.cancel_event is not None
            ),
            None,
        )
        media_splitter = MediaSplitter(
            ffmpeg_path=config.paths.ffmpeg,
            ffprobe_path=config.paths.ffprobe,
            work_directory_name=config.splitting.work_directory_name,
            max_replans=config.splitting.max_replans,
            disk_space_multiplier=config.splitting.disk_space_multiplier,
            cancel_event=cancel_event,
        )
    multipart_plans: list[SplitPlan] = []
    youtube_uploaded_this_cycle = False
    youtube_finalization_complete = True

    for platform, uploader in uploaders.items():
        try:
            current = state_store.get_status(video_path, platform)
            needs_multipart = _requires_multipart(
                video_path,
                duration_seconds,
                config,
                platform,
            )
            existing_parts = state_store.get_part_statuses(video_path, platform)
            if needs_multipart and (
                current is None
                or current.status is not UploadStatus.SUCCESS
                or bool(existing_parts)
            ):
                complete, plan = _process_multipart_platform(
                    video_path,
                    metadata,
                    duration_seconds,
                    config,
                    state_store,
                    platform,
                    uploader,
                    media_splitter,
                    srt_path,
                )
                multipart_plans.append(plan)
                if platform == "youtube" and not complete:
                    youtube_finalization_complete = False
                continue
            if current is not None and current.status is UploadStatus.SUCCESS:
                if platform == "youtube":
                    youtube_finalization_complete = _youtube_finalization_complete(
                        video_path,
                        metadata,
                        srt_path,
                        config,
                        state_store,
                        uploader,
                        retry_missing=True,
                    )
                logger.info("%s: skipping terminal status %s", platform, current.status.value)
                continue
            if current is not None and current.status is UploadStatus.SKIPPED:
                logger.info("%s: skipping terminal status %s", platform, current.status.value)
                continue
            if (
                platform == "rumble"
                and current is not None
                and current.status is UploadStatus.FAILED
                and _is_file_size_limit_error(current.last_error or "")
            ):
                reason = current.last_error or "Rumble file-size limit"
                state_store.mark_skipped(video_path, platform, reason)
                logger.warning(
                    "rumble: converted the previous file-size-limit failure to SKIPPED"
                )
                continue
            if (
                platform == "rumble"
                and current is not None
                and current.status is UploadStatus.FAILED
                and "the video file has no video track"
                in (current.last_error or "").casefold()
            ):
                reason = (
                    "Rumble skipped the recording because the uploaded file has "
                    "no video track"
                )
                state_store.mark_skipped(video_path, platform, reason)
                logger.warning(
                    "rumble: converted the previous no-video-track failure to SKIPPED"
                )
                continue
            if (
                current is not None
                and current.status is UploadStatus.FAILED
                and (current.last_error or "").startswith(NO_AUTO_RETRY_PREFIX)
            ):
                logger.warning(
                    "%s: the previous upload requires manual verification; "
                    "automatic retry is disabled: %s",
                    platform,
                    current.last_error,
                )
                continue

            limit = _platform_limit(config, platform)
            if limit is not None and exceeds_duration_limit(duration_seconds, limit):
                reason = f"Duration {_format_duration(duration_seconds)} exceeds the {limit:g} h limit"
                state_store.mark_skipped(video_path, platform, reason)
                logger.warning("%s: %s", platform, reason)
                continue

            title = title_from_metadata(
                metadata,
                config.metadata.title_template,
                _platform_title_limit(config, platform),
            )
            state_store.mark_in_progress(video_path, platform)
            result = uploader.upload(video_path, title, description, tags, srt_path)
            if result.skipped:
                reason = result.error_message or f"{platform} intentionally skipped the upload"
                state_store.mark_skipped(video_path, platform, reason)
                logger.warning("%s: upload skipped: %s", platform, reason)
                continue
            if not result.success:
                error = result.error_message or "Uploader returned success=False"
                if not result.retry_allowed:
                    error = f"{NO_AUTO_RETRY_PREFIX}{error}"
                state_store.mark_failed(video_path, platform, error)
                logger.error("%s: upload %s failed: %s", platform, video_path, error)
                continue

            stored_identifier = (
                result.platform_url
                if platform in {"cda", "rumble"} and result.platform_url
                else result.platform_video_id
            )
            state_store.mark_success(video_path, platform, stored_identifier)
            logger.info(
                "%s: upload completed successfully; stored identifier/URL: %s",
                platform,
                stored_identifier,
            )
            if result.captions_uploaded:
                state_store.mark_captions_uploaded(video_path, platform)

            if platform == "youtube" and metadata.channel in config.platforms.youtube.playlists:
                youtube_uploaded_this_cycle = True
                playlist_id = config.platforms.youtube.playlists[metadata.channel]
                if uploader.add_to_playlist(
                    result.platform_video_id or "",
                    playlist_id,
                    playlist_title=metadata.channel,
                ):
                    state_store.mark_playlist_added(video_path, platform)
            elif platform == "youtube":
                youtube_uploaded_this_cycle = True
        except Exception as exc:
            _mark_exception_failed(state_store, video_path, platform, exc)

    if "youtube" in uploaders and youtube_uploaded_this_cycle:
        youtube_finalization_complete = _youtube_finalization_complete(
            video_path,
            metadata,
            srt_path,
            config,
            state_store,
            uploaders["youtube"],
            retry_missing=False,
        )
    if not youtube_finalization_complete:
        logger.warning(
            "youtube: keeping source files until captions and playlist "
            "finalization succeeds"
        )
        return

    move_result = move_processed_recording(
        video_path, config, state_store, required_platforms
    )
    if move_result.moved:
        logger.info("Moved processed recording to %s", move_result.destination)
        if not config.splitting.keep_parts_after_success:
            for plan in multipart_plans:
                media_splitter.cleanup(plan)
                logger.info(
                    "Removed completed %s split work files from %s",
                    plan.platform,
                    plan.work_directory,
                )
    for warning in move_result.warnings:
        logger.warning("Mover: %s", warning)


def process_readiness_results(
    results: Sequence[ReadinessResult],
    config: Config,
    state_store: StateStore,
    uploaders: Mapping[str, BaseUploader],
    *,
    duration_probe: DurationProbe | None = None,
) -> None:
    probe = duration_probe or (
        lambda path: probe_duration_seconds(path, ffprobe_path=config.paths.ffprobe)
    )
    for result in results:
        if result.status is not ReadinessStatus.READY or result.metadata is None:
            continue
        try:
            video_path = _video_path_from_metadata(result.metadata)
            duration = probe(video_path)
            process_ready_recording(
                video_path, result.metadata, duration, config, state_store, uploaders
            )
        except Exception:
            logger.exception("Unexpected error while processing a ready recording")


def run_cycle(
    config: Config,
    state_store: StateStore,
    tracker: FileSizeStabilityTracker,
    uploaders: Mapping[str, BaseUploader],
    *,
    now: float | None = None,
    duration_probe: DurationProbe | None = None,
) -> list[ReadinessResult]:
    results = scan_cycle(
        config.paths.recordings_root,
        tracker,
        config.moving.uploaded_directory_name,
        excluded_directory_names=[config.splitting.work_directory_name],
        now=now,
    )
    process_readiness_results(
        results, config, state_store, uploaders, duration_probe=duration_probe
    )
    return results


def run(config: Config, *, once: bool = False) -> int:
    configure_logging(config)
    stop_event = threading.Event()

    for signal_name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, signal_name):
            signal.signal(
                getattr(signal, signal_name),
                lambda signum, frame: _request_stop(stop_event, signum, frame),
            )

    with StateStore(config.paths.database) as store:
        uploaders = build_uploaders(config, store, stop_event)
        tracker = FileSizeStabilityTracker(config.watcher.size_stability_seconds)
        try:
            while not stop_event.is_set():
                try:
                    run_cycle(config, store, tracker, uploaders)
                except Exception:
                    # A complete scan failure (for example a temporary disk error)
                    # must not terminate the long-running process.
                    logger.exception("Unexpected cycle error; will retry")
                if once:
                    break
                stop_event.wait(config.watcher.poll_interval_seconds)
        except KeyboardInterrupt:
            logger.info("Interrupted by the user")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--browser-debug",
        action="store_true",
        help="Show the Playwright window and capture diagnostic traces/screenshots",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    if args.browser_debug:
        config = replace(
            config,
            browser=replace(config.browser, debug=True, headless=False),
        )
    return run(config, once=args.once)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
