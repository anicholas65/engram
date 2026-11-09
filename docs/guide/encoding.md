# Encoding (fork feature)

Engram files a lossless MakeMKV remux into your library. The **encode stage** can
then re-encode the *video* of each file to a codec and bitrate you choose, in the
background, with an NVIDIA GPU (NVENC) or the CPU.

What never changes:

- **Original audio is kept untouched**: TrueHD/Atmos, DTS-HD MA/DTS:X, LPCM, AC3,
  commentary tracks, language tags and default flags.
- **All subtitles, chapters and attachments are kept.**
- **Output is always MKV**, under the same file name the organizer gave it.

## How it works

```
disc → rip → match → organize → COMPLETED ──► encode queue (background)
                                              ffmpeg → .<name>.encoding.mkv
                                              ffprobe verify
                                              swap in place
```

The disc job finishes as soon as the remux is filed, so the drive is free for the
next disc while encodes run. Each encode is checked before it replaces anything:
duration, video codec, audio track count and codecs, subtitle count, and HDR
transfer must match. A failed, cancelled or interrupted encode leaves the
original untouched.

## Encode page

**Settings**

| Setting | Meaning |
|---|---|
| Encode automatically | Queue a disc's files when its job completes (off by default) |
| Movies / Movies (4K UHD) / TV episodes | Default profile per content type; 4K is chosen when the source is taller than 1440 lines |
| Simultaneous encodes | Parallel ffmpeg processes. NVENC sessions are shared with Jellyfin |
| After a verified encode | Delete the original remux, or keep it in the archive folder (mirrors the library layout) |
| Skip if source is within | A bitrate profile skips a source already at or below target × factor, e.g. a 30 Mbps Blu-ray against a 60 Mbps target |

**Queue**: live progress, fps, speed and ETA; cancel, retry, and the exact
ffmpeg command for each task. Add any library file by path.

**Profiles**: starter profiles are seeded on first run and can be edited.

| Profile | Video | Audio |
|---|---|---|
| Archive | Remux, no encode | Original |
| 4K UHD High | HEVC 10-bit NVENC, 60 Mbps VBR, 80 Mbps peak, multipass | Original |
| 4K HDR Standard | HEVC 10-bit NVENC, 20 Mbps | Original |
| 1080p Standard | HEVC NVENC, 6 Mbps | Original |
| TV Episodes | HEVC NVENC, CQ 22 | Original |
| Mobile | H.264 NVENC 720p, 2.5 Mbps | Original + AAC 192k stereo |

Bitrate presets per resolution are offered as a dropdown, and **Custom…** accepts
any value in Mbps.

## HDR and Dolby Vision

HDR10 is kept by encoding 10-bit HEVC/AV1 and carrying the source's BT.2020 / PQ
colour signalling (x265 also writes mastering-display and MaxCLL metadata).
H.264 cannot carry HDR10, so an H.264 profile skips HDR sources.
A **Dolby Vision** layer is not carried through a re-encode; the HDR10 base layer is.
Keep the original remux if you need DV.

## Hardware (RTX 20-series / Turing)

- NVENC: H.264 and HEVC 8/10-bit. **No AV1 encode** (needs RTX 40-series), so AV1
  profiles run on the CPU (SVT-AV1) and are slow.
- NVDEC decodes the source (MPEG-2, VC-1, H.264, HEVC), so the CPU stays mostly idle.

## Unraid

Use `deploy/unraid/engram-encode.xml`. You need the **Nvidia Driver** plugin, plus
`--runtime=nvidia` and `NVIDIA_VISIBLE_DEVICES` (both are in the template). The Encode page
shows whether NVENC works by running a real test encode; use **Re-check** after
changing the container.
