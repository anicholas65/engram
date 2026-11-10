/**
 * Fork (encode stage): app-wide toasts when an encode starts, finishes or fails,
 * so you see it on any page, not only on ENCODE. Renders nothing.
 */
import { useEffect, useRef } from "react";
import { toast } from "sonner";
import type { EncodeTask } from "../api/encodeClient";
import { useEncodeTasks } from "../hooks/useEncodeTasks";
import { encoderLabel, mbps, targetLabel } from "./encodeFormat";

export default function EncodeWatcher() {
  const { tasks } = useEncodeTasks();
  // Per task: the started_at and state we last saw. Null until the first load,
  // so tasks already running when the page opens don't toast.
  const seen = useRef<Map<number, { started: string | null; state: EncodeTask["state"] }> | null>(null);

  useEffect(() => {
    const prev = seen.current;
    const next = new Map<number, { started: string | null; state: EncodeTask["state"] }>();
    for (const t of tasks) {
      next.set(t.id, { started: t.started_at, state: t.state });
      if (!prev) continue;
      const before = prev.get(t.id);
      if (t.started_at && t.started_at !== before?.started && t.state === "running") {
        const src = t.source_video_kbps ? ` · source ${mbps(t.source_video_kbps)}` : "";
        toast.info(`Encoding started: ${t.filename}`, {
          description: `${encoderLabel(t.encoder)} · ${targetLabel(t)}${src}`,
        });
      } else if (before && before.state !== t.state) {
        if (t.state === "done") {
          toast.success(`Encode finished: ${t.filename}`, {
            description: t.output_video_kbps ? `Video ${mbps(t.output_video_kbps)} · ${t.note ?? ""}` : (t.note ?? undefined),
          });
        } else if (t.state === "failed") {
          toast.error(`Encode failed: ${t.filename}`, { description: t.error?.slice(0, 200) });
        }
      }
    }
    if (tasks.length || prev) seen.current = next;
  }, [tasks]);

  return null;
}
