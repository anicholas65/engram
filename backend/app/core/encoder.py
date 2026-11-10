"""Video re-encoding for the encode stage: probe, command building, progress, verification.

Design rules (they are the feature, not incidental choices):

- **Only the video is encoded.** Every audio track (TrueHD/Atmos, DTS-HD MA/DTS:X,
  LPCM, AC3...) and every subtitle track is stream-copied bit-for-bit, in the
  source's order, with its language/title/default flags. A profile may *add* one
  AAC stereo compatibility track; it never replaces an original.
- **The container is always Matroska.** MP4 cannot reliably carry TrueHD/DTS-HD/PGS.
- **HDR10 is preserved** by encoding 10-bit and carrying the source's colour
  primaries/transfer/matrix (plus mastering-display metadata for x265). A
  Dolby Vision RPU/enhancement layer is not carried through a re-encode; the
  HDR10 base layer is.
- Pure functions here; ``EncodeQueue`` owns processes, files and the DB.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from app.models.encode import EncodeProfile, RateControl, VideoEncoder

# ---------------------------------------------------------------------------
# Option catalog (drives the UI dropdowns; "custom" values are always allowed)
# ---------------------------------------------------------------------------

# Target video bitrates in Mbps, per resolution tier and codec family.
VIDEO_BITRATE_PRESETS_MBPS: dict[str, dict[str, list[float]]] = {
    "480p": {"h264": [1.5, 2, 3, 4], "hevc": [1, 1.5, 2, 3], "av1": [0.8, 1, 1.5, 2]},
    "720p": {"h264": [3, 4, 5, 6], "hevc": [2, 2.5, 3, 4], "av1": [1.5, 2, 2.5, 3]},
    "1080p": {
        "h264": [6, 8, 10, 12, 15],
        "hevc": [4, 5, 6, 8, 10],
        "av1": [3, 4, 5, 6, 8],
    },
    "2160p": {
        "h264": [25, 35, 45],
        "hevc": [12, 15, 20, 25, 35, 50, 60, 80],
        "av1": [8, 12, 15, 20, 30, 40, 60],
    },
}
MAX_BITRATE_PRESETS_MBPS = [10, 20, 40, 80, 100]
QUALITY_RANGE = (14, 30)
STEREO_AAC_KBPS_PRESETS = [128, 160, 192, 256]
NVENC_PRESETS = ["p1", "p2", "p3", "p4", "p5", "p6", "p7"]
X26X_PRESETS = ["ultrafast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]
SVT_PRESETS = [str(n) for n in range(0, 13)]

ENCODER_FAMILY = {
    VideoEncoder.HEVC_NVENC: "hevc",
    VideoEncoder.LIBX265: "hevc",
    VideoEncoder.H264_NVENC: "h264",
    VideoEncoder.LIBX264: "h264",
    VideoEncoder.LIBSVTAV1: "av1",
}
# ffprobe codec_name an encoder produces, for verification.
ENCODER_OUTPUT_CODEC = {
    VideoEncoder.HEVC_NVENC: "hevc",
    VideoEncoder.LIBX265: "hevc",
    VideoEncoder.H264_NVENC: "h264",
    VideoEncoder.LIBX264: "h264",
    VideoEncoder.LIBSVTAV1: "av1",
}

# Source codecs NVDEC on Turing can decode. Anything else decodes on the CPU.
NVDEC_CODECS = frozenset({"h264", "hevc", "mpeg2video", "mpeg1video", "vc1", "mpeg4", "vp8", "vp9"})

# Global tag written on every encode, so a file is never re-encoded by accident.
ENCODE_TAG = "ENGRAM_ENCODE_PROFILE"

# Per-stream statistics tags MakeMKV/mkvmerge write. After a re-encode the video's
# values describe the *old* stream (Jellyfin would show the remux bitrate), so they
# are cleared on the video stream. Audio/subtitle streams are untouched and keep theirs.
_STALE_VIDEO_TAGS = (
    "BPS",
    "BPS-eng",
    "NUMBER_OF_BYTES",
    "NUMBER_OF_BYTES-eng",
    "NUMBER_OF_FRAMES",
    "NUMBER_OF_FRAMES-eng",
    "DURATION",
    "DURATION-eng",
    "_STATISTICS_TAGS",
    "_STATISTICS_TAGS-eng",
    "_STATISTICS_WRITING_APP",
    "_STATISTICS_WRITING_APP-eng",
    "_STATISTICS_WRITING_DATE_UTC",
    "_STATISTICS_WRITING_DATE_UTC-eng",
)

INTERLACED_FIELD_ORDERS = frozenset({"tt", "bb", "tb", "bt"})


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------


@dataclass
class AudioStream:
    index: int  # Position among audio streams (the N in 0:a:N)
    codec: str
    channels: int | None = None
    language: str = "und"
    title: str = ""
    bit_rate: int | None = None


@dataclass
class MediaInfo:
    duration: float
    size_bytes: int | None
    format_bit_rate: int | None
    video_codec: str
    width: int
    height: int
    pix_fmt: str = ""
    field_order: str = ""
    color_primaries: str = ""
    color_transfer: str = ""
    color_space: str = ""
    video_bit_rate: int | None = None
    dolby_vision: bool = False
    mastering_display: dict | None = None
    content_light: dict | None = None
    audio: list[AudioStream] = field(default_factory=list)
    subtitle_count: int = 0
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def is_hdr(self) -> bool:
        return self.color_transfer in ("smpte2084", "arib-std-b67")

    @property
    def resolution_tier(self) -> str:
        if self.height > 1440:
            return "2160p"
        if self.height > 800:
            return "1080p"
        if self.height > 576:
            return "720p"
        return "480p"


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _stream_bit_rate(stream: dict) -> int | None:
    """A stream's bitrate: ffprobe's own figure, else MakeMKV/mkvmerge's BPS tag."""
    rate = _int(stream.get("bit_rate"))
    if rate:
        return rate
    tags = stream.get("tags") or {}
    for key in ("BPS", "BPS-eng"):
        rate = _int(tags.get(key))
        if rate:
            return rate
    return None


