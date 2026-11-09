/** Encode stage API (fork feature): profiles, settings, queue. */
import { apiFetch, apiFetchVoid } from "./client";

export type VideoEncoder =
  | "copy"
  | "hevc_nvenc"
  | "h264_nvenc"
  | "libx265"
  | "libx264"
  | "libsvtav1";
export type RateControl = "bitrate" | "quality";
export type EncodeTaskState =
  | "queued"
  | "running"
  | "verifying"
  | "done"
  | "skipped"
  | "failed"
  | "cancelled";

export interface EncodeProfile {
  id: number;
  name: string;
  description: string;
  builtin: boolean;
  encoder: VideoEncoder;
  rate_control: RateControl;
  video_bitrate_kbps: number | null;
  max_bitrate_kbps: number | null;
  quality: number | null;
  multipass: boolean;
  preset: string;
  ten_bit: boolean;
  max_height: number | null;
  add_stereo_aac: boolean;
  stereo_aac_kbps: number;
  audio_languages: string;
}

export type EncodeProfileInput = Omit<EncodeProfile, "id" | "builtin">;

export interface EncodeSettings {
  enabled: boolean;
  movie_profile_id: number | null;
  movie_4k_profile_id: number | null;
  tv_profile_id: number | null;
  include_extras: boolean;
  concurrency: number;
  keep_original: boolean;
  archive_path: string;
  skip_if_source_below_ratio: number;
}

export interface EncodeTask {
  id: number;
  job_id: number | null;
  source_path: string;
  filename: string;
  profile_id: number;
  profile_name: string;
  state: EncodeTaskState;
  progress: number;
  fps: number | null;
  speed: number | null;
  eta_seconds: number | null;
  duration_seconds: number | null;
  source_bytes: number | null;
  output_bytes: number | null;
  error: string | null;
  note: string | null;
  command: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface EncodeOptions {
  ffmpeg: string | null;
  hardware: Record<string, boolean>;
  encoders: { id: VideoEncoder; family: string; hardware: boolean }[];
  video_bitrate_presets_mbps: Record<string, Record<string, number[]>>;
  max_bitrate_presets_mbps: number[];
  quality_range: [number, number];
  stereo_aac_kbps_presets: number[];
  presets: { nvenc: string[]; x26x: string[]; svtav1: string[] };
}

const BASE = "/api/encode";
const json = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const encodeApi = {
  options: (refresh = false) =>
    apiFetch<EncodeOptions>(`${BASE}/options${refresh ? "?refresh_hardware=true" : ""}`),
  profiles: () => apiFetch<EncodeProfile[]>(`${BASE}/profiles`),
  createProfile: (p: EncodeProfileInput) =>
    apiFetch<EncodeProfile>(`${BASE}/profiles`, json("POST", p)),
  updateProfile: (id: number, p: EncodeProfileInput) =>
    apiFetch<EncodeProfile>(`${BASE}/profiles/${id}`, json("PUT", p)),
  deleteProfile: (id: number) => apiFetchVoid(`${BASE}/profiles/${id}`, { method: "DELETE" }),
  settings: () => apiFetch<EncodeSettings>(`${BASE}/settings`),
  saveSettings: (s: EncodeSettings) =>
    apiFetch<EncodeSettings>(`${BASE}/settings`, json("PUT", s)),
  tasks: () => apiFetch<EncodeTask[]>(`${BASE}/tasks`),
  enqueuePath: (path: string, profile_id: number) =>
    apiFetch<EncodeTask[]>(`${BASE}/tasks`, json("POST", { path, profile_id })),
  enqueueJob: (job_id: number) =>
    apiFetch<EncodeTask[]>(`${BASE}/tasks`, json("POST", { job_id })),
  cancel: (id: number) => apiFetchVoid(`${BASE}/tasks/${id}/cancel`, { method: "POST" }),
  retry: (id: number) => apiFetchVoid(`${BASE}/tasks/${id}/retry`, { method: "POST" }),
  clearFinished: () => apiFetchVoid(`${BASE}/tasks/finished`, { method: "DELETE" }),
};

/** Estimated size of a bitrate profile's output, in GB, for a runtime in minutes. */
export function estimateGb(p: Pick<EncodeProfile, "rate_control" | "video_bitrate_kbps" | "encoder">, minutes: number, audioKbps = 4500): number | null {
  if (p.encoder === "copy" || p.rate_control !== "bitrate" || !p.video_bitrate_kbps) return null;
  return ((p.video_bitrate_kbps + audioKbps) * 1000 * minutes * 60) / 8 / 1e9;
}
