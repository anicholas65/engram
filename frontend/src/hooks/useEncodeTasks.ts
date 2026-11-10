/**
 * Fork (encode stage): live encode tasks shared app-wide.
 *
 * One WebSocket feeds every subscriber (Encode page, nav badge, start/finish
 * toasts), so mounting several consumers never opens several sockets. Encode
 * events are fork-only and deliberately stay out of the dashboard's typed
 * WebSocket message union to keep upstream merges cheap.
 */
import { useCallback, useSyncExternalStore } from "react";
import { encodeApi, type EncodeTask } from "../api/encodeClient";

type Listener = () => void;

let tasks: EncodeTask[] = [];
const listeners = new Set<Listener>();
let ws: WebSocket | null = null;
let retry: number | undefined;

function emit(next: EncodeTask[]) {
  tasks = next;
  listeners.forEach((l) => l());
}

export function refreshEncodeTasks(): void {
  encodeApi
    .tasks()
    .then(emit)
    .catch(() => undefined);
}

function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws`);
  ws.onopen = refreshEncodeTasks;
  ws.onmessage = (ev) => {
    let msg: { type?: string; task?: EncodeTask };
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (msg.type === "encode_task" && msg.task) {
      const t = msg.task;
      const i = tasks.findIndex((x) => x.id === t.id);
      if (i === -1) emit([t, ...tasks]);
      else {
        const next = tasks.slice();
        next[i] = t;
        emit(next);
      }
    } else if (msg.type === "encode_cleared") {
      refreshEncodeTasks();
    }
  };
  ws.onclose = () => {
    ws = null;
    if (listeners.size) retry = window.setTimeout(connect, 3000);
  };
}

function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  if (listeners.size === 1) {
    refreshEncodeTasks();
    if (!ws) connect();
  }
  return () => {
    listeners.delete(listener);
    if (!listeners.size) {
      window.clearTimeout(retry);
      ws?.close();
      ws = null;
    }
  };
}

const getSnapshot = () => tasks;

export function useEncodeTasks(): { tasks: EncodeTask[]; refresh: () => void } {
  const current = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  const refresh = useCallback(() => refreshEncodeTasks(), []);
  return { tasks: current, refresh };
}

export const ENCODE_ACTIVE_STATES = ["queued", "running", "verifying"] as const;

/** Count of encodes running right now (not merely queued). */
export function useEncodingCount(): number {
  const { tasks: all } = useEncodeTasks();
  return all.filter((t) => t.state === "running" || t.state === "verifying").length;
}