def parse_probe(data: dict, first_frame: dict | None = None) -> MediaInfo:
    """Turn ``ffprobe -show_streams -show_format`` JSON into a ``MediaInfo``.

    ``first_frame`` is the optional ``-show_frames`` record of the first video
    frame, which is where ffprobe reports HDR10 mastering-display metadata.
    """
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise ValueError("no video stream")

    side_data = list(video.get("side_data_list") or [])
    if first_frame:
        side_data += list(first_frame.get("side_data_list") or [])
    mastering = next(
        (d for d in side_data if d.get("side_data_type") == "Mastering display metadata"),
        None,
    )
    light = next(
        (d for d in side_data if d.get("side_data_type") == "Content light level metadata"),
        None,
    )
    dovi = any("DOVI" in str(d.get("side_data_type", "")) for d in side_data)

    audio: list[AudioStream] = []
    subtitles = 0
    for s in streams:
        kind = s.get("codec_type")
        tags = s.get("tags") or {}
        if kind == "audio":
            audio.append(
                AudioStream(
                    index=len(audio),
                    codec=s.get("codec_name", ""),
                    channels=_int(s.get("channels")),
                    language=(tags.get("language") or "und").lower(),
                    title=tags.get("title", ""),
                    bit_rate=_stream_bit_rate(s),
                )
            )
        elif kind == "subtitle":
            subtitles += 1

    duration = _float(fmt.get("duration")) or _float(video.get("duration")) or 0.0
    format_rate = _int(fmt.get("bit_rate"))
    video_rate = _stream_bit_rate(video)
    if video_rate is None and format_rate:
        # MKV rarely reports a per-stream video bitrate; subtract the audio we know.
        known_audio = sum(a.bit_rate or 0 for a in audio)
        if known_audio and format_rate > known_audio:
            video_rate = format_rate - known_audio

    return MediaInfo(
        duration=duration,
        size_bytes=_int(fmt.get("size")),
        format_bit_rate=format_rate,
        video_codec=video.get("codec_name", ""),
        width=_int(video.get("width")) or 0,
        height=_int(video.get("height")) or 0,
        pix_fmt=video.get("pix_fmt", ""),
        field_order=video.get("field_order", ""),
        color_primaries=video.get("color_primaries", ""),
        color_transfer=video.get("color_transfer", ""),
        color_space=video.get("color_space", ""),
        video_bit_rate=video_rate,
        dolby_vision=dovi,
        mastering_display=mastering,
        content_light=light,
        audio=audio,
        subtitle_count=subtitles,
        tags={str(k).upper(): str(v) for k, v in (fmt.get("tags") or {}).items()},
    )


