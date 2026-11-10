# Engram Encode Fork

A fork of [Engram](https://github.com/Jsakkos/engram) by Jsakkos that adds an **encode
stage**. Discs are ripped, identified and filed by stock Engram; this fork then
re-encodes the video to the codec and bitrate you choose, using an NVIDIA GPU, while
keeping the original audio and subtitles untouched.

| | |
|---|---|
| **Repo** | https://github.com/anicholas65/engram (branch `main`, dev branch `feature/encode-profiles`) |
| **Image** | `ghcr.io/anicholas65/engram:latest` |
| **Based on** | Engram v0.45.0 |
| **Target host** | Unraid (GORT) with an NVIDIA RTX 2080 Ti |
| **Licence** | AGPL-3.0, same as upstream |

---

## What it adds

- **Encode profiles**: named recipes for codec, bitrate or quality, peak cap, resolution and preset.
- **Bitrate presets per resolution**, plus a **Custom** value. This includes **4K HEVC 60 Mbps**.
- **NVENC hardware encoding** (HEVC 10-bit / H.264), with CPU fallbacks (x265, x264, SVT-AV1).
- **Original audio always kept**: TrueHD/Atmos, DTS-HD MA/DTS:X, LPCM, AC3 and all tracks.
  You can optionally add an AAC stereo compatibility track.
- **All subtitles, chapters and attachments kept.**
- **MKV output only**, written under the same name and path the organizer chose.
- **HDR10 preserved** (10-bit, BT.2020/PQ signalling).
- **Background encode queue** with live progress, fps, speed, ETA, cancel and retry.
- **Safe swap**: every encode is verified before it replaces anything.
- **ENCODE tab** in the dashboard for settings, the queue and profiles.
- **No uploads to the upstream author's servers** (see [Privacy](#privacy-no-upstream-uploads)).

## How it works

```
Disc ─► IDENTIFY ─► RIP (MakeMKV) ─► MATCH ─► ORGANIZE ─► COMPLETED
                                                              │
                                     on COMPLETED (if enabled) ▼
                                  ┌──────────── Encode queue ────────────┐
                                  │ probe (ffprobe)                      │
                                  │ skip? (archive profile, low bitrate, │
                                  │        already encoded, HDR→H.264)   │
                                  │ ffmpeg → .<name>.encoding.mkv        │
                                  │ verify output                        │
                                  │ swap in place; delete/archive remux  │
                                  └──────────────────────────────────────┘
```

- The disc job finishes as soon as the lossless remux is filed. Encoding runs
  separately, so you can keep ripping discs while encodes run.
- Matching uses the original audio, so encoding always happens after matching.
- The temporary file is hidden (dot-prefixed), so Jellyfin/Plex never pick up a half-written encode.
- **Verification** before the swap checks:
  - duration (within 0.5%, or 2 seconds)
  - video codec
  - audio track count and codecs
  - subtitle count
  - HDR transfer
- A failed, cancelled or interrupted encode **never touches the original**.
  Interrupted tasks are re-queued on restart.
- Every encoded file is tagged `ENGRAM_ENCODE_PROFILE=<name>`, so it is never re-encoded by accident.

## Starter profiles

| Profile | Encoder | Video | Peak | Audio |
|---|---|---|---|---|
| Archive | — | Remux, no encode | — | Original |
| **4K UHD High** | HEVC NVENC 10-bit | **60 Mbps** VBR, multipass, p7 | 80 Mbps | Original |
| 4K HDR Standard | HEVC NVENC 10-bit | 20 Mbps | 40 Mbps | Original |
| 1080p Standard | HEVC NVENC 10-bit | 6 Mbps | 10 Mbps | Original |
| TV Episodes | HEVC NVENC 10-bit | CQ 22 | 10 Mbps | Original |
| Mobile | H.264 NVENC 720p | 2.5 Mbps | 5 Mbps | Original + AAC 192k stereo |

Default assignment: **every slot starts on *4K UHD High*** (60 Mbps). With the skip rule below, only sources above ~66 Mbps are re-encoded down to 60 Mbps; anything already at or below that (all 1080p Blu-rays, DVDs, most UHD discs) keeps its lossless remux. Pick *1080p Standard* / *TV Episodes* per slot if you want smaller files.
All profiles can be edited; new ones can be added.

## Bitrate options

Presets in the profile editor (Mbps). **Custom…** accepts any value.

| Resolution | H.264 | HEVC | AV1 |
|---|---|---|---|
| 480p | 1.5 · 2 · 3 · 4 | 1 · 1.5 · 2 · 3 | 0.8 · 1 · 1.5 · 2 |
| 720p | 3 · 4 · 5 · 6 | 2 · 2.5 · 3 · 4 | 1.5 · 2 · 2.5 · 3 |
| 1080p | 6 · 8 · 10 · 12 · 15 | 4 · 5 · 6 · 8 · 10 | 3 · 4 · 5 · 6 · 8 |
| 2160p | 25 · 35 · 45 | 12 · 15 · 20 · 25 · 35 · 50 · **60** · 80 | 8 · 12 · 15 · 20 · 30 · 40 · 60 |

- **Peak cap:** none · 10 · 20 · 40 · 80 · 100 Mbps
- **Constant quality:** CQ/CRF 14–30. 18 is near-transparent, 22 is high quality, 26 is compact.
- **Stereo AAC track:** 128 · 160 · 192 · 256 kbps
- **Rough size:** 60 Mbps works out to about 27 GB per hour, so about 58 GB for a 2-hour film
  including lossless audio.

## Hardware notes (RTX 2080 Ti / Turing)

| Feature | Supported |
|---|---|
| HEVC 8/10-bit encode, including 4K HDR10 | ✅ NVENC |
| H.264 encode | ✅ NVENC (8-bit only, so no HDR) |
| AV1 encode | ❌ needs RTX 40-series. AV1 profiles use the CPU (SVT-AV1) and are slow |
| Decoding MPEG-2 / VC-1 / H.264 / HEVC | ✅ NVDEC |

- Rough speed: a 2-hour 4K film takes about 30–60 minutes on NVENC, against 15–30 hours on the CPU.
- NVENC sessions are shared with Jellyfin transcodes. The 2080 Ti has one NVENC engine, so the default is 1 encode at a time (more just share it).
- NVENC runs on separate hardware from CUDA, so encodes can overlap with ComfyUI.

## Encode settings

| Setting | Default | Meaning |
|---|---|---|
| Encode automatically | **On** | Queue a disc's files when its job completes |
| Movies / Movies (4K) / TV | see above | Profile per content type. A source taller than 1440 lines counts as 4K |
| Include extras | On | Encode bonus features too |
| Simultaneous encodes | 1 | Parallel ffmpeg processes |
| After a verified encode | Delete original | Or keep the remux in an archive folder (mirrors the library layout) |
| Skip if source within | 1.1× target | Bitrate profiles skip sources already at or below the target |

**What you see while it runs:** a toast on any page when an encode starts (encoder, target, source bitrate), finishes or fails; a badge on the ENCODE tab while encoding; and on each queue row the start time and elapsed time, encoder, target (e.g. *60 Mbps (peak 80)*), source video bitrate, the live video bitrate (*Now ≈ 58.7 Mbps*), and, when done, the measured output bitrate.

Defaults apply when the settings row is first created. An install that already ran `v0.45.0-encode.1` keeps its saved settings; change them on the ENCODE tab.

**Known limits:**
- **HDR10 static metadata with NVENC** (to check on real hardware): the image's ffmpeg is Debian bookworm's 5.1, whose `hevc_nvenc` keeps BT.2020/PQ signalling but does not write mastering-display / MaxCLL metadata. Most TVs handle that; a newer ffmpeg (e.g. jellyfin-ffmpeg 7) would carry it. The x265 path writes it.
- **Dolby Vision** is not carried through a re-encode; the HDR10 base layer is.
  Keep the original remux if you need DV.
- **H.264 profiles skip HDR sources**, because H.264 can't carry HDR10.

---

## Install on Unraid

1. Install the **Nvidia Driver** plugin (Apps).
2. Copy the template to the flash drive:
   ```
   cp /mnt/user/Projects/Coding/engram/my-engram-encode.xml /boot/config/plugins/dockerMan/templates-user/
   ```
   The source file is `deploy/unraid/engram-encode.xml` in this repo.
3. **Docker → Add Container → Template: engram-encode**, then set:
   - **Optical drive:** `/dev/sr0` (check with `ls -l /dev/sr*`)
   - **Config:** `/mnt/user/appdata/engram-encode` (a copy of the old Engram appdata, if migrating)
   - **Movies / TV / Staging:** your shares
   - **NVIDIA_VISIBLE_DEVICES:** the GPU UUID, or `all`
   - **Extra Parameters:** `--runtime=nvidia` (already set)
4. Open `http://192.168.1.64:8000` and complete the wizard. Use `/media/movies`,
   `/media/tv` and `/staging` as the paths, plus your MakeMKV key and TMDB Read Access Token.
5. Open the **ENCODE** tab and confirm the green **"NVIDIA NVENC ready"** banner.
   Use **Re-check** after changing the container.

Run only **one** Engram container per optical drive. Stop the stock Engram container
and turn its autostart off.

### Updating

Unraid **Docker → engram-encode → Update** (or *Check for updates*) pulls the latest image.

---

## Privacy (no upstream uploads)

The fork never sends data to the upstream author:

| Path | Status |
|---|---|
| Fingerprint, disc-layout and retraction uploads to `api.engramfp.com` | **Blocked by default.** Gated by `ENGRAM_UPSTREAM_UPLOADS=1`, whatever the in-app toggles say |
| Remote "forget my pseudonym" call | Skipped, since nothing was uploaded |
| `git push` to Jsakkos/engram | Disabled in the local clone (`upstream` push URL) |
| `gh` PRs / issues | Default repo is `anicholas65/engram` |
| GitHub Actions | Only `ci.yml` and `docker.yml` are enabled on the fork |

Read-only traffic still happens: release checks, which only notify and never auto-update
in Docker, and downloads of subtitle and fingerprint packs. TheDiscDB contributions go to
a third party and are off unless you enable them.

---

## Development

Work happens in `~/projects/engram` on agent-01.

```bash
# Backend
cd backend
uv sync
uv run pytest tests/unit tests/integration   # ~5,370 tests
uv run ruff check . && uv run ruff format .
uv run uvicorn app.main:app --port 8100       # dev server (never --reload)

# Frontend
cd frontend
npm ci
npm run build
npx vitest run
```

Use simulation mode (`DEBUG=true` + `/api/simulate/*`) instead of a real drive when
testing on agent-01.

### Fork files

Almost all fork code lives in new files, so upstream merges stay easy.

| File | Purpose |
|---|---|
| `backend/app/models/encode.py` | `EncodeProfile`, `EncodeTask`, `EncodeSettings` tables |
| `backend/migrations/versions/b7e4c2a9d1f0_add_encode_tables.py` | Alembic migration |
| `backend/app/core/encoder.py` | Probe, skip rules, ffmpeg command builder, progress, verification, presets |
| `backend/app/services/encode_queue.py` | Worker queue, COMPLETED hook, safe swap, restart recovery |
| `backend/app/api/encode_routes.py` | `/api/encode/*` REST API |
| `frontend/src/components/EncodePage.tsx` | ENCODE tab (settings, queue, profile editor) |
| `frontend/src/api/encodeClient.ts` | Typed API client |
| `deploy/unraid/engram-encode.xml` | Unraid template |
| `docs/guide/encoding.md` | User guide |
| `backend/tests/unit/test_encoder.py`, `test_encode_queue.py`, `test_fork_no_upstream_uploads.py` | Tests |

Upstream files touched (small edits):
- `app/main.py`: start/stop the queue and mount the router
- `app/services/contribution_uploader.py` and `app/api/routes.py`: upload kill switch
- `tests/conftest.py`: let upstream's tests opt back in to uploads
- `App.tsx`, `navigation.ts`, `routes.ts`: the ENCODE route and tab
- `Dockerfile`: `NVIDIA_DRIVER_CAPABILITIES`
- `.github/workflows/docker.yml`: amd64-only build

### API

| Method | Path | |
|---|---|---|
| GET | `/api/encode/options` | Encoders, bitrate presets, NVENC test result (`?refresh_hardware=true`) |
| GET/POST | `/api/encode/profiles` | List / create |
| PUT/DELETE | `/api/encode/profiles/{id}` | Edit / delete (defaults and in-use profiles are protected) |
| GET/PUT | `/api/encode/settings` | Encode settings |
| GET/POST | `/api/encode/tasks` | Queue / enqueue `{path, profile_id}` or `{job_id}` |
| POST | `/api/encode/tasks/{id}/cancel` · `/retry` | Controls |
| DELETE | `/api/encode/tasks/finished` | Clear finished |

WebSocket events on `/ws`: `encode_task` (a task changed) and `encode_cleared`.

### Pulling upstream updates

```bash
git fetch upstream
git merge upstream/main          # resolve any conflicts in the files listed above
cd backend && uv run pytest tests/unit tests/integration
git push origin feature/encode-profiles feature/encode-profiles:main
```

### Releasing a new image

```bash
git tag -a v0.45.0-encode.2 -m "..."   # bump the suffix; use the upstream version you're based on
git push origin v0.45.0-encode.2      # triggers docker.yml → ghcr.io/anicholas65/engram:latest
```

---

## Roadmap

- [x] **Phase 1:** profiles, queue, NVENC/CPU encoders, ENCODE tab, Unraid template, upload kill switch
- [ ] **Phase 2:**
  - [ ] HDR → SDR tone-mapping profiles
  - [ ] Confirm HDR10 mastering metadata survives NVENC
  - [ ] Choose a profile per disc before ripping
  - [ ] Re-encode from History
- [ ] **Phase 3:**
  - [ ] Size estimate from a 60-second sample encode
  - [ ] Per-title encode stats in History

## Credits

Engram is by [Jsakkos](https://github.com/Jsakkos/engram) and contributors (AGPL-3.0).
This fork adds the encode stage and is not affiliated with or supported by upstream, so
please don't file issues for fork features on the upstream repo.
