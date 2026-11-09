"""Encode stage: probe parsing, ffmpeg command building, skip rules, verification."""

import pytest

from app.core import encoder
from app.models.encode import EncodeProfile, RateControl, VideoEncoder


def _probe_json(
    *,
    codec="hevc",
    width=3840,
    height=2160,
    transfer="smpte2084",
    audio=(("truehd", 8, "eng"), ("ac3", 6, "eng"), ("dts", 6, "fre")),
    subtitles=3,
    video_tags=None,
    format_tags=None,
    format_bit_rate="75000000",
    field_order="progressive",
    dovi=False,
):
    video = {
        "codec_type": "video",
        "codec_name": codec,
        "width": width,
        "height": height,
        "pix_fmt": "yuv420p10le",
        "field_order": field_order,
        "color_primaries": "bt2020" if transfer == "smpte2084" else "bt709",
        "color_transfer": transfer,
        "color_space": "bt2020nc" if transfer == "smpte2084" else "bt709",
        "tags": video_tags if video_tags is not None else {"BPS-eng": "68000000"},
    }
    if dovi:
        video["side_data_list"] = [{"side_data_type": "DOVI configuration record"}]
    streams = [video]
    for c, ch, lang in audio:
        streams.append(
            {"codec_type": "audio", "codec_name": c, "channels": ch, "tags": {"language": lang}}
        )
    streams += [{"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle"}] * subtitles
    return {
        "streams": streams,
        "format": {
            "duration": "7200.0",
            "size": "60000000000",
            "bit_rate": format_bit_rate,
            "tags": format_tags or {},
        },
    }


MASTERING = {
    "side_data_type": "Mastering display metadata",
    "red_x": "34000/50000",
    "red_y": "16000/50000",
    "green_x": "13250/50000",
    "green_y": "34500/50000",
    "blue_x": "7500/50000",
    "blue_y": "3000/50000",
    "white_point_x": "15635/50000",
    "white_point_y": "16450/50000",
    "min_luminance": "50/10000",
    "max_luminance": "10000000/10000",
}


def _profile(name="4K UHD High", **kw) -> EncodeProfile:
    spec = next(p for p in encoder.BUILTIN_PROFILES if p["name"] == name)
    return EncodeProfile(**{**spec, **kw})


def _arg(cmd, flag):
    return cmd[cmd.index(flag) + 1]


class TestParseProbe:
    def test_uhd_hdr(self):
        info = encoder.parse_probe(_probe_json())
        assert (info.width, info.height, info.resolution_tier) == (3840, 2160, "2160p")
        assert info.is_hdr
        assert info.video_bit_rate == 68_000_000  # from MakeMKV's BPS-eng tag
        assert [a.codec for a in info.audio] == ["truehd", "ac3", "dts"]
        assert info.audio[2].language == "fre"
        assert info.subtitle_count == 3

    def test_video_bitrate_falls_back_to_format_minus_known_audio(self):
        data = _probe_json(video_tags={})
        data["streams"][1]["tags"]["BPS"] = "5000000"
        info = encoder.parse_probe(data)
        assert info.video_bit_rate == 70_000_000

    def test_mastering_display_from_first_frame(self):
        info = encoder.parse_probe(_probe_json(), {"side_data_list": [MASTERING]})
        assert info.mastering_display["max_luminance"] == "10000000/10000"

    def test_dolby_vision_detected(self):
        assert encoder.parse_probe(_probe_json(dovi=True)).dolby_vision

    def test_no_video_raises(self):
        with pytest.raises(ValueError):
            encoder.parse_probe({"streams": [], "format": {}})