def ffprobe_for(ffmpeg: str) -> str:
    """The ffprobe that ships beside ``ffmpeg`` (falls back to PATH)."""
    p = Path(ffmpeg)
    sibling = p.with_name(p.name.replace("ffmpeg", "ffprobe"))
    if p.name != sibling.name and sibling.exists():
        return str(sibling)
    return shutil.which("ffprobe") or "ffprobe"


async def _run_json(cmd: list[str]) -> dict:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {err.decode(errors='replace').strip()[-500:]}")
    return json.loads(out or b"{}")


async def probe(path: str | Path, ffprobe: str) -> MediaInfo:
    data = await _run_json(
        [
            ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        ]
    )
    first_frame = None
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    if video.get("color_transfer") in ("smpte2084", "arib-std-b67"):
        frames = await _run_json(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-read_intervals",
                "%+#1",
                "-show_frames",
                "-print_format",
                "json",
                str(path),
            ]
        )
        first_frame = (frames.get("frames") or [None])[0]
    return parse_probe(data, first_frame)


# ---------------------------------------------------------------------------
# Decisions: skip / estimate
# ---------------------------------------------------------------------------


def skip_reason(info: MediaInfo, profile: EncodeProfile, *, ratio: float = 1.1) -> str | None:
    """Why this file should not be encoded with this profile, or None to encode it."""
    if profile.encoder == VideoEncoder.COPY:
        return "Archive profile: remux kept as-is"
    if info.tags.get(ENCODE_TAG):
        return f"Already encoded (profile {info.tags[ENCODE_TAG]!r})"
    if info.is_hdr and ENCODER_FAMILY.get(profile.encoder) == "h264":
        return "HDR source: H.264 cannot carry HDR10 — use an HEVC or AV1 profile"
    if info.is_hdr and not profile.ten_bit:
        return "HDR source needs a 10-bit profile to keep HDR10"
    if info.dolby_vision and not info.is_hdr:
        # Profile 5-style DV has no HDR10 base layer; re-encoding it gives wrong colours.
        return "Dolby Vision without an HDR10 base layer: cannot be re-encoded safely"
    if (
        profile.rate_control == RateControl.BITRATE
        and profile.video_bitrate_kbps
        and not _downscales(info, profile)
    ):
        rate, exact = source_video_rate(info)
        if rate is None:
            # Never encode blind: a 6 Mbps DVD "re-encoded" at 60 Mbps would grow tenfold.
            return "Could not read the source bitrate; not re-encoding to a fixed bitrate"
        if rate <= profile.video_bitrate_kbps * 1000 * ratio:
            what = "Source video is" if exact else "Source (video + audio) is about"
            return (
                f"{what} {rate / 1e6:.1f} Mbps, already at or below the "
                f"{profile.video_bitrate_kbps / 1000:g} Mbps target"
            )
    return None


def source_video_rate(info: MediaInfo) -> tuple[int | None, bool]:
    """Best estimate of the source video bitrate (bps) and whether it is video-only.

    Falls back to the whole-file rate (video + audio, so an over-estimate) when the
    container gives no per-stream figure.
    """
    if info.video_bit_rate:
        return info.video_bit_rate, True
    if info.format_bit_rate:
        return info.format_bit_rate, False
    if info.size_bytes and info.duration > 0:
        return int(info.size_bytes * 8 / info.duration), False
    return None, False


