"""Encode queue: seeding, enqueue rules, the job hook, and the safe file swap."""

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlmodel import select

from app import database
from app.core import encoder
from app.models.disc_job import ContentType, DiscJob, DiscTitle, JobState, TitleState
from app.models.encode import EncodeProfile, EncodeSettings, EncodeTask, EncodeTaskState
from app.services import encode_queue as eq
from app.services.config_service import update_config


@pytest.fixture
async def library(tmp_path):
    movies, tv = tmp_path / "Movies", tmp_path / "TV"
    movies.mkdir()
    tv.mkdir()
    await update_config(library_movies_path=str(movies), library_tv_path=str(tv))
    await eq.seed_defaults()
    return movies, tv


async def _profile_id(name: str) -> int:
    async with database.async_session() as s:
        row = (await s.execute(select(EncodeProfile).where(EncodeProfile.name == name))).scalars()
        return row.first().id


async def _set(**fields) -> None:
    async with database.async_session() as s:
        settings = (await s.execute(select(EncodeSettings))).scalars().first()
        for k, v in fields.items():
            setattr(settings, k, v)
        s.add(settings)
        await s.commit()


async def _task(task_id: int) -> EncodeTask:
    async with database.async_session() as s:
        return await s.get(EncodeTask, task_id)


def _info(**kw) -> encoder.MediaInfo:
    base = dict(
        duration=7200.0,
        size_bytes=1000,
        format_bit_rate=None,
        video_codec="hevc",
        width=3840,
        height=2160,
        color_primaries="bt2020",
        color_transfer="smpte2084",
        color_space="bt2020nc",
        video_bit_rate=90_000_000,
        audio=[encoder.AudioStream(0, "truehd", 8, "eng")],
        subtitle_count=2,
    )
    base.update(kw)
    return encoder.MediaInfo(**base)


class TestSeeding:
    async def test_profiles_and_default_assignment(self, library):
        await eq.seed_defaults()  # idempotent
        async with database.async_session() as s:
            names = [p.name for p in (await s.execute(select(EncodeProfile))).scalars()]
            settings = (await s.execute(select(EncodeSettings))).scalars().all()
        assert sorted(names) == sorted(p["name"] for p in encoder.BUILTIN_PROFILES)
        assert len(settings) == 1
        assert not settings[0].enabled  # stock Engram behaviour until switched on
        assert settings[0].movie_4k_profile_id == await _profile_id("4K UHD High")

    async def test_null_bitrates_survive_the_round_trip(self, library):
        async with database.async_session() as s:
            rows = {p.name: p for p in (await s.execute(select(EncodeProfile))).scalars()}
        assert rows["Archive"].video_bitrate_kbps is None
        assert rows["TV Episodes"].video_bitrate_kbps is None
        assert rows["TV Episodes"].quality == 22
        assert rows["4K UHD High"].video_bitrate_kbps == 60_000


class TestEnqueue:
    async def test_outside_library_rejected(self, library, tmp_path):
        with pytest.raises(ValueError, match="outside"):
            await eq.encode_queue.enqueue_path(str(tmp_path / "x.mkv"), 1)

    async def test_duplicate_pending_ignored(self, library):
        movies, _ = library
        f = movies / "A (2020)" / "A (2020).mkv"
        pid = await _profile_id("4K UHD High")
        assert await eq.encode_queue.enqueue_path(str(f), pid)
        assert await eq.encode_queue.enqueue_path(str(f), pid) is None

    async def _tv_job(self, tv: Path) -> int:
        async with database.async_session() as s:
            job = DiscJob(drive_id="E:", content_type=ContentType.TV)
            s.add(job)
            await s.commit()
            await s.refresh(job)
            for i, (org, state, extra) in enumerate(
                [
                    (tv / "Show" / "Season 01" / "Show - S01E01.mkv", TitleState.COMPLETED, False),
                    (tv / "Show" / "Season 01" / "Show - S01E02.mkv", TitleState.COMPLETED, False),
                    (None, TitleState.REVIEW, False),
                    (tv / "Show" / "Extras" / "x.mkv", TitleState.COMPLETED, True),
                ]
            ):
                s.add(
                    DiscTitle(
                        job_id=job.id,
                        title_index=i,
                        duration_seconds=1300,
                        organized_to=str(org) if org else None,
                        state=state,
                        is_extra=extra,
                    )
                )
            await s.commit()
            return job.id

    async def test_hook_does_nothing_while_disabled(self, library):
        job_id = await self._tv_job(library[1])
        await eq.encode_queue.on_job_terminal(job_id, JobState.COMPLETED)
        async with database.async_session() as s:
            assert (await s.execute(select(EncodeTask))).scalars().all() == []

    async def test_hook_enqueues_organized_titles(self, library):
        await _set(enabled=True, include_extras=False)
        job_id = await self._tv_job(library[1])
        await eq.encode_queue.on_job_terminal(job_id, JobState.FAILED)  # ignored
        await eq.encode_queue.on_job_terminal(job_id, JobState.COMPLETED)
        async with database.async_session() as s:
            tasks = (await s.execute(select(EncodeTask))).scalars().all()
        assert sorted(Path(t.source_path).name for t in tasks) == [
            "Show - S01E01.mkv",
            "Show - S01E02.mkv",
        ]
        assert {t.profile_name for t in tasks} == {"TV Episodes"}