class TestBuildCommand:
    def test_4k_60mbps_nvenc_keeps_everything_else(self):
        info = encoder.parse_probe(_probe_json())
        cmd = encoder.build_command("ffmpeg", "/m/in.mkv", "/m/.in.encoding.mkv", _profile(), info)

        assert cmd[cmd.index("-hwaccel") + 1] == "cuda"
        assert _arg(cmd, "-c:v") == "hevc_nvenc"
        assert _arg(cmd, "-b:v") == "60000k"
        assert _arg(cmd, "-maxrate") == "80000k"
        assert _arg(cmd, "-bufsize") == "160000k"
        assert _arg(cmd, "-multipass") == "fullres"
        assert _arg(cmd, "-preset") == "p7"
        assert _arg(cmd, "-profile:v") == "main10"
        assert _arg(cmd, "-pix_fmt") == "p010le"
        # HDR10 signalling carried from the source
        assert _arg(cmd, "-color_trc") == "smpte2084"
        assert _arg(cmd, "-color_primaries") == "bt2020"
        assert _arg(cmd, "-colorspace") == "bt2020nc"
        # Audio + subs + attachments + chapters copied untouched; MKV out
        assert _arg(cmd, "-c") == "copy"
        assert "0:a?" in cmd and "0:s?" in cmd and "0:t?" in cmd
        assert _arg(cmd, "-map_chapters") == "0"
        assert not any(a.startswith("-c:a") for a in cmd)
        assert cmd[-3:] == ["-f", "matroska", "/m/.in.encoding.mkv"]
        assert f"{encoder.ENCODE_TAG}=4K UHD High" in cmd
        assert "BPS-eng=" in cmd  # stale remux stats cleared on video

    def test_quality_mode_nvenc(self):
        info = encoder.parse_probe(_probe_json(height=1080, width=1920, transfer="bt709"))
        cmd = encoder.build_command("ffmpeg", "a", "b", _profile("TV Episodes"), info)
        assert _arg(cmd, "-cq") == "22"
        assert _arg(cmd, "-b:v") == "0"
        assert "-multipass" not in cmd

    def test_mobile_downscale_and_stereo_track(self):
        info = encoder.parse_probe(
            _probe_json(height=1080, width=1920, transfer="bt709", subtitles=1)
        )
        cmd = encoder.build_command("ffmpeg", "a", "b", _profile("Mobile"), info)
        assert _arg(cmd, "-vf") == "scale=-2:720:flags=lanczos"
        assert _arg(cmd, "-c:v") == "h264_nvenc"
        assert _arg(cmd, "-pix_fmt") == "yuv420p"
        # 3 originals copied, AAC 2.0 added as audio stream 3 from the first track
        assert cmd.count("0:a:0") == 1
        assert _arg(cmd, "-c:a:3") == "aac"
        assert _arg(cmd, "-b:a:3") == "192k"
        assert _arg(cmd, "-ac:a:3") == "2"
        assert encoder.expected_audio_count(info, _profile("Mobile")) == 4

    def test_language_filter_maps_only_kept_tracks(self):
        info = encoder.parse_probe(_probe_json())
        prof = _profile(audio_languages="eng")
        cmd = encoder.build_command("ffmpeg", "a", "b", prof, info)
        assert "0:a?" not in cmd
        assert "0:a:0" in cmd and "0:a:1" in cmd and "0:a:2" not in cmd

    def test_language_filter_matching_nothing_keeps_all(self):
        info = encoder.parse_probe(_probe_json())
        assert len(encoder.kept_audio(info, _profile(audio_languages="jpn"))) == 3

    def test_interlaced_dvd_is_deinterlaced_and_decoded_on_gpu(self):
        info = encoder.parse_probe(
            _probe_json(codec="mpeg2video", width=720, height=480, transfer="", field_order="tt")
        )
        cmd = encoder.build_command(
            "ffmpeg", "a", "b", _profile("1080p Standard", video_bitrate_kbps=2000), info
        )
        assert _arg(cmd, "-vf").startswith("bwdif=")
        assert "-hwaccel" in cmd

    def test_x265_hdr_params(self):
        info = encoder.parse_probe(_probe_json(), {"side_data_list": [MASTERING]})
        prof = _profile(encoder=VideoEncoder.LIBX265, preset="slow")
        cmd = encoder.build_command("ffmpeg", "a", "b", prof, info)
        assert "-hwaccel" not in cmd
        params = _arg(cmd, "-x265-params")
        assert "hdr10=1" in params
        assert "vbv-maxrate=80000" in params
        assert (
            "master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)"
        ) in params

    def test_svtav1_crf(self):
        info = encoder.parse_probe(_probe_json())
        prof = _profile(
            encoder=VideoEncoder.LIBSVTAV1,
            rate_control=RateControl.QUALITY,
            quality=26,
            preset="6",
        )
        cmd = encoder.build_command("ffmpeg", "a", "b", prof, info)
        assert _arg(cmd, "-c:v") == "libsvtav1"
        assert _arg(cmd, "-crf") == "26"