def _downscales(info: MediaInfo, profile: EncodeProfile) -> bool:
    return bool(profile.max_height and info.height > profile.max_height)


def kept_audio(info: MediaInfo, profile: EncodeProfile) -> list[AudioStream]:
    """Audio streams to copy. A language filter that would keep nothing keeps everything."""
    wanted = {c.strip().lower() for c in profile.audio_languages.split(",") if c.strip()}
    if not wanted:
        return list(info.audio)
    from app.core.languages import normalize_language  # local: keeps this module light

    norm = {normalize_language(c) or c for c in wanted}
    keep = [
        a
        for a in info.audio
        if a.language == "und" or (normalize_language(a.language) or a.language) in norm
    ]
    return keep or list(info.audio)


def estimate_size_bytes(info: MediaInfo, profile: EncodeProfile) -> int | None:
    """Expected output size; None for constant-quality (size depends on content)."""
    if profile.encoder == VideoEncoder.COPY:
        return info.size_bytes
    if profile.rate_control != RateControl.BITRATE or not profile.video_bitrate_kbps:
        return None
    audio_bps = output_audio_bps(info, profile)
    total_bps = profile.video_bitrate_kbps * 1000 + audio_bps
    return int(total_bps * info.duration / 8)


# ---------------------------------------------------------------------------
# Command building
# ---------------------------------------------------------------------------


def _x265_master_display(md: dict) -> str | None:
    """ffprobe mastering metadata -> x265 ``master-display`` (G,B,R,WP,L order)."""

    def chroma(key: str) -> int:
        return round(Fraction(md[key]) * 50000)

    def lum(key: str) -> int:
        return round(Fraction(md[key]) * 10000)

    try:
        return (
            f"G({chroma('green_x')},{chroma('green_y')})"
            f"B({chroma('blue_x')},{chroma('blue_y')})"
            f"R({chroma('red_x')},{chroma('red_y')})"
            f"WP({chroma('white_point_x')},{chroma('white_point_y')})"
            f"L({lum('max_luminance')},{lum('min_luminance')})"
        )
    except (KeyError, ValueError, ZeroDivisionError):
        return None


def _video_filters(info: MediaInfo, profile: EncodeProfile) -> list[str]:
    filters: list[str] = []
    if info.field_order in INTERLACED_FIELD_ORDERS:
        filters.append("bwdif=mode=send_frame:parity=auto:deint=interlaced")
    if _downscales(info, profile):
        filters.append(f"scale=-2:{profile.max_height}:flags=lanczos")
    return filters


def _color_args(info: MediaInfo) -> list[str]:
    args: list[str] = []
    for flag, value in (
        ("-color_primaries", info.color_primaries),
        ("-color_trc", info.color_transfer),
        ("-colorspace", info.color_space),
    ):
        if value and value != "unknown":
            args += [flag, value]
    return args


