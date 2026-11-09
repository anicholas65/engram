"""The encode queue: re-encodes organized library files off the job pipeline.

A disc job is finished (COMPLETED) as soon as its lossless remuxes are filed, so
the drive is free for the next disc. When encoding is enabled, the COMPLETED hook
enqueues one ``EncodeTask`` per organized file and this worker encodes them in the
background with ``concurrency`` parallel ffmpeg processes.

Each encode writes a hidden temp file beside the original (``.<name>.encoding.mkv``,
which Jellyfin/Plex ignore), verifies it with ffprobe, and only then swaps it in
under the original name. The original is deleted, or moved to the archive folder
when ``keep_original`` is set. A failed or cancelled encode never touches the
original.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import shutil
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger
from sqlmodel import select

from app import database
from app.api.websocket import manager as ws_manager
from app.core import encoder
from app.core.security import is_within_configured_roots
from app.models.disc_job import ContentType, DiscJob, DiscTitle, JobState, TitleState
from app.models.encode import (
    ACTIVE_TASK_STATES,
    FINISHED_TASK_STATES,
    EncodeProfile,
    EncodeSettings,
    EncodeTask,
    EncodeTaskState,
)

PROGRESS_INTERVAL_SECONDS = 1.0
FREE_SPACE_MARGIN_BYTES = 1 << 30  # Leave 1 GiB on the volume after the temp file
TEMP_SUFFIX = ".encoding.mkv"


def _now() -> datetime:
    return datetime.now(UTC)


def temp_path_for(source: Path) -> Path:
    return source.with_name(f".{source.stem}{TEMP_SUFFIX}")


def archive_relpath(source: Path, roots: list[str]) -> Path:
    """Where a file sits relative to its library root (``Show/Season 01/x.mkv``).

    The archive mirrors the library layout so two shows' ``Season 01`` folders
    never collide. A file outside every root keeps just its parent folder.
    """
    resolved = source.resolve()
    for root in roots:
        if not root:
            continue
        try:
            rel = resolved.relative_to(Path(root).resolve())
        except ValueError:
            continue
        return Path(Path(root).name) / rel
    return Path(source.parent.name) / source.name


def task_dict(task: EncodeTask) -> dict:
    data = task.model_dump(mode="json")
    data["filename"] = Path(task.source_path).name
    return data


async def resolve_ffmpeg() -> str | None:
    from app.services.config_service import get_config

    config = await get_config()
    configured = (config.ffmpeg_path or "").strip()
    if configured and Path(configured).exists():
        return configured
    return shutil.which("ffmpeg")


async def library_roots() -> list[str]:
    from app.services.config_service import get_config

    config = await get_config()
    return [config.library_movies_path, config.library_tv_path]


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


async def seed_defaults() -> None:
    """Insert any missing starter profile, and the settings row on first run."""
    async with database.async_session() as session:
        existing = {p.name: p for p in (await session.execute(select(EncodeProfile))).scalars()}
        for spec in encoder.BUILTIN_PROFILES:
            if spec["name"] not in existing:
                profile = EncodeProfile(builtin=True, **spec)
                session.add(profile)
                existing[spec["name"]] = profile
        await session.flush()

        settings = (await session.execute(select(EncodeSettings).limit(1))).scalars().first()
        if settings is None:
            settings = EncodeSettings()
            for field_name, profile_name in encoder.DEFAULT_ASSIGNMENT.items():
                if profile_name in existing:
                    setattr(settings, field_name, existing[profile_name].id)
            session.add(settings)
        await session.commit()


async def get_settings() -> EncodeSettings:
    async with database.async_session() as session:
        settings = (await session.execute(select(EncodeSettings).limit(1))).scalars().first()
        if settings is None:
            await seed_defaults()
            settings = (await session.execute(select(EncodeSettings).limit(1))).scalars().first()
        return settings


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


class EncodeQueue:
    def __init__(self) -> None:
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task | None = None
        self._running: dict[int, asyncio.Task] = {}
        self._procs: dict[int, asyncio.subprocess.Process] = {}
        self._cancel_requested: set[int] = set()
        self._hook_registered = False

    # --- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        await seed_defaults()
        await self._recover_interrupted()
        if not self._hook_registered:
            from app.services.job_manager import state_machine

            state_machine.on_terminal_state(self.on_job_terminal)
            self._hook_registered = True
        self._loop_task = asyncio.create_task(self._run_loop(), name="encode-queue")
        logger.info("Encode queue started")

    async def stop(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
            self._loop_task = None
        for proc in list(self._procs.values()):
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
        for task in list(self._running.values()):
            task.cancel()
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)
        # Interrupted tasks stay RUNNING in the DB; the next start() re-queues them.

    async def _recover_interrupted(self) -> None:
        """Re-queue encodes a restart interrupted and remove their temp files."""
        async with database.async_session() as session:
            rows = (
                await session.execute(
                    select(EncodeTask).where(EncodeTask.state.in_(list(ACTIVE_TASK_STATES)))
                )
            ).scalars()
            for task in rows:
                with contextlib.suppress(OSError):
                    temp_path_for(Path(task.source_path)).unlink(missing_ok=True)
                task.state = EncodeTaskState.QUEUED
                task.progress = 0.0
                task.note = "Re-queued after restart"
                session.add(task)
            await session.commit()

    def wake(self) -> None:
        self._wake.set()

    # --- job hook ---------------------------------------------------------

    async def on_job_terminal(self, job_id: int, state: JobState) -> None:
        """JobStateMachine on_terminal_state hook: enqueue a completed job's files."""
        if state != JobState.COMPLETED:
            return
        try:
            await self.enqueue_job(job_id)
        except Exception as e:  # never break terminal-state dispatch
            logger.warning(f"Job {job_id}: encode enqueue failed: {e}", exc_info=True)

    async def enqueue_job(self, job_id: int, *, force: bool = False) -> list[EncodeTask]:
        settings = await get_settings()
        if not settings.enabled and not force:
            return []
        async with database.async_session() as session:
            job = await session.get(DiscJob, job_id)
            if job is None:
                return []
            titles = (
                (await session.execute(select(DiscTitle).where(DiscTitle.job_id == job_id)))
                .scalars()
                .all()
            )
            content_type = job.content_type

        ffmpeg = await resolve_ffmpeg()
        ffprobe = encoder.ffprobe_for(ffmpeg) if ffmpeg else None
        created: list[EncodeTask] = []
        for title in titles:
            if not title.organized_to or title.state != TitleState.COMPLETED:
                continue
            if title.is_extra and not settings.include_extras:
                continue
            profile_id = await self._profile_for(
                settings, content_type, Path(title.organized_to), ffprobe
            )
            if profile_id is None:
                continue
            task = await self.enqueue_path(
                title.organized_to, profile_id, job_id=job_id, title_id=title.id
            )
            if task:
                created.append(task)
        if created:
            logger.info(f"Job {job_id}: queued {len(created)} file(s) for encoding")
        return created

    async def _profile_for(
        self,
        settings: EncodeSettings,
        content_type: ContentType,
        path: Path,
        ffprobe: str | None,
    ) -> int | None:
        if content_type == ContentType.TV:
            return settings.tv_profile_id
        if settings.movie_4k_profile_id and ffprobe:
            try:
                info = await encoder.probe(path, ffprobe)
                if info.resolution_tier == "2160p":
                    return settings.movie_4k_profile_id
            except Exception as e:
                logger.warning(f"Encode: could not probe {path.name} for resolution: {e}")
        return settings.movie_profile_id

    async def enqueue_path(
        self,
        path: str,
        profile_id: int,
        *,
        job_id: int | None = None,
        title_id: int | None = None,
    ) -> EncodeTask | None:
        """Queue one file. Returns None if it is already queued or encoding."""
        if not is_within_configured_roots(path, await library_roots()):
            raise ValueError("path is outside the configured library folders")
        async with database.async_session() as session:
            profile = await session.get(EncodeProfile, profile_id)
            if profile is None:
                raise ValueError(f"encode profile {profile_id} does not exist")
            pending = (
                (
                    await session.execute(
                        select(EncodeTask).where(
                            EncodeTask.source_path == str(path),
                            EncodeTask.state.in_([EncodeTaskState.QUEUED, *ACTIVE_TASK_STATES]),
                        )
                    )
                )
                .scalars()
                .first()
            )
            if pending:
                return None
            task = EncodeTask(
                job_id=job_id,
                title_id=title_id,
                source_path=str(path),
                profile_id=profile.id,
                profile_name=profile.name,
            )
            session.add(task)
            await session.commit()
            await session.refresh(task)
        await self._broadcast(task)
        self.wake()
        return task

    # --- controls ---------------------------------------------------------

    async def cancel(self, task_id: int) -> bool:
        async with database.async_session() as session:
            task = await session.get(EncodeTask, task_id)
            if task is None or task.state in FINISHED_TASK_STATES:
                return False
            if task.state == EncodeTaskState.QUEUED:
                task.state = EncodeTaskState.CANCELLED
                task.finished_at = _now()
                session.add(task)
                await session.commit()
                await session.refresh(task)
                await self._broadcast(task)
                return True
        self._cancel_requested.add(task_id)
        proc = self._procs.get(task_id)
        if proc:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
        return True

    async def retry(self, task_id: int) -> bool:
        async with database.async_session() as session:
            task = await session.get(EncodeTask, task_id)
            if task is None or task.state not in (
                EncodeTaskState.FAILED,
                EncodeTaskState.CANCELLED,
            ):
                return False
            task.state = EncodeTaskState.QUEUED
            task.progress = 0.0
            task.error = task.note = None
            task.started_at = task.finished_at = None
            session.add(task)
            await session.commit()
            await session.refresh(task)
        await self._broadcast(task)
        self.wake()
        return True

    async def clear_finished(self) -> int:
        async with database.async_session() as session:
            rows = (
                (
                    await session.execute(
                        select(EncodeTask).where(EncodeTask.state.in_(list(FINISHED_TASK_STATES)))
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                await session.delete(row)
            await session.commit()
        await ws_manager.broadcast({"type": "encode_cleared"})
        return len(rows)

    # --- worker -----------------------------------------------------------

    async def _run_loop(self) -> None:
        while True:
            try:
                await self._fill_slots()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Encode queue loop error: {e}", exc_info=True)
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=10)

    async def _fill_slots(self) -> None:
        settings = await get_settings()
        free = max(1, settings.concurrency) - len(self._running)
        if free <= 0:
            return
        async with database.async_session() as session:
            queued = (
                (
                    await session.execute(
                        select(EncodeTask)
                        .where(EncodeTask.state == EncodeTaskState.QUEUED)
                        .order_by(EncodeTask.id)
                        .limit(free)
                    )
                )
                .scalars()
                .all()
            )
            ids = [t.id for t in queued if t.id not in self._running]
        for task_id in ids:
            t = asyncio.create_task(self._run_task(task_id), name=f"encode-{task_id}")
            self._running[task_id] = t
            t.add_done_callback(lambda _t, i=task_id: self._on_task_done(i))

    def _on_task_done(self, task_id: int) -> None:
        self._running.pop(task_id, None)
        self._procs.pop(task_id, None)
        self._cancel_requested.discard(task_id)
        self.wake()

    async def _update(self, task_id: int, **fields) -> EncodeTask | None:
        async with database.async_session() as session:
            task = await session.get(EncodeTask, task_id)
            if task is None:
                return None
            for key, value in fields.items():
                setattr(task, key, value)
            session.add(task)
            await session.commit()
            await session.refresh(task)
        await self._broadcast(task)
        return task

    async def _finish(self, task_id: int, state: EncodeTaskState, **fields) -> None:
        await self._update(task_id, state=state, finished_at=_now(), **fields)

    async def _broadcast(self, task: EncodeTask) -> None:
        with contextlib.suppress(Exception):
            await ws_manager.broadcast({"type": "encode_task", "task": task_dict(task)})

    async def _run_task(self, task_id: int) -> None:
        temp: Path | None = None
        try:
            temp = await self._encode(task_id)
        except asyncio.CancelledError:
            if temp:
                with contextlib.suppress(OSError):
                    temp.unlink(missing_ok=True)
            raise
        except Exception as e:
            logger.error(f"Encode task {task_id} failed: {e}", exc_info=True)
            if temp:
                with contextlib.suppress(OSError):
                    temp.unlink(missing_ok=True)
            await self._finish(task_id, EncodeTaskState.FAILED, error=str(e)[:2000])

    async def _encode(self, task_id: int) -> Path | None:
        async with database.async_session() as session:
            task = await session.get(EncodeTask, task_id)
            profile = await session.get(EncodeProfile, task.profile_id) if task else None
        if task is None:
            return None
        if profile is None:
            await self._finish(task_id, EncodeTaskState.FAILED, error="Profile was deleted")
            return None
        settings = await get_settings()

        source = Path(task.source_path)
        if not source.is_file():
            await self._finish(task_id, EncodeTaskState.FAILED, error="Source file not found")
            return None
        ffmpeg = await resolve_ffmpeg()
        if not ffmpeg:
            await self._finish(task_id, EncodeTaskState.FAILED, error="ffmpeg not found")
            return None
        ffprobe = encoder.ffprobe_for(ffmpeg)

        await self._update(
            task_id,
            state=EncodeTaskState.RUNNING,
            started_at=_now(),
            progress=0.0,
            source_bytes=source.stat().st_size,
        )
        info = await encoder.probe(source, ffprobe)
        if reason := encoder.skip_reason(info, profile, ratio=settings.skip_if_source_below_ratio):
            await self._finish(task_id, EncodeTaskState.SKIPPED, note=reason, progress=100.0)
            return None

        estimate = encoder.estimate_size_bytes(info, profile) or source.stat().st_size
        free = shutil.disk_usage(source.parent).free
        if free < estimate + FREE_SPACE_MARGIN_BYTES:
            raise RuntimeError(
                f"Not enough free space: need ~{estimate / 1e9:.1f} GB, {free / 1e9:.1f} GB free"
            )

        temp = temp_path_for(source)
        cmd = encoder.build_command(ffmpeg, source, temp, profile, info)
        await self._update(task_id, command=shlex.join(cmd), duration_seconds=info.duration)
        logger.info(f"Encode task {task_id}: {source.name} with {profile.name!r}")

        returncode, stderr_tail = await self._run_ffmpeg(task_id, cmd, info.duration)
        if task_id in self._cancel_requested:
            temp.unlink(missing_ok=True)
            await self._finish(task_id, EncodeTaskState.CANCELLED, note="Cancelled by user")
            return None
        if returncode != 0:
            temp.unlink(missing_ok=True)
            raise RuntimeError(f"ffmpeg exited {returncode}: {stderr_tail}")

        await self._update(task_id, state=EncodeTaskState.VERIFYING, progress=100.0)
        out_info = await encoder.probe(temp, ffprobe)
        if problems := encoder.verify_output(info, out_info, profile):
            temp.unlink(missing_ok=True)
            raise RuntimeError("Verification failed: " + "; ".join(problems))

        output_bytes = temp.stat().st_size
        roots = await library_roots()
        note = await asyncio.to_thread(self._swap_in, source, temp, settings, roots)
        src_bytes = info.size_bytes or 0
        if src_bytes:
            pct = (1 - output_bytes / src_bytes) * 100
            note = f"{src_bytes / 1e9:.1f} GB → {output_bytes / 1e9:.1f} GB ({pct:.0f}% smaller). {note}"
        if info.dolby_vision:
            note += " Dolby Vision layer not carried over; HDR10 kept."
        await self._finish(
            task_id, EncodeTaskState.DONE, output_bytes=output_bytes, note=note.strip()
        )
        return None

    async def _run_ffmpeg(self, task_id: int, cmd: list[str], duration: float) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        self._procs[task_id] = proc
        stderr_lines: deque[str] = deque(maxlen=20)

        async def drain_stderr() -> None:
            assert proc.stderr
            async for raw in proc.stderr:
                stderr_lines.append(raw.decode(errors="replace").rstrip())

        stderr_task = asyncio.create_task(drain_stderr())
        state = encoder.Progress()
        last_sent = 0.0
        loop = asyncio.get_running_loop()
        assert proc.stdout
        async for raw in proc.stdout:
            if not encoder.parse_progress_line(raw.decode(errors="replace"), state):
                continue
            now = loop.time()
            if now - last_sent >= PROGRESS_INTERVAL_SECONDS:
                last_sent = now
                await self._update(
                    task_id,
                    progress=round(state.percent(duration), 1),
                    fps=state.fps,
                    speed=state.speed,
                    eta_seconds=state.eta(duration),
                )
        returncode = await proc.wait()
        await stderr_task
        return returncode, " | ".join(list(stderr_lines)[-5:])

    @staticmethod
    def _swap_in(
        source: Path, temp: Path, settings: EncodeSettings, roots: list[str] | None = None
    ) -> str:
        """Put the verified encode at the original path; delete or archive the remux."""
        with contextlib.suppress(OSError):
            shutil.copymode(source, temp)
        if not settings.keep_original:
            os.replace(temp, source)
            return "Original remux deleted."
        archive_root = settings.archive_path.strip()
        if archive_root:
            dest = Path(archive_root) / archive_relpath(source, roots or [])
            dest.parent.mkdir(parents=True, exist_ok=True)
        else:
            # Hidden sibling: media servers skip dotfiles, so it never shows as a second version.
            dest = source.with_name(f".{source.stem}.original{source.suffix}")
        shutil.move(str(source), str(dest))
        os.replace(temp, source)
        return f"Original remux kept at {dest}."


encode_queue = EncodeQueue()