class TestSkipReason:
    def test_archive_profile_skips(self):
        info = encoder.parse_probe(_probe_json())
        assert "Archive" in encoder.skip_reason(info, _profile("Archive"))

    def test_source_already_below_target(self):
        # 1080p Blu-ray at 30 Mbps vs a 60 Mbps target: re-encoding only grows it
        info = encoder.parse_probe(_probe_json(video_tags={"BPS": "30000000"}))
        assert "already at or below" in encoder.skip_reason(info, _profile())

    def test_high_bitrate_source_is_encoded(self):
        info = encoder.parse_probe(_probe_json(video_tags={"BPS": "90000000"}))
        assert encoder.skip_reason(info, _profile()) is None

    def test_downscale_is_never_skipped_for_bitrate(self):
        info = encoder.parse_probe(_probe_json(video_tags={"BPS": "1000000"}))
        assert encoder.skip_reason(info, _profile("4K UHD High", max_height=1080)) is None

    def test_hdr_into_h264_refused(self):
        info = encoder.parse_probe(_probe_json(video_tags={"BPS": "90000000"}))
        assert "H.264" in encoder.skip_reason(info, _profile("Mobile"))

    def test_already_encoded_tag(self):
        info = encoder.parse_probe(_probe_json(format_tags={encoder.ENCODE_TAG: "4K UHD High"}))
        assert "Already encoded" in encoder.skip_reason(info, _profile())


class TestVerify:
    def _out(self, **kw):
        base = dict(video_tags={})
        base.update(kw)
        return encoder.parse_probe(_probe_json(**base))

    def test_good_output(self):
        src = encoder.parse_probe(_probe_json())
        assert encoder.verify_output(src, self._out(), _profile()) == []

    def test_missing_audio_track(self):
        src = encoder.parse_probe(_probe_json())
        out = self._out(audio=(("truehd", 8, "eng"),))
        assert any("audio tracks" in p for p in encoder.verify_output(src, out, _profile()))

    def test_audio_reencoded_detected(self):
        src = encoder.parse_probe(_probe_json())
        out = self._out(audio=(("aac", 2, "eng"), ("ac3", 6, "eng"), ("dts", 6, "fre")))
        assert any("truehd" in p for p in encoder.verify_output(src, out, _profile()))

    def test_lost_hdr_and_subs(self):
        src = encoder.parse_probe(_probe_json())
        out = self._out(transfer="bt709", subtitles=0)
        problems = encoder.verify_output(src, out, _profile())
        assert any("HDR" in p for p in problems)
        assert any("subtitle" in p for p in problems)

    def test_truncated_output(self):
        src = encoder.parse_probe(_probe_json())
        out_data = _probe_json(video_tags={})
        out_data["format"]["duration"] = "3600"
        out = encoder.parse_probe(out_data)
        assert any("duration" in p for p in encoder.verify_output(src, out, _profile()))


class TestProgressAndEstimate:
    def test_progress_block(self):
        state = encoder.Progress()
        lines = ["frame=100", "fps=95.5", "out_time_us=1800000000", "speed=3.2x"]
        assert not any(encoder.parse_progress_line(line, state) for line in lines)
        assert encoder.parse_progress_line("progress=continue", state)
        assert state.percent(7200) == 25.0
        assert state.fps == 95.5
        assert state.eta(7200) == int(5400 / 3.2)
        encoder.parse_progress_line("progress=end", state)
        assert state.finished

    def test_negative_out_time_ignored(self):
        state = encoder.Progress()
        encoder.parse_progress_line("out_time_us=-9223372036854775807", state)
        assert state.out_seconds == 0

    def test_estimate_60mbps_two_hours(self):
        info = encoder.parse_probe(_probe_json())
        size = encoder.estimate_size_bytes(info, _profile())
        assert size == int(60_000_000 * 7200 / 8)  # audio bitrates unknown in fixture

    def test_quality_mode_has_no_estimate(self):
        info = encoder.parse_probe(_probe_json())
        assert encoder.estimate_size_bytes(info, _profile("TV Episodes")) is None


def test_bitrate_catalog_offers_60mbps_4k():
    assert 60 in encoder.VIDEO_BITRATE_PRESETS_MBPS["2160p"]["hevc"]
    assert 60 in encoder.VIDEO_BITRATE_PRESETS_MBPS["2160p"]["av1"]