def _video_args(info: MediaInfo, profile: EncodeProfile) -> list[str]:
    enc = profile.encoder
    ten_bit = profile.ten_bit and ENCODER_FAMILY.get(enc) != "h264"
    bitrate = profile.rate_control == RateControl.BITRATE
    kbps = profile.video_bitrate_kbps
    maxk = profile.max_bitrate_kbps
    args = ["-c:v", enc.value]

    if enc in (VideoEncoder.HEVC_NVENC, VideoEncoder.H264_NVENC):
        args += ["-preset", profile.preset or "p7", "-tune", "hq", "-rc", "vbr"]
        args += ["-spatial-aq", "1", "-temporal-aq", "1", "-rc-lookahead", "32"]
        if enc == VideoEncoder.HEVC_NVENC:
            args += ["-profile:v", "main10" if ten_bit else "main"]
        else:
            args += ["-profile:v", "high"]
        args += ["-pix_fmt", "p010le" if ten_bit else "yuv420p"]
        if bitrate and kbps:
            args += ["-b:v", f"{kbps}k"]
            if profile.multipass:
                args += ["-multipass", "fullres"]
        else:
            args += ["-cq", str(profile.quality or 22), "-b:v", "0"]
        if maxk:
            args += ["-maxrate", f"{maxk}k", "-bufsize", f"{maxk * 2}k"]

    elif enc in (VideoEncoder.LIBX265, VideoEncoder.LIBX264):
        args += ["-preset", profile.preset or "slow"]
        if enc == VideoEncoder.LIBX265:
            args += ["-profile:v", "main10" if ten_bit else "main"]
        args += ["-pix_fmt", "yuv420p10le" if ten_bit else "yuv420p"]
        if bitrate and kbps:
            args += ["-b:v", f"{kbps}k"]
        else:
            args += ["-crf", str(profile.quality or 20)]
        params: list[str] = []
        if maxk:
            if enc == VideoEncoder.LIBX265:
                params += [f"vbv-maxrate={maxk}", f"vbv-bufsize={maxk * 2}"]
            else:
                args += ["-maxrate", f"{maxk}k", "-bufsize", f"{maxk * 2}k"]
        if enc == VideoEncoder.LIBX265 and info.is_hdr and ten_bit:
            params += ["hdr10=1", "repeat-headers=1", "hdr10-opt=1"]
            params += [
                f"colorprim={info.color_primaries or 'bt2020'}",
                f"transfer={info.color_transfer}",
                f"colormatrix={info.color_space or 'bt2020nc'}",
            ]
            if info.mastering_display and (md := _x265_master_display(info.mastering_display)):
                params.append(f"master-display={md}")
            if info.content_light:
                cll = info.content_light
                params.append(
                    f"max-cll={_int(cll.get('max_content')) or 0},"
                    f"{_int(cll.get('max_average')) or 0}"
                )
        if params:
            args += [f"-{enc.value.removeprefix('lib')}-params", ":".join(params)]

    elif enc == VideoEncoder.LIBSVTAV1:
        args += ["-preset", profile.preset or "6"]
        args += ["-pix_fmt", "yuv420p10le" if ten_bit else "yuv420p"]
        if bitrate and kbps:
            # SVT-AV1 rejects a max bitrate outside CRF mode ("Max Bitrate only
            # supported with CRF mode"), so the peak cap only applies to quality profiles.
            args += ["-b:v", f"{kbps}k"]
        else:
            args += ["-crf", str(profile.quality or 28)]
            if maxk:
                args += ["-maxrate", f"{maxk}k"]

    return args + _color_args(info)


def build_command(
    ffmpeg: str,
    source: str | Path,
    output: str | Path,
    profile: EncodeProfile,
    info: MediaInfo,
    *,
    hw_decode: bool = True,
) -> list[str]:
    """The full ffmpeg invocation: encode video, copy everything else, write MKV."""
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-nostats", "-progress", "pipe:1"]
    if (
        hw_decode
        and profile.encoder in (VideoEncoder.HEVC_NVENC, VideoEncoder.H264_NVENC)
        and info.video_codec in NVDEC_CODECS
    ):
        # Decoded frames come back to system memory, so CPU filters (deinterlace,
        # scale, 10-bit conversion) keep working without a CUDA filter chain.
        cmd += ["-hwaccel", "cuda"]
    cmd += ["-i", str(source)]

    audio = kept_audio(info, profile)
    cmd += ["-map", "0:v:0"]
    if len(audio) == len(info.audio):
        cmd += ["-map", "0:a?"]
    else:
        for a in audio:
            cmd += ["-map", f"0:a:{a.index}"]
    stereo_from = audio[0] if (profile.add_stereo_aac and audio) else None
    if stereo_from is not None:
        cmd += ["-map", f"0:a:{stereo_from.index}"]
    cmd += ["-map", "0:s?", "-map", "0:t?", "-map_metadata", "0", "-map_chapters", "0"]

    cmd += ["-c", "copy"]  # Every stream copied unless overridden below
    filters = _video_filters(info, profile)
    if filters:
        cmd += ["-vf", ",".join(filters)]
    cmd += _video_args(info, profile)
    for tag in _STALE_VIDEO_TAGS:
        cmd += ["-metadata:s:v:0", f"{tag}="]

    if stereo_from is not None:
        n = len(audio)  # The extra track is the last audio stream in the output
        cmd += [
            f"-c:a:{n}",
            "aac",
            f"-b:a:{n}",
            f"{profile.stereo_aac_kbps}k",
            f"-ac:a:{n}",
            "2",
            f"-metadata:s:a:{n}",
            "title=Stereo (AAC)",
            f"-metadata:s:a:{n}",
            f"language={stereo_from.language}",
            f"-disposition:a:{n}",
            "0",
        ]

    cmd += [
        "-metadata",
        f"{ENCODE_TAG}={profile.name}",
        "-max_muxing_queue_size",
        "4096",
        "-f",
        "matroska",
        str(output),
    ]
    return cmd


