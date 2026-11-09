"""REST API for the encode stage: options, profiles, settings, and the queue."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlmodel import select

from app import database
from app.core import encoder
from app.models.encode import (
    HARDWARE_ENCODERS,
    EncodeProfile,
    EncodeSettings,
    EncodeTask,
    EncodeTaskState,
    RateControl,
    VideoEncoder,
)
from app.services.encode_queue import encode_queue, get_settings, resolve_ffmpeg, task_dict

router = APIRouter(prefix="/api/encode", tags=["encode"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = ""
    encoder: VideoEncoder = VideoEncoder.HEVC_NVENC
    rate_control: RateControl = RateControl.BITRATE
    video_bitrate_kbps: int | None = Field(default=None, ge=100, le=400_000)
    max_bitrate_kbps: int | None = Field(default=None, ge=100, le=400_000)
    quality: int | None = Field(default=None, ge=0, le=63)
    multipass: bool = True
    preset: str = "p7"
    ten_bit: bool = True
    max_height: int | None = Field(default=None, ge=240, le=4320)
    add_stereo_aac: bool = False
    stereo_aac_kbps: int = Field(default=192, ge=64, le=512)
    audio_languages: str = ""

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def _consistent(self) -> ProfileIn:
        if self.encoder == VideoEncoder.COPY:
            return self
        if self.rate_control == RateControl.BITRATE and not self.video_bitrate_kbps:
            raise ValueError("bitrate mode needs video_bitrate_kbps")
        if self.rate_control == RateControl.QUALITY and self.quality is None:
            raise ValueError("quality mode needs quality")
        if (
            self.max_bitrate_kbps
            and self.video_bitrate_kbps
            and self.rate_control == RateControl.BITRATE
            and self.max_bitrate_kbps < self.video_bitrate_kbps
        ):
            raise ValueError("max_bitrate_kbps must be at least video_bitrate_kbps")
        valid = (
            encoder.NVENC_PRESETS
            if self.encoder in HARDWARE_ENCODERS
            else encoder.SVT_PRESETS
            if self.encoder == VideoEncoder.LIBSVTAV1
            else encoder.X26X_PRESETS
        )
        if self.preset not in valid:
            raise ValueError(f"preset must be one of {valid}")
        return self


class SettingsIn(BaseModel):
    enabled: bool
    movie_profile_id: int | None = None
    movie_4k_profile_id: int | None = None
    tv_profile_id: int | None = None
    include_extras: bool = True
    concurrency: int = Field(default=2, ge=1, le=8)
    keep_original: bool = False
    archive_path: str = ""
    skip_if_source_below_ratio: float = Field(default=1.1, ge=0, le=10)


class EnqueueIn(BaseModel):
    path: str | None = None
    job_id: int | None = None
    profile_id: int | None = None

    @model_validator(mode="after")
    def _one(self) -> EnqueueIn:
        if bool(self.path) == bool(self.job_id):
            raise ValueError("give exactly one of path or job_id")
        if self.path and not self.profile_id:
            raise ValueError("a path needs a profile_id")
        return self


# ---------------------------------------------------------------------------
# Options / hardware
# ---------------------------------------------------------------------------

_nvenc_cache: dict | None = None


async def _probe_nvenc(ffmpeg: str) -> dict:
    """Try a tiny real encode with each NVENC encoder (listing alone proves nothing)."""
    results: dict[str, bool] = {}
    for enc in ("hevc_nvenc", "h264_nvenc"):
        proc = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=256x144:d=0.2",
            "-c:v",
            enc,
            "-f",
            "null",
            "-",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=20)
            results[enc] = proc.returncode == 0
        except TimeoutError:
            proc.kill()
            results[enc] = False
    return results


@router.get("/options")
async def get_options(refresh_hardware: bool = False) -> dict:
    global _nvenc_cache
    ffmpeg = await resolve_ffmpeg()
    if ffmpeg and (_nvenc_cache is None or refresh_hardware):
        _nvenc_cache = await _probe_nvenc(ffmpeg)
    return {
        "ffmpeg": ffmpeg,
        "hardware": _nvenc_cache or {},
        "encoders": [
            {
                "id": e.value,
                "family": encoder.ENCODER_FAMILY.get(e, "copy"),
                "hardware": e in HARDWARE_ENCODERS,
            }
            for e in VideoEncoder
        ],
        "video_bitrate_presets_mbps": encoder.VIDEO_BITRATE_PRESETS_MBPS,
        "max_bitrate_presets_mbps": encoder.MAX_BITRATE_PRESETS_MBPS,
        "quality_range": encoder.QUALITY_RANGE,
        "stereo_aac_kbps_presets": encoder.STEREO_AAC_KBPS_PRESETS,
        "presets": {
            "nvenc": encoder.NVENC_PRESETS,
            "x26x": encoder.X26X_PRESETS,
            "svtav1": encoder.SVT_PRESETS,
        },
    }


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


@router.get("/profiles")
async def list_profiles() -> list[EncodeProfile]:
    async with database.async_session() as session:
        rows = await session.execute(select(EncodeProfile).order_by(EncodeProfile.id))
        return list(rows.scalars())


async def _name_taken(session, name: str, exclude_id: int | None = None) -> bool:
    row = (
        (await session.execute(select(EncodeProfile).where(EncodeProfile.name == name)))
        .scalars()
        .first()
    )
    return row is not None and row.id != exclude_id


@router.post("/profiles", status_code=201)
async def create_profile(body: ProfileIn) -> EncodeProfile:
    async with database.async_session() as session:
        if await _name_taken(session, body.name):
            raise HTTPException(409, "A profile with that name already exists")
        profile = EncodeProfile(**body.model_dump())
        session.add(profile)
        await session.commit()
        await session.refresh(profile)
        return profile


@router.put("/profiles/{profile_id}")
async def update_profile(profile_id: int, body: ProfileIn) -> EncodeProfile:
    async with database.async_session() as session:
        profile = await session.get(EncodeProfile, profile_id)
        if profile is None:
            raise HTTPException(404, "Profile not found")
        if await _name_taken(session, body.name, exclude_id=profile_id):
            raise HTTPException(409, "A profile with that name already exists")
        for key, value in body.model_dump().items():
            setattr(profile, key, value)
        profile.updated_at = datetime.now(UTC)
        session.add(profile)
        await session.commit()
        await session.refresh(profile)
        return profile


@router.delete("/profiles/{profile_id}", status_code=204)
async def delete_profile(profile_id: int) -> None:
    async with database.async_session() as session:
        profile = await session.get(EncodeProfile, profile_id)
        if profile is None:
            raise HTTPException(404, "Profile not found")
        settings = (await session.execute(select(EncodeSettings).limit(1))).scalars().first()
        if settings and profile_id in (
            settings.movie_profile_id,
            settings.movie_4k_profile_id,
            settings.tv_profile_id,
        ):
            raise HTTPException(409, "Profile is a default in Encode settings")
        queued = (
            (
                await session.execute(
                    select(EncodeTask).where(
                        EncodeTask.profile_id == profile_id,
                        EncodeTask.state.in_(
                            [
                                EncodeTaskState.QUEUED,
                                EncodeTaskState.RUNNING,
                                EncodeTaskState.VERIFYING,
                            ]
                        ),
                    )
                )
            )
            .scalars()
            .first()
        )
        if queued:
            raise HTTPException(409, "Profile has queued or running encodes")
        await session.delete(profile)
        await session.commit()


@router.post("/profiles/{profile_id}/estimate")
async def estimate(profile_id: int, duration_minutes: float = 120) -> dict:
    """Rough output size for a given runtime (bitrate profiles only)."""
    async with database.async_session() as session:
        profile = await session.get(EncodeProfile, profile_id)
    if profile is None:
        raise HTTPException(404, "Profile not found")
    if profile.rate_control != RateControl.BITRATE or not profile.video_bitrate_kbps:
        return {"bytes": None, "note": "Constant-quality size depends on the content"}
    video = profile.video_bitrate_kbps * 1000 * duration_minutes * 60 / 8
    return {"bytes": int(video), "note": "Video only; original audio adds roughly 2-6 GB"}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@router.get("/settings")
async def read_settings() -> EncodeSettings:
    return await get_settings()


@router.put("/settings")
async def write_settings(body: SettingsIn) -> EncodeSettings:
    async with database.async_session() as session:
        for pid in (body.movie_profile_id, body.movie_4k_profile_id, body.tv_profile_id):
            if pid is not None and await session.get(EncodeProfile, pid) is None:
                raise HTTPException(422, f"Profile {pid} does not exist")
        settings = (await session.execute(select(EncodeSettings).limit(1))).scalars().first()
        if settings is None:
            settings = EncodeSettings()
        for key, value in body.model_dump().items():
            setattr(settings, key, value)
        session.add(settings)
        await session.commit()
        await session.refresh(settings)
    encode_queue.wake()  # concurrency may have grown
    return settings


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


@router.get("/tasks")
async def list_tasks(limit: int = 200) -> list[dict]:
    async with database.async_session() as session:
        rows = await session.execute(
            select(EncodeTask).order_by(EncodeTask.id.desc()).limit(min(limit, 1000))
        )
        return [task_dict(t) for t in rows.scalars()]


@router.post("/tasks", status_code=201)
async def enqueue(body: EnqueueIn) -> list[dict]:
    try:
        if body.job_id:
            tasks = await encode_queue.enqueue_job(body.job_id, force=True)
        else:
            task = await encode_queue.enqueue_path(body.path, body.profile_id)
            tasks = [task] if task else []
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return [task_dict(t) for t in tasks]


@router.post("/tasks/{task_id}/cancel")
async def cancel(task_id: int) -> dict:
    if not await encode_queue.cancel(task_id):
        raise HTTPException(409, "Task is not queued or running")
    return {"ok": True}


@router.post("/tasks/{task_id}/retry")
async def retry(task_id: int) -> dict:
    if not await encode_queue.retry(task_id):
        raise HTTPException(409, "Only failed or cancelled tasks can be retried")
    return {"ok": True}


@router.delete("/tasks/finished")
async def clear_finished() -> dict:
    return {"removed": await encode_queue.clear_finished()}
