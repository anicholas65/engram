"""Encode profiles, the encode queue, and encode settings.

Engram's pipeline stops at a lossless MakeMKV remux filed into the library. The
encode stage runs *after* that, off the job pipeline: a COMPLETED job enqueues
one ``EncodeTask`` per organized file, and ``EncodeQueue`` re-encodes the video
in place. Audio and subtitles are always copied untouched and the container is
always Matroska, so a profile only describes the video (plus an optional extra
stereo compatibility track).

Everything lives in its own tables so the upstream ``disc_jobs`` /
``app_config`` schemas stay untouched and fork merges stay cheap.
"""

from datetime import UTC, datetime
from enum import StrEnum

from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(UTC)


class VideoEncoder(StrEnum):
    """ffmpeg video encoders a profile may use."""

    COPY = "copy"  # Archive: no video encode at all
    HEVC_NVENC = "hevc_nvenc"
    H264_NVENC = "h264_nvenc"
    LIBX265 = "libx265"
    LIBX264 = "libx264"
    LIBSVTAV1 = "libsvtav1"  # CPU only: Turing (RTX 20xx) has no AV1 encoder


HARDWARE_ENCODERS = frozenset({VideoEncoder.HEVC_NVENC, VideoEncoder.H264_NVENC})


class RateControl(StrEnum):
    """How the video bitrate is decided."""

    BITRATE = "bitrate"  # Target average bitrate (VBR, optional multipass)
    QUALITY = "quality"  # Constant quality (CRF for software, CQ for NVENC)


class EncodeTaskState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    VERIFYING = "verifying"
    DONE = "done"
    SKIPPED = "skipped"  # Nothing to gain (e.g. source already below the target bitrate)
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_TASK_STATES = frozenset({EncodeTaskState.RUNNING, EncodeTaskState.VERIFYING})
FINISHED_TASK_STATES = frozenset(
    {
        EncodeTaskState.DONE,
        EncodeTaskState.SKIPPED,
        EncodeTaskState.FAILED,
        EncodeTaskState.CANCELLED,
    }
)


class EncodeProfile(SQLModel, table=True):
    """A named video encode recipe. Audio/subtitles are always passed through."""

    __tablename__ = "encode_profiles"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    description: str = ""
    builtin: bool = False  # Seeded starter profile (editable, re-seeded only if missing)

    encoder: VideoEncoder = VideoEncoder.HEVC_NVENC
    rate_control: RateControl = RateControl.BITRATE
    # No non-None defaults on nullable columns: SQLAlchemy treats an explicit None
    # as "unset" and would apply the default, turning a CQ profile into 60 Mbps.
    video_bitrate_kbps: int | None = None  # BITRATE mode target
    max_bitrate_kbps: int | None = None  # Peak cap (VBV maxrate); None = uncapped
    quality: int | None = None  # QUALITY mode: CRF / CQ value
    multipass: bool = True  # NVENC "fullres" multipass / x26x 2-pass
    preset: str = "p7"  # NVENC p1-p7, or x264/x265/svt preset name/number
    ten_bit: bool = True  # Encode 10-bit (required to keep HDR10)
    max_height: int | None = None  # Downscale above this height (e.g. 1080); None = keep

    add_stereo_aac: bool = False  # Extra AAC 2.0 track next to the untouched originals
    stereo_aac_kbps: int = 192
    audio_languages: str = ""  # CSV of ISO 639-2 codes to keep; "" keeps every track

    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class EncodeTask(SQLModel, table=True):
    """One file waiting for, undergoing, or finished with an encode."""

    __tablename__ = "encode_tasks"

    id: int | None = Field(default=None, primary_key=True)
    job_id: int | None = Field(default=None, foreign_key="disc_jobs.id", index=True)
    title_id: int | None = Field(default=None, foreign_key="disc_titles.id")
    source_path: str
    profile_id: int = Field(foreign_key="encode_profiles.id")
    profile_name: str = ""  # Snapshot for display if the profile is later deleted

    state: EncodeTaskState = Field(default=EncodeTaskState.QUEUED, index=True)
    progress: float = 0.0  # 0-100
    fps: float | None = None
    speed: float | None = None  # Multiple of realtime
    eta_seconds: int | None = None
    duration_seconds: float | None = None
    # Bitrates (kbps) for display. "current" is the live video rate while encoding,
    # "output" the measured video rate of the finished file.
    encoder: str | None = None  # ffmpeg encoder actually used (e.g. hevc_nvenc)
    source_video_kbps: int | None = None
    target_kbps: int | None = None  # None for constant-quality profiles
    peak_kbps: int | None = None
    quality: int | None = None  # CQ/CRF for constant-quality profiles
    current_kbps: int | None = None
    output_video_kbps: int | None = None

    source_bytes: int | None = None
    output_bytes: int | None = None
    error: str | None = None
    note: str | None = None  # Human-readable outcome detail (skip reason, archive path)
    command: str | None = None  # Exact ffmpeg invocation, for diagnostics

    created_at: datetime = Field(default_factory=_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class EncodeSettings(SQLModel, table=True):
    """Single-row settings for the encode stage."""

    __tablename__ = "encode_settings"

    id: int | None = Field(default=None, primary_key=True)
    # Auto-enqueue organized files when a job completes. On by default in the fork:
    # with the default "4K UHD High" profile only sources above ~66 Mbps are re-encoded.
    enabled: bool = True
    movie_profile_id: int | None = None  # 1080p and below
    movie_4k_profile_id: int | None = None  # Sources taller than 1440 lines
    tv_profile_id: int | None = None
    include_extras: bool = True
    # Simultaneous encodes. Turing has one NVENC engine, so parallel encodes share it
    # (and Jellyfin's transcodes) rather than finishing sooner.
    concurrency: int = 1
    keep_original: bool = False  # Move the remux to archive_path instead of deleting it
    archive_path: str = ""
    # Skip a file whose source video is already at or below this fraction of the
    # target bitrate: re-encoding a 30 Mbps Blu-ray at 60 Mbps only wastes space.
    skip_if_source_below_ratio: float = 1.1