def output_audio_bps(info: MediaInfo, profile: EncodeProfile) -> int:
    """Bitrate of the audio the output carries (copied tracks + optional AAC), in bps."""
    bps = sum(a.bit_rate or 0 for a in kept_audio(info, profile))
    if profile.add_stereo_aac and info.audio:
        bps += profile.stereo_aac_kbps * 1000
    return bps


def expected_audio_count(info: MediaInfo, profile: EncodeProfile) -> int:
    kept = kept_audio(info, profile)
    return len(kept) + (1 if profile.add_stereo_aac and kept else 0)


# ---------------------------------------------------------------------------
# Progress + verification
# ---------------------------------------------------------------------------


@dataclass
class Progress:
    out_seconds: float = 0.0
    fps: float | None = None
    speed: float | None = None
    total_size: int | None = None  # Bytes written so far (all streams)
    finished: bool = False

    def percent(self, duration: float) -> float:
        if duration <= 0:
            return 0.0
        return max(0.0, min(100.0, self.out_seconds / duration * 100))

    def video_kbps(self, audio_bps: int = 0) -> int | None:
        """Live video bitrate: output bytes so far over output time, minus copied audio."""
        if not self.total_size or self.out_seconds < 2:
            return None
        total_bps = self.total_size * 8 / self.out_seconds
        video_bps = total_bps - audio_bps
        return round(video_bps / 1000) if video_bps > 0 else None

    def eta(self, duration: float) -> int | None:
        if not self.speed or self.speed <= 0 or duration <= 0:
            return None
        return max(0, int((duration - self.out_seconds) / self.speed))


def parse_progress_line(line: str, state: Progress) -> bool:
    """Fold one ``-progress`` key=value line into ``state``.

    Returns True at the end of a progress block (``progress=continue|end``),
    which is when the caller should publish an update.
    """
    key, _, value = line.strip().partition("=")
    if key in ("out_time_us", "out_time_ms"):  # Both are microseconds (ffmpeg quirk)
        if (v := _int(value)) is not None and v >= 0:
            state.out_seconds = v / 1_000_000
    elif key == "fps":
        state.fps = _float(value) or state.fps
    elif key == "total_size":
        if (v := _int(value)) is not None and v >= 0:
            state.total_size = v
    elif key == "speed":
        state.speed = _float(value.rstrip("x").strip()) or state.speed
    elif key == "progress":
        state.finished = value == "end"
        return True
    return False