class TestEncodeRun:
    async def _queue_file(self, library, *, content=b"remux") -> tuple[Path, int]:
        movies, _ = library
        src = movies / "Film (2024)" / "Film (2024).mkv"
        src.parent.mkdir(parents=True)
        src.write_bytes(content)
        task = await eq.encode_queue.enqueue_path(str(src), await _profile_id("4K UHD High"))
        return src, task.id

    def _fake_tools(self, monkeypatch, *, out_info=None, returncode=0):
        async def fake_probe(path, ffprobe):
            if Path(path).name.endswith(eq.TEMP_SUFFIX):
                return out_info or _info(size_bytes=400)
            return _info()

        async def fake_run(self, task_id, cmd, duration):
            Path(cmd[-1]).write_bytes(b"encoded")
            return returncode, "boom" if returncode else ""

        async def fake_ffmpeg():
            return "/usr/bin/ffmpeg"

        monkeypatch.setattr(eq.encoder, "probe", fake_probe)
        monkeypatch.setattr(eq.EncodeQueue, "_run_ffmpeg", fake_run)
        monkeypatch.setattr(eq, "resolve_ffmpeg", fake_ffmpeg)
        monkeypatch.setattr(eq.shutil, "disk_usage", lambda p: type("U", (), {"free": 10**15})())

    async def test_not_enough_space_fails_safely(self, library, monkeypatch):
        self._fake_tools(monkeypatch)
        monkeypatch.setattr(eq.shutil, "disk_usage", lambda p: type("U", (), {"free": 10**9})())
        src, tid = await self._queue_file(library)
        await eq.encode_queue._run_task(tid)
        task = await _task(tid)
        assert task.state == EncodeTaskState.FAILED and "free space" in task.error
        assert src.read_bytes() == b"remux"

    async def test_success_replaces_original(self, library, monkeypatch):
        self._fake_tools(monkeypatch)
        src, tid = await self._queue_file(library)
        await eq.encode_queue._run_task(tid)
        task = await _task(tid)
        assert task.state == EncodeTaskState.DONE, task.error
        assert src.read_bytes() == b"encoded"
        assert not eq.temp_path_for(src).exists()
        assert "hevc_nvenc" in task.command and "60000k" in task.command
        assert "deleted" in task.note

    async def test_keep_original_mirrors_library_layout(self, library, monkeypatch, tmp_path):
        self._fake_tools(monkeypatch)
        archive = tmp_path / "archive"
        await _set(keep_original=True, archive_path=str(archive))
        src, tid = await self._queue_file(library)
        await eq.encode_queue._run_task(tid)
        assert (await _task(tid)).state == EncodeTaskState.DONE
        kept = archive / "Movies" / "Film (2024)" / "Film (2024).mkv"
        assert kept.read_bytes() == b"remux"
        assert src.read_bytes() == b"encoded"

    async def test_failed_verification_leaves_original(self, library, monkeypatch):
        self._fake_tools(monkeypatch, out_info=_info(audio=[], size_bytes=10))
        src, tid = await self._queue_file(library)
        await eq.encode_queue._run_task(tid)
        task = await _task(tid)
        assert task.state == EncodeTaskState.FAILED
        assert "audio tracks" in task.error
        assert src.read_bytes() == b"remux"
        assert not eq.temp_path_for(src).exists()

    async def test_ffmpeg_error_leaves_original(self, library, monkeypatch):
        self._fake_tools(monkeypatch, returncode=1)
        src, tid = await self._queue_file(library)
        await eq.encode_queue._run_task(tid)
        task = await _task(tid)
        assert task.state == EncodeTaskState.FAILED and "boom" in task.error
        assert src.read_bytes() == b"remux"
        assert not eq.temp_path_for(src).exists()

    async def test_low_bitrate_source_skipped(self, library, monkeypatch):
        self._fake_tools(monkeypatch)

        async def low(path, ffprobe):
            return _info(video_bit_rate=40_000_000)

        monkeypatch.setattr(eq.encoder, "probe", low)
        src, tid = await self._queue_file(library)
        await eq.encode_queue._run_task(tid)
        task = await _task(tid)
        assert task.state == EncodeTaskState.SKIPPED
        assert src.read_bytes() == b"remux"

    async def test_cancel_queued_and_retry(self, library):
        _, tid = await self._queue_file(library)
        assert await eq.encode_queue.cancel(tid)
        assert (await _task(tid)).state == EncodeTaskState.CANCELLED
        assert await eq.encode_queue.retry(tid)
        assert (await _task(tid)).state == EncodeTaskState.QUEUED

    async def test_restart_requeues_interrupted(self, library):
        src, tid = await self._queue_file(library)
        eq.temp_path_for(src).write_bytes(b"partial")
        async with database.async_session() as s:
            t = await s.get(EncodeTask, tid)
            t.state = EncodeTaskState.RUNNING
            s.add(t)
            await s.commit()
        await eq.encode_queue._recover_interrupted()
        assert (await _task(tid)).state == EncodeTaskState.QUEUED
        assert not eq.temp_path_for(src).exists()


