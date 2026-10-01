"use client";

import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import type { JobStatus, JobView } from "@/lib/types";
import { registerEventConnection } from "@/features/session/provider";
import { useSession } from "@/features/session/use-session";

export const statusLabels: Record<JobStatus, string> = { queued: "等待检查", running: "正在检查", retry_wait: "等待重试", succeeded: "检查成功", failed: "检查失败" };
export function isTerminal(status: JobStatus) { return status === "succeeded" || status === "failed"; }
type Progress = { sequence: number; status: JobStatus; attempt: number };
type Connection = "loading" | "live" | "retrying" | "paused" | "closed" | "denied";

export function useJobEvents(jobId: string, initialSequence: number, terminal: boolean) {
  const { session } = useSession();
  const queryClient = useQueryClient();
  const userId = session?.user.id;
  const [job, setJob] = useState<JobView | null>(null);
  const [events, setEvents] = useState<Progress[]>([]);
  const [connection, setConnection] = useState<Connection>("loading");
  const recover = useRef<() => void>(() => {});

  useEffect(() => {
    if (!userId) return;
    let active = true;
    let denied = false;
    let finishing = terminal;
    let sequence = initialSequence;
    let initialized = false;
    let failures = 0;
    let source: EventSource | undefined;
    let unregister: (() => void) | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let pending = false;
    const key = ["job", userId, jobId];
    const path = `/api/v1/jobs/${encodeURIComponent(jobId)}`;
    function close() {
      if (source) { source.onerror = null; source.onopen = null; source.close(); source = undefined; }
      unregister?.(); unregister = undefined;
    }
    function loseAccess() {
      denied = true; close(); clearTimeout(timer);
      void queryClient.cancelQueries({ queryKey: key });
      queryClient.removeQueries({ queryKey: key });
      queryClient.removeQueries({ queryKey: ["repositories", userId] });
      setJob(null); setEvents([]); setConnection("denied");
    }
    function retry() {
      if (!active || denied) return;
      close();
      if (failures >= 3) { setConnection("paused"); return; }
      setConnection("retrying");
      timer = setTimeout(() => void refresh(), 1000 * 2 ** failures++);
    }
    function open() {
      if (!active || denied || finishing || source) return;
      source = new EventSource(`${path}/events?after=${sequence}`);
      const current = source;
      unregister = registerEventConnection(source);
      source.onopen = () => { if (active && source === current) setConnection("live"); };
      source.addEventListener("job.status", event => {
        if (!active || denied || source !== current) return;
        const message = event as MessageEvent<string>;
        const next = Number(message.lastEventId);
        if (!Number.isSafeInteger(next) || next <= sequence) return;
        let data: { status?: JobStatus; attempt?: number };
        try { data = JSON.parse(message.data); } catch { return; }
        if (!data || !data.status || !Object.hasOwn(statusLabels, data.status) || !Number.isSafeInteger(data.attempt) || data.attempt! < 0) return;
        sequence = next;
        failures = 0;
        setEvents(previous => [...previous, { sequence: next, status: data.status!, attempt: data.attempt! }]);
        setConnection("live");
        if (isTerminal(data.status)) { finishing = true; close(); void refresh(); }
      });
      source.addEventListener("access_lost", () => { if (active && source === current) loseAccess(); });
      source.onerror = () => {
        if (!active || source !== current) return;
        close();
        setConnection("retrying");
        void refresh(true);
      };
      setConnection("live");
    }
    async function refresh(reconnecting = false) {
      if (!active || denied || pending) return;
      pending = true;
      try {
        const detail = await queryClient.fetchQuery({ queryKey: key, queryFn: ({ signal }) => api<JobView>(path, { signal }), staleTime: 0, retry: false });
        if (!active || denied) return;
        setJob(detail);
        // Only the first snapshot seeds the cursor. Later snapshots must not skip
        // events that were persisted while this client was disconnected.
        if (!initialized) { sequence = Math.max(sequence, detail.last_sequence); initialized = true; }
        if (isTerminal(detail.status)) { finishing = true; close(); clearTimeout(timer); setConnection("closed"); }
        else if (finishing) retry();
        else if (reconnecting) retry();
        else open();
      } catch (error) {
        if (!active || denied) return;
        if (error instanceof ApiError && [401, 403, 404].includes(error.status)) loseAccess();
        else retry();
      } finally { pending = false; }
    }
    recover.current = () => { if (active && !denied && !pending) { failures = 0; clearTimeout(timer); setConnection("retrying"); void refresh(); } };
    void refresh();
    return () => { active = false; close(); clearTimeout(timer); recover.current = () => {}; void queryClient.cancelQueries({ queryKey: key }); queryClient.removeQueries({ queryKey: key }); };
  }, [jobId, userId, initialSequence, terminal, queryClient]);
  return { job, events, connection, recover: () => recover.current() };
}
