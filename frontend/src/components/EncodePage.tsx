/**
 * Encode page (fork feature): encode settings, the live encode queue, and the
 * profile editor. Video is re-encoded; original audio/subtitles are always kept
 * and the output is always MKV, so the editor only exposes video choices.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import {
  SvActionButton,
  SvAtmosphere,
  SvBadge,
  type SvBadgeState,
  SvLabel,
  SvNotice,
  SvPageHeader,
  SvPanel,
  SvProgressBar,
  sv,
} from "../app/components/synapse";
import {
  encodeApi,
  estimateGb,
  type EncodeOptions,
  type EncodeProfile,
  type EncodeProfileInput,
  type EncodeSettings,
  type EncodeTask,
  type EncodeTaskState,
  type VideoEncoder,
} from "../api/encodeClient";
import { apiErrorDetail, ApiError } from "../api/client";
import { useEncodeTasks } from "../hooks/useEncodeTasks";
import { clockTime, elapsed, encoderLabel, mbps, targetLabel } from "./encodeFormat";

// ---------------------------------------------------------------------------
// Small presentational helpers
// ---------------------------------------------------------------------------

const ENCODER_LABEL: Record<VideoEncoder, string> = {
  copy: "Remux only (no encode)",
  hevc_nvenc: "HEVC / H.265 — NVIDIA NVENC",
  h264_nvenc: "H.264 — NVIDIA NVENC",
  libx265: "HEVC / H.265 — CPU (x265)",
  libx264: "H.264 — CPU (x264)",
  libsvtav1: "AV1 — CPU (SVT-AV1, slow)",
};

const STATE_BADGE: Record<EncodeTaskState, SvBadgeState> = {
  queued: "queued",
  running: "ripping",
  verifying: "matching",
  done: "complete",
  skipped: "idle",
  failed: "error",
  cancelled: "idle",
};

const HEIGHTS: { label: string; value: number | null }[] = [
  { label: "Keep source", value: null },
  { label: "2160p", value: 2160 },
  { label: "1080p", value: 1080 },
  { label: "720p", value: 720 },
  { label: "480p", value: 480 },
];

const inputStyle: CSSProperties = {
  width: "100%",
  boxSizing: "border-box",
  background: sv.bg1,
  color: sv.ink,
  border: `1px solid ${sv.lineMid}`,
  padding: "7px 9px",
  fontFamily: sv.mono,
  fontSize: 12,
  borderRadius: 0,
};

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 5, minWidth: 0 }}>
      <SvLabel>{label}</SvLabel>
      {children}
      {hint && (
        <span style={{ fontFamily: sv.mono, fontSize: 10, color: sv.inkFaint, lineHeight: 1.4 }}>{hint}</span>
      )}
    </label>
  );
}

function Check({ checked, onChange, children }: { checked: boolean; onChange: (v: boolean) => void; children: ReactNode }) {
  return (
    <label style={{ display: "flex", alignItems: "center", gap: 8, fontFamily: sv.mono, fontSize: 12, color: sv.ink, cursor: "pointer" }}>
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} style={{ accentColor: sv.cyan }} />
      {children}
    </label>
  );
}

function SectionTitle({ children, right }: { children: ReactNode; right?: ReactNode }) {
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap", marginBottom: 14 }}>
      <h2 style={{ margin: 0, fontFamily: sv.display, fontSize: 15, fontWeight: 700, letterSpacing: "0.14em", textTransform: "uppercase", color: sv.cyanHi }}>
        {children}
      </h2>
      {right}
    </div>
  );
}

const grid = (min: number): CSSProperties => ({
  display: "grid",
  gridTemplateColumns: `repeat(auto-fit, minmax(min(${min}px, 100%), 1fr))`,
  gap: 14,
});

function fmtMbps(kbps: number | null): string {
  return kbps ? `${(kbps / 1000).toLocaleString(undefined, { maximumFractionDigits: 1 })} Mbps` : "—";
}

function fmtGb(bytes: number | null): string {
  return bytes ? `${(bytes / 1e9).toFixed(1)} GB` : "—";
}

function fmtEta(s: number | null): string {
  if (s == null) return "";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${m}m left` : `${m}m left`;
}

function errorText(e: unknown): string {
  if (e instanceof ApiError) return apiErrorDetail(e.body) ?? e.message;
  return e instanceof Error ? e.message : String(e);
}

function profileSummary(p: EncodeProfile): string {
  if (p.encoder === "copy") return "Remux · original audio";
  const rate = p.rate_control === "bitrate" ? fmtMbps(p.video_bitrate_kbps) : `CQ ${p.quality}`;
  const cap = p.max_bitrate_kbps ? ` (peak ${fmtMbps(p.max_bitrate_kbps)})` : "";
  const res = p.max_height ? ` · ≤${p.max_height}p` : "";
  const aac = p.add_stereo_aac ? ` + AAC ${p.stereo_aac_kbps}k stereo` : "";
  return `${ENCODER_LABEL[p.encoder].split(" — ")[0]} ${p.ten_bit ? "10-bit " : ""}${rate}${cap}${res} · original audio${aac}`;
}

// ---------------------------------------------------------------------------
// Profile editor
// ---------------------------------------------------------------------------

const NEW_PROFILE: EncodeProfileInput = {
  name: "",
  description: "",
  encoder: "hevc_nvenc",
  rate_control: "bitrate",
  video_bitrate_kbps: 60_000,
  max_bitrate_kbps: 80_000,
  quality: 22,
  multipass: true,
  preset: "p7",
  ten_bit: true,
  max_height: null,
  add_stereo_aac: false,
  stereo_aac_kbps: 192,
  audio_languages: "",
};

function presetList(options: EncodeOptions, enc: VideoEncoder): string[] {
  if (enc === "hevc_nvenc" || enc === "h264_nvenc") return options.presets.nvenc;
  if (enc === "libsvtav1") return options.presets.svtav1;
  if (enc === "copy") return [""];
  return options.presets.x26x;
}

function defaultPreset(enc: VideoEncoder): string {
  return enc.endsWith("nvenc") ? "p7" : enc === "libsvtav1" ? "6" : enc === "copy" ? "" : "slow";
}

/** Bitrate select: tiered presets for the codec family plus a custom Mbps box. */
function BitrateInput({
  value,
  onChange,
  tiers,
  allowNone,
}: {
  value: number | null;
  onChange: (kbps: number | null) => void;
  tiers: { tier: string; mbps: number[] }[];
  allowNone?: boolean;
}) {
  const allKbps = tiers.flatMap((t) => t.mbps.map((m) => Math.round(m * 1000)));
  const isPreset = value == null ? !!allowNone : allKbps.includes(value);
  const [custom, setCustom] = useState(!isPreset);
  return (
    <div style={{ display: "flex", gap: 6 }}>
      <select
        style={inputStyle}
        value={custom ? "custom" : value == null ? "none" : String(value)}
        onChange={(e) => {
          const v = e.target.value;
          if (v === "custom") {
            setCustom(true);
          } else {
            setCustom(false);
            onChange(v === "none" ? null : Number(v));
          }
        }}
      >
        {allowNone && <option value="none">No cap</option>}
        {tiers.map((t) => (
          <optgroup key={t.tier} label={t.tier}>
            {t.mbps.map((m) => (
              <option key={`${t.tier}-${m}`} value={Math.round(m * 1000)}>
                {m} Mbps
              </option>
            ))}
          </optgroup>
        ))}
        <option value="custom">Custom…</option>
      </select>
      {custom && (
        <input
          aria-label="Custom bitrate in Mbps"
          style={{ ...inputStyle, width: 110 }}
          type="number"
          min={0.1}
          max={400}
          step={0.5}
          placeholder="Mbps"
          value={value ? value / 1000 : ""}
          onChange={(e) => onChange(e.target.value ? Math.round(Number(e.target.value) * 1000) : null)}
        />
      )}
    </div>
  );
}