def test_archive_relpath_keeps_show_folders_apart(tmp_path):
    root = tmp_path / "TV"
    a = root / "Show A" / "Season 01" / "e.mkv"
    b = root / "Show B" / "Season 01" / "e.mkv"
    assert eq.archive_relpath(a, [str(root)]) != eq.archive_relpath(b, [str(root)])
    assert eq.archive_relpath(a, [str(root)]) == Path("TV/Show A/Season 01/e.mkv")


class TestApi:
    @pytest.fixture
    async def client(self, library):
        from app.api.encode_routes import router

        app = FastAPI()
        app.include_router(router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            yield c

    async def test_create_custom_60mbps_profile(self, client):
        r = await client.post(
            "/api/encode/profiles",
            json={
                "name": "UHD 70",
                "video_bitrate_kbps": 70_000,
                "max_bitrate_kbps": 90_000,
            },
        )
        assert r.status_code == 201, r.text
        assert r.json()["encoder"] == "hevc_nvenc"

    async def test_bitrate_mode_needs_bitrate(self, client):
        r = await client.post("/api/encode/profiles", json={"name": "bad"})
        assert r.status_code == 422

    async def test_maxrate_below_target_rejected(self, client):
        r = await client.post(
            "/api/encode/profiles",
            json={"name": "bad", "video_bitrate_kbps": 60_000, "max_bitrate_kbps": 20_000},
        )
        assert r.status_code == 422

    async def test_wrong_preset_for_encoder(self, client):
        r = await client.post(
            "/api/encode/profiles",
            json={"name": "bad", "encoder": "libx265", "video_bitrate_kbps": 8000},
        )
        assert r.status_code == 422  # default "p7" is NVENC-only

    async def test_default_profile_cannot_be_deleted(self, client):
        pid = await _profile_id("4K UHD High")
        assert (await client.delete(f"/api/encode/profiles/{pid}")).status_code == 409

    async def test_settings_roundtrip(self, client):
        r = await client.get("/api/encode/settings")
        body = r.json()
        body.update(enabled=True, concurrency=3)
        body.pop("id")
        r = await client.put("/api/encode/settings", json=body)
        assert r.status_code == 200 and r.json()["concurrency"] == 3

    async def test_options_without_ffmpeg(self, client, monkeypatch):
        async def none():
            return None

        monkeypatch.setattr("app.api.encode_routes.resolve_ffmpeg", none)
        r = await client.get("/api/encode/options")
        assert 60 in r.json()["video_bitrate_presets_mbps"]["2160p"]["hevc"]


async def test_start_stop(library):
    q = eq.EncodeQueue()
    q._hook_registered = True  # don't add to the shared module-level state machine
    await q.start()
    await asyncio.sleep(0)
    await q.stop()
