/** Fork (encode stage): shared formatting for encode tasks. */
import type { EncodeTask } from "../api/encodeClient";

const ENCODER_SHORT: Record<string, string> = {
  hevc_nvenc: "HEVC NVENC",
  h264_nvenc: "H.264 NVENC",
  libx265: "x265",
  libx264: "x264",
  libsvtav1: "SVT-AV1",
  copy: "Copy",
};

export function mbps(kbps: number | null | undefined, digits = 1): string {
  if (!kbps) return "—";
  return `${(kbps / 1000).toLocaleString(undefined, { maximumFractionDigits: digits })} Mbps`;
}

export function encoderLabel(enc: string | null | undefined): string {
  return enc ? (ENCODER_SHORT[enc] ?? enc) : "";
}

/** "60 Mbps (peak 80)" or "CQ 22". */
export function targetLabel(t: Pick<EncodeTask, "target_kbps" | "peak_kbps" | "quality">): string {
  if (t.target_kbps) return `${mbps(t.target_kbps, 1)}${t.peak_kbps ? ` (peak ${(t.peak_kbps / 1000).toLocaleString()})` : ""}`;
  if (t.quality) return `CQ ${t.quality}${t.peak_kbps ? ` (cap ${mbps(t.peak_kbps)})` : ""}`;
  return "";
}

export function clockTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso.endsWith("Z") || /[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`);
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

export function elapsed(fromIso: string | null | undefined, toIso?: string | null, now = Date.now()): string {
  if (!fromIso) return "";
  const parse = (s: string) => new Date(s.endsWith("Z") || /[+-]\d\d:\d\d$/.test(s) ? s : `${s}Z`).getTime();
  const secs = Math.max(0, Math.round(((toIso ? parse(toIso) : now) - parse(fromIso)) / 1000));
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m`;
  return `${secs}s`;
}