def verify_output(source: MediaInfo, output: MediaInfo, profile: EncodeProfile) -> list[str]:
    """Problems that make an encode unsafe to swap into the library (empty = OK)."""
    problems: list[str] = []
    tolerance = max(2.0, source.duration * 0.005)
    if abs(output.duration - source.duration) > tolerance:
        problems.append(
            f"duration {output.duration:.1f}s differs from source {source.duration:.1f}s"
        )
    want_codec = ENCODER_OUTPUT_CODEC.get(profile.encoder)
    if want_codec and output.video_codec != want_codec:
        problems.append(f"video codec is {output.video_codec!r}, expected {want_codec!r}")
    want_audio = expected_audio_count(source, profile)
    if len(output.audio) != want_audio:
        problems.append(f"{len(output.audio)} audio tracks, expected {want_audio}")
    else:
        originals = kept_audio(source, profile)
        for src, out in zip(originals, output.audio, strict=False):
            if src.codec != out.codec:
                problems.append(f"audio track {out.index} is {out.codec}, source was {src.codec}")
    if output.subtitle_count != source.subtitle_count:
        problems.append(
            f"{output.subtitle_count} subtitle tracks, expected {source.subtitle_count}"
        )
    if source.is_hdr and output.color_transfer != source.color_transfer:
        problems.append(
            f"HDR transfer lost ({output.color_transfer or 'none'} vs {source.color_transfer})"
        )
    return problems


# ---------------------------------------------------------------------------
# Starter profiles
# ---------------------------------------------------------------------------

BUILTIN_PROFILES: list[dict] = [
    {
        "name": "Archive",
        "description": "Keep the lossless MakeMKV remux untouched.",
        "encoder": VideoEncoder.COPY,
        "rate_control": RateControl.BITRATE,
        "video_bitrate_kbps": None,
        "max_bitrate_kbps": None,
        "multipass": False,
        "preset": "",
        "ten_bit": False,
    },
    {
        "name": "4K UHD High",
        "description": "HEVC 10-bit NVENC, 60 Mbps VBR (80 peak). HDR10 kept, original audio.",
        "encoder": VideoEncoder.HEVC_NVENC,
        "rate_control": RateControl.BITRATE,
        "video_bitrate_kbps": 60_000,
        "max_bitrate_kbps": 80_000,
        "multipass": True,
        "preset": "p7",
        "ten_bit": True,
    },
    {
        "name": "4K HDR Standard",
        "description": "HEVC 10-bit NVENC, 20 Mbps. HDR10 kept, original audio.",
        "encoder": VideoEncoder.HEVC_NVENC,
        "rate_control": RateControl.BITRATE,
        "video_bitrate_kbps": 20_000,
        "max_bitrate_kbps": 40_000,
        "multipass": True,
        "preset": "p7",
        "ten_bit": True,
    },
    {
        "name": "1080p Standard",
        "description": "HEVC NVENC, 6 Mbps. Original audio.",
        "encoder": VideoEncoder.HEVC_NVENC,
        "rate_control": RateControl.BITRATE,
        "video_bitrate_kbps": 6_000,
        "max_bitrate_kbps": 10_000,
        "multipass": True,
        "preset": "p7",
        "ten_bit": True,
    },
    {
        "name": "TV Episodes",
        "description": "HEVC NVENC, constant quality CQ 22. Original audio.",
        "encoder": VideoEncoder.HEVC_NVENC,
        "rate_control": RateControl.QUALITY,
        "video_bitrate_kbps": None,
        "max_bitrate_kbps": 10_000,
        "quality": 22,
        "multipass": False,
        "preset": "p7",
        "ten_bit": True,
    },
    {
        "name": "Mobile",
        "description": "H.264 NVENC 720p, 2.5 Mbps. Original audio + AAC 192k stereo.",
        "encoder": VideoEncoder.H264_NVENC,
        "rate_control": RateControl.BITRATE,
        "video_bitrate_kbps": 2_500,
        "max_bitrate_kbps": 5_000,
        "multipass": True,
        "preset": "p7",
        "ten_bit": False,
        "max_height": 720,
        "add_stereo_aac": True,
        "stereo_aac_kbps": 192,
    },
]

# Default profile assignment by name when settings are first created. Every slot
# starts on "4K UHD High" (HEVC 60 Mbps): a source already at or below the target
# (x skip_if_source_below_ratio) is skipped and keeps its remux, so only files that
# need it are re-encoded down to 60 Mbps.
DEFAULT_ASSIGNMENT = {
    "movie_profile_id": "4K UHD High",
    "movie_4k_profile_id": "4K UHD High",
    "tv_profile_id": "4K UHD High",
}