function ProfileEditor({
  options,
  initial,
  onSave,
  onCancel,
}: {
  options: EncodeOptions;
  initial: EncodeProfileInput;
  onSave: (p: EncodeProfileInput) => Promise<void>;
  onCancel: () => void;
}) {
  const [p, setP] = useState<EncodeProfileInput>(initial);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = <K extends keyof EncodeProfileInput>(k: K, v: EncodeProfileInput[K]) => setP((x) => ({ ...x, [k]: v }));

  const family = options.encoders.find((e) => e.id === p.encoder)?.family ?? "hevc";
  const tiers = Object.entries(options.video_bitrate_presets_mbps)
    .map(([tier, fam]) => ({ tier, mbps: fam[family] ?? [] }))
    .filter((t) => t.mbps.length)
    .reverse(); // 2160p first
  const capTiers = [{ tier: "Peak cap", mbps: options.max_bitrate_presets_mbps }];
  const isCopy = p.encoder === "copy";
  const isH264 = family === "h264";
  const gb = estimateGb(p, 120);
  const hwMissing = p.encoder.endsWith("nvenc") && options.hardware[p.encoder] === false;

  const save = async () => {
    setBusy(true);
    setErr(null);
    try {
      await onSave({ ...p, ten_bit: isH264 ? false : p.ten_bit });
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <SvPanel pad={18} accent={sv.lineHi} style={{ marginBottom: 14 }}>
      <div style={{ ...grid(220), marginBottom: 14 }}>
        <Field label="Name">
          <input style={inputStyle} value={p.name} onChange={(e) => set("name", e.target.value)} placeholder="e.g. 4K UHD 60" />
        </Field>
        <Field label="Video encoder">
          <select
            style={inputStyle}
            value={p.encoder}
            onChange={(e) => {
              const enc = e.target.value as VideoEncoder;
              setP((x) => ({ ...x, encoder: enc, preset: defaultPreset(enc) }));
            }}
          >
            {options.encoders.map((e) => (
              <option key={e.id} value={e.id}>
                {ENCODER_LABEL[e.id]}
              </option>
            ))}
          </select>
        </Field>
        {!isCopy && (
          <Field label="Rate control">
            <select style={inputStyle} value={p.rate_control} onChange={(e) => set("rate_control", e.target.value as EncodeProfileInput["rate_control"])}>
              <option value="bitrate">Target bitrate</option>
              <option value="quality">Constant quality</option>
            </select>
          </Field>
        )}
      </div>

      {hwMissing && (
        <SvNotice tone="warn" style={{ marginBottom: 14 }}>
          NVENC was not detected on this server. Encodes with this profile will fail until the GPU is passed to the container.
        </SvNotice>
      )}

      {!isCopy && (
        <div style={{ ...grid(220), marginBottom: 14 }}>
          {p.rate_control === "bitrate" ? (
            <Field label="Video bitrate" hint={gb ? `≈ ${gb.toFixed(0)} GB for a 2-hour film (incl. ~4.5 Mbps lossless audio)` : undefined}>
              <BitrateInput value={p.video_bitrate_kbps} onChange={(v) => set("video_bitrate_kbps", v)} tiers={tiers} />
            </Field>
          ) : (
            <Field label={`Quality (CQ/CRF): ${p.quality ?? 22}`} hint="Lower = better quality, bigger files. 18 near-transparent, 22 high, 26 compact.">
              <input
                type="range"
                min={options.quality_range[0]}
                max={options.quality_range[1]}
                value={p.quality ?? 22}
                onChange={(e) => set("quality", Number(e.target.value))}
                style={{ accentColor: sv.cyan }}
              />
            </Field>
          )}
          <Field label="Peak bitrate cap" hint="Limits spikes for streaming. Must be ≥ the target.">
            <BitrateInput value={p.max_bitrate_kbps} onChange={(v) => set("max_bitrate_kbps", v)} tiers={capTiers} allowNone />
          </Field>
          <Field label="Encoder preset" hint={p.encoder.endsWith("nvenc") ? "p7 = slowest / best quality" : undefined}>
            <select style={inputStyle} value={p.preset} onChange={(e) => set("preset", e.target.value)}>
              {presetList(options, p.encoder).map((x) => (
                <option key={x} value={x}>
                  {x}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Resolution">
            <select style={inputStyle} value={p.max_height ?? ""} onChange={(e) => set("max_height", e.target.value ? Number(e.target.value) : null)}>
              {HEIGHTS.map((h) => (
                <option key={h.label} value={h.value ?? ""}>
                  {h.label}
                </option>
              ))}
            </select>
          </Field>
        </div>
      )}

      <div style={{ display: "flex", flexWrap: "wrap", gap: 18, marginBottom: 14 }}>
        {!isCopy && (
          <>
            <Check checked={!isH264 && p.ten_bit} onChange={(v) => set("ten_bit", v)}>
              10-bit {isH264 ? "(not available for H.264)" : "(needed to keep HDR10)"}
            </Check>
            {p.rate_control === "bitrate" && (
              <Check checked={p.multipass} onChange={(v) => set("multipass", v)}>
                Multipass (more accurate bitrate)
              </Check>
            )}
          </>
        )}
        <Check checked={p.add_stereo_aac} onChange={(v) => set("add_stereo_aac", v)}>
          Add AAC stereo compatibility track
        </Check>
        {p.add_stereo_aac && (
          <select style={{ ...inputStyle, width: 110 }} value={p.stereo_aac_kbps} onChange={(e) => set("stereo_aac_kbps", Number(e.target.value))}>
            {options.stereo_aac_kbps_presets.map((k) => (
              <option key={k} value={k}>
                {k} kbps
              </option>
            ))}
          </select>
        )}
      </div>

      <div style={{ ...grid(220), marginBottom: 14 }}>
        <Field label="Keep audio languages" hint="Comma-separated, e.g. eng,jpn. Blank keeps every track.">
          <input style={inputStyle} value={p.audio_languages} onChange={(e) => set("audio_languages", e.target.value)} placeholder="all" />
        </Field>
        <Field label="Description">
          <input style={inputStyle} value={p.description} onChange={(e) => set("description", e.target.value)} />
        </Field>
      </div>

      <SvNotice tone="info" style={{ marginBottom: 14 }}>
        Original audio (TrueHD/Atmos, DTS-HD MA/DTS:X, LPCM…) and all subtitles are always copied untouched. Output is always MKV.
      </SvNotice>

      {err && (
        <SvNotice tone="error" style={{ marginBottom: 14 }}>
          {err}
        </SvNotice>
      )}
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
        <SvActionButton tone="neutral" onClick={onCancel}>
          Cancel
        </SvActionButton>
        <SvActionButton tone="cyan" onClick={save} disabled={busy || !p.name.trim()}>
          Save profile
        </SvActionButton>
      </div>
    </SvPanel>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function EncodePage() {
  const navigate = useNavigate();
  const [options, setOptions] = useState<EncodeOptions | null>(null);
  const [profiles, setProfiles] = useState<EncodeProfile[]>([]);
  const [settings, setSettings] = useState<EncodeSettings | null>(null);
  const [savedNote, setSavedNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<EncodeProfile | "new" | null>(null);
  const [addPath, setAddPath] = useState("");
  const [addProfile, setAddProfile] = useState<number | "">("");
  const { tasks, refresh } = useEncodeTasks();
  const noteTimer = useRef<number | undefined>(undefined);

  const loadProfiles = useCallback(() => encodeApi.profiles().then(setProfiles), []);

  useEffect(() => {
    Promise.all([encodeApi.options(), loadProfiles(), encodeApi.settings()])
      .then(([o, , s]) => {
        setOptions(o);
        setSettings(s);
      })
      .catch((e) => setError(errorText(e)));
  }, [loadProfiles]);

  const flash = (msg: string) => {
    setSavedNote(msg);
    window.clearTimeout(noteTimer.current);
    noteTimer.current = window.setTimeout(() => setSavedNote(null), 2500);
  };

  const run = async (fn: () => Promise<unknown>, ok?: string) => {
    setError(null);
    try {
      await fn();
      if (ok) flash(ok);
    } catch (e) {
      setError(errorText(e));
    }
  };

  const saveSettings = (next: EncodeSettings) =>
    run(async () => setSettings(await encodeApi.saveSettings(next)), "Settings saved");

  const active = useMemo(() => tasks.filter((t) => ["queued", "running", "verifying"].includes(t.state)), [tasks]);
  const finished = useMemo(() => tasks.filter((t) => !["queued", "running", "verifying"].includes(t.state)), [tasks]);

  if (!options || !settings) {
    return (
      <SvAtmosphere>
        <SvPageHeader title="Encode" onBack={() => navigate("/")} />
        <div style={{ padding: 48, textAlign: "center", fontFamily: sv.mono, fontSize: 12, color: error ? sv.red : sv.cyan }}>
          {error ?? "› LOADING"}
        </div>
      </SvAtmosphere>
    );
  }

  const nvencOk = options.hardware.hevc_nvenc === true;
  const profileOptions = (value: number | null, onChange: (id: number | null) => void) => (
    <select style={inputStyle} value={value ?? ""} onChange={(e) => onChange(e.target.value ? Number(e.target.value) : null)}>
      <option value="">Don't encode</option>
      {profiles.map((p) => (
        <option key={p.id} value={p.id}>
          {p.name}
        </option>
      ))}
    </select>
  );

  return (
    <SvAtmosphere>
      <SvPageHeader
        title="Encode"
        subtitle="› Re-encode ripped video to your chosen codec and bitrate — original audio kept, MKV out"
        onBack={() => navigate("/")}
        right={
          <SvBadge state={settings.enabled ? "live" : "idle"} dot>
            {settings.enabled ? "Auto-encode on" : "Auto-encode off"}
          </SvBadge>
        }
      />
      <div style={{ maxWidth: 1200, margin: "0 auto", padding: "24px 16px 80px", display: "flex", flexDirection: "column", gap: 20 }}>
        {!options.ffmpeg && <SvNotice tone="error">ffmpeg was not found. Set its path in Settings.</SvNotice>}
        {options.ffmpeg && !nvencOk && (
          <SvNotice tone="warn">
            NVIDIA NVENC not available — GPU profiles will fail. On Unraid add <code>--runtime=nvidia</code> and{" "}
            <code>NVIDIA_VISIBLE_DEVICES</code> to the container.{" "}
            <button
              type="button"
              onClick={() => encodeApi.options(true).then(setOptions)}
              style={{ background: "none", border: "none", color: sv.cyan, cursor: "pointer", fontFamily: sv.mono, fontSize: 12, padding: 0 }}
            >
              Re-check
            </button>
          </SvNotice>
        )}
        {options.ffmpeg && nvencOk && <SvNotice tone="success">NVIDIA NVENC ready (HEVC{options.hardware.h264_nvenc ? " + H.264" : ""}).</SvNotice>}
        {error && (
          <SvNotice tone="error" onDismiss={() => setError(null)}>
            {error}
          </SvNotice>
        )}
        {savedNote && <SvNotice tone="success">{savedNote}</SvNotice>}

        {/* ---------------- Settings ---------------- */}
        <SvPanel pad={20}>
          <SectionTitle
            right={
              <SvActionButton tone="cyan" size="sm" onClick={() => saveSettings(settings)}>
                Save settings
              </SvActionButton>
            }
          >
            Settings
          </SectionTitle>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 18, marginBottom: 16 }}>
            <Check checked={settings.enabled} onChange={(v) => setSettings({ ...settings, enabled: v })}>
              Encode automatically when a disc finishes
            </Check>
            <Check checked={settings.include_extras} onChange={(v) => setSettings({ ...settings, include_extras: v })}>
              Include extras
            </Check>
          </div>
          <div style={{ ...grid(220), marginBottom: 16 }}>
            <Field label="Movies (1080p and below)">{profileOptions(settings.movie_profile_id, (id) => setSettings({ ...settings, movie_profile_id: id }))}</Field>
            <Field label="Movies (4K UHD)">{profileOptions(settings.movie_4k_profile_id, (id) => setSettings({ ...settings, movie_4k_profile_id: id }))}</Field>
            <Field label="TV episodes">{profileOptions(settings.tv_profile_id, (id) => setSettings({ ...settings, tv_profile_id: id }))}</Field>
            <Field label="Simultaneous encodes" hint="NVENC sessions are shared with Jellyfin transcodes.">
              <select style={inputStyle} value={settings.concurrency} onChange={(e) => setSettings({ ...settings, concurrency: Number(e.target.value) })}>
                {[1, 2, 3, 4].map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          <div style={grid(220)}>
            <Field label="After a verified encode">
              <select style={inputStyle} value={settings.keep_original ? "keep" : "delete"} onChange={(e) => setSettings({ ...settings, keep_original: e.target.value === "keep" })}>
                <option value="delete">Delete the original remux</option>
                <option value="keep">Keep the original remux</option>
              </select>
            </Field>
            {settings.keep_original && (
              <Field label="Archive folder" hint="Blank = hidden file beside the encode (media servers ignore it).">
                <input style={inputStyle} value={settings.archive_path} onChange={(e) => setSettings({ ...settings, archive_path: e.target.value })} placeholder="/media/archive" />
              </Field>
            )}
            <Field label="Skip if source is within" hint="Bitrate profiles skip sources already at/below target × this factor.">
              <select
                style={inputStyle}
                value={settings.skip_if_source_below_ratio}
                onChange={(e) => setSettings({ ...settings, skip_if_source_below_ratio: Number(e.target.value) })}
              >
                <option value={0}>Never skip</option>
                <option value={1}>1.0× target</option>
                <option value={1.1}>1.1× target</option>
                <option value={1.25}>1.25× target</option>
                <option value={1.5}>1.5× target</option>
              </select>
            </Field>
          </div>
        </SvPanel>

        {/* ---------------- Queue ---------------- */}
        <SvPanel pad={20}>
          <SectionTitle
            right={
              finished.length > 0 && (
                <SvActionButton tone="neutral" size="sm" onClick={() => run(encodeApi.clearFinished).then(refresh)}>
                  Clear finished
                </SvActionButton>
              )
            }
          >
            Queue {active.length > 0 && <span style={{ color: sv.inkDim }}>({active.length})</span>}
          </SectionTitle>

          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 16 }}>
            <input
              style={{ ...inputStyle, flex: "1 1 260px", width: "auto" }}
              placeholder="Library file path, e.g. /media/movies/Film (2024)/Film (2024).mkv"
              value={addPath}
              onChange={(e) => setAddPath(e.target.value)}
            />
            <select style={{ ...inputStyle, flex: "0 1 200px", width: "auto" }} value={addProfile} onChange={(e) => setAddProfile(e.target.value ? Number(e.target.value) : "")}>
              <option value="">Profile…</option>
              {profiles.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
            <SvActionButton
              tone="cyan"
              disabled={!addPath.trim() || addProfile === ""}
              onClick={() =>
                run(async () => {
                  const r = await encodeApi.enqueuePath(addPath.trim(), Number(addProfile));
                  if (!r.length) throw new Error("That file is already queued or encoding");
                  setAddPath("");
                }, "Added to queue")
              }
            >
              Add
            </SvActionButton>
          </div>

          {tasks.length === 0 && (
            <div style={{ fontFamily: sv.mono, fontSize: 12, color: sv.inkFaint, padding: "12px 0" }}>
              Nothing queued. Finished discs appear here when auto-encode is on.
            </div>
          )}
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {[...active, ...finished].map((t) => (
              <TaskRow key={t.id} task={t} onCancel={() => run(() => encodeApi.cancel(t.id))} onRetry={() => run(() => encodeApi.retry(t.id))} />
            ))}
          </div>
        </SvPanel>

        {/* ---------------- Profiles ---------------- */}
        <SvPanel pad={20}>
          <SectionTitle
            right={
              editing === null && (
                <SvActionButton tone="cyan" size="sm" onClick={() => setEditing("new")}>
                  New profile
                </SvActionButton>
              )
            }
          >
            Profiles
          </SectionTitle>
          {editing !== null && (
            <ProfileEditor
              key={editing === "new" ? "new" : editing.id}
              options={options}
              initial={editing === "new" ? NEW_PROFILE : (({ id: _id, builtin: _b, ...rest }) => rest)(editing)}
              onCancel={() => setEditing(null)}
              onSave={async (p) => {
                if (editing === "new") await encodeApi.createProfile(p);
                else await encodeApi.updateProfile(editing.id, p);
                await loadProfiles();
                setEditing(null);
                flash("Profile saved");
              }}
            />
          )}
          <div style={grid(320)}>
            {profiles.map((p) => (
              <div key={p.id} style={{ border: `1px solid ${sv.line}`, padding: 14, background: sv.bg1, display: "flex", flexDirection: "column", gap: 8 }}>
                <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "baseline" }}>
                  <span style={{ fontFamily: sv.display, fontWeight: 700, fontSize: 14, color: sv.ink, letterSpacing: "0.06em" }}>{p.name}</span>
                  {p.encoder.endsWith("nvenc") && (
                    <SvBadge tone={sv.green} size="sm">
                      GPU
                    </SvBadge>
                  )}
                </div>
                <span style={{ fontFamily: sv.mono, fontSize: 11, color: sv.inkDim, lineHeight: 1.5 }}>{profileSummary(p)}</span>
                {estimateGb(p, 120) && (
                  <span style={{ fontFamily: sv.mono, fontSize: 10, color: sv.inkFaint }}>≈ {estimateGb(p, 120)!.toFixed(0)} GB / 2 h film</span>
                )}
                <div style={{ display: "flex", gap: 6, marginTop: "auto" }}>
                  <SvActionButton tone="neutral" size="sm" onClick={() => setEditing(p)}>
                    Edit
                  </SvActionButton>
                  <SvActionButton
                    tone="red"
                    size="sm"
                    onClick={() => {
                      if (window.confirm(`Delete profile “${p.name}”?`)) run(() => encodeApi.deleteProfile(p.id).then(loadProfiles), "Profile deleted");
                    }}
                  >
                    Delete
                  </SvActionButton>
                </div>
              </div>
            ))}
          </div>
        </SvPanel>
      </div>
    </SvAtmosphere>
  );
}

/** When it started and the bitrates involved: source → target, live or final output. */
function RateLine({ task: t }: { task: EncodeTask }) {
  const parts: ReactNode[] = [];
  if (t.started_at) {
    const took = t.state === "done" ? `took ${elapsed(t.started_at, t.finished_at)}` : `${elapsed(t.started_at)} ago`;
    parts.push(
      <span key="start" title={t.started_at}>
        Started {clockTime(t.started_at)} · {took}
      </span>,
    );
  }
  if (t.encoder) parts.push(<span key="enc">{encoderLabel(t.encoder)}</span>);
  const target = targetLabel(t);
  if (target) parts.push(<span key="target">Target <b style={{ color: sv.ink }}>{target}</b></span>);
  if (t.source_video_kbps) parts.push(<span key="src">Source {mbps(t.source_video_kbps)}</span>);
  if (t.state === "running" && t.current_kbps)
    parts.push(
      <span key="now" title="Video bitrate so far (output size over time, minus the copied audio)">
        Now ≈ <b style={{ color: sv.magenta }}>{mbps(t.current_kbps)}</b>
      </span>,
    );
  if (t.state === "done" && t.output_video_kbps)
    parts.push(
      <span key="out">
        Output <b style={{ color: sv.green }}>{mbps(t.output_video_kbps)}</b>
      </span>,
    );
  if (!parts.length) return null;
  return (
    <div style={{ display: "flex", gap: 14, marginTop: 8, fontFamily: sv.mono, fontSize: 11, color: sv.inkDim, flexWrap: "wrap" }}>
      {parts}
    </div>
  );
}

function TaskRow({ task: t, onCancel, onRetry }: { task: EncodeTask; onCancel: () => void; onRetry: () => void }) {
  const [showCmd, setShowCmd] = useState(false);
  const live = t.state === "running" || t.state === "verifying";
  return (
    <div style={{ border: `1px solid ${live ? sv.lineHi : sv.line}`, padding: 12, background: sv.bg1 }}>
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <SvBadge state={STATE_BADGE[t.state]} size="sm">
          {t.state}
        </SvBadge>
        <span style={{ fontFamily: sv.mono, fontSize: 12, color: sv.ink, flex: "1 1 200px", minWidth: 0, overflowWrap: "anywhere" }} title={t.source_path}>
          {t.filename}
        </span>
        <span style={{ fontFamily: sv.mono, fontSize: 11, color: sv.inkDim }}>{t.profile_name}</span>
        {(t.state === "queued" || live) && (
          <SvActionButton tone="red" size="sm" onClick={onCancel}>
            Cancel
          </SvActionButton>
        )}
        {(t.state === "failed" || t.state === "cancelled") && (
          <SvActionButton tone="amber" size="sm" onClick={onRetry}>
            Retry
          </SvActionButton>
        )}
      </div>
      {(live || t.state === "done") && <RateLine task={t} />}
      {live && (
        <div style={{ marginTop: 10 }}>
          <SvProgressBar progress={t.progress} color={t.state === "verifying" ? "yellow" : "magenta"} />
          <div style={{ display: "flex", gap: 14, marginTop: 6, fontFamily: sv.mono, fontSize: 10, color: sv.inkDim, flexWrap: "wrap" }}>
            {t.started_at ? <span>{t.progress.toFixed(1)}%</span> : <span>{t.note ?? "Preparing…"}</span>}
            {t.fps != null && <span>{t.fps.toFixed(0)} fps</span>}
            {t.speed != null && <span>{t.speed.toFixed(2)}× realtime</span>}
            <span>{fmtEta(t.eta_seconds)}</span>
            {t.state === "verifying" && <span>Verifying output…</span>}
          </div>
        </div>
      )}
      {t.state === "done" && (
        <div style={{ marginTop: 6, fontFamily: sv.mono, fontSize: 11, color: sv.green }}>
          {t.note ?? `${fmtGb(t.source_bytes)} → ${fmtGb(t.output_bytes)}`}
        </div>
      )}
      {t.state === "skipped" && t.note && <div style={{ marginTop: 6, fontFamily: sv.mono, fontSize: 11, color: sv.inkDim }}>{t.note}</div>}
      {t.error && <div style={{ marginTop: 6, fontFamily: sv.mono, fontSize: 11, color: sv.red, overflowWrap: "anywhere" }}>{t.error}</div>}
      {t.command && (
        <button
          type="button"
          onClick={() => setShowCmd((v) => !v)}
          style={{ marginTop: 6, background: "none", border: "none", padding: 0, color: sv.inkFaint, fontFamily: sv.mono, fontSize: 10, cursor: "pointer" }}
        >
          {showCmd ? "Hide" : "Show"} ffmpeg command
        </button>
      )}
      {showCmd && t.command && (
        <pre style={{ margin: "6px 0 0", whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontFamily: sv.mono, fontSize: 10, color: sv.inkDim }}>{t.command}</pre>
      )}
    </div>
  );
}
