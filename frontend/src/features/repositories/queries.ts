"use client";

import { createContext, useContext, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import type { RepositoryDetailView, SyncView } from "./types";

export const pendingStatus = (value: string | undefined) => ["queued", "running", "retry_wait"].includes(value ?? "");
export function createAccess() {
  const listeners = new Set<() => void>();
  const state = {
    active: true, denied: false, confirmed: false, retryAt: 0, controller: new AbortController(),
    activate: () => { state.active = true; if (state.controller.signal.aborted && !state.denied) state.controller = new AbortController(); },
    deactivate: () => { state.active = false; state.controller.abort(); },
    confirm: () => { state.confirmed = true; },
    subscribe: (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; },
    snapshot: () => state.denied,
    revoke: () => { state.denied = true; state.controller.abort(); for (const listener of listeners) listener(); },
    async request<T>(path: string, init: RequestInit = {}): Promise<T> {
      const signal = init.signal ? AbortSignal.any([init.signal, state.controller.signal]) : state.controller.signal;
      try {
        const data = await api<T>(path, { ...init, signal });
        signal.throwIfAborted();
        if (!state.active || state.denied) throw new DOMException("Inactive repository", "AbortError");
        return data;
      } catch (error) {
        if (error instanceof ApiError && error.retryAt !== null) { state.retryAt = Math.max(state.retryAt, error.retryAt); for (const listener of listeners) listener(); }
        if (state.active && error instanceof ApiError && (error.status === 401 || ["resource_not_found", "permission_revoked", "reauth_required", "permission_missing", "github_installation_unavailable"].includes(error.code))) state.revoke();
        throw error;
      }
    },
  };
  return state;
}
export type RepositoryScopeValue = { prefix: readonly string[]; path: string; repository: RepositoryDetailView; sync: SyncView; access: ReturnType<typeof createAccess> };
export const RepositoryContext = createContext<RepositoryScopeValue | null>(null);
export function useRepository() {
  const scope = useContext(RepositoryContext);
  if (!scope) throw new Error("Repository scope is required");
  return scope;
}
export function useRepositoryQuery<T>(identity: readonly unknown[], suffix: string, params: Record<string, string> = {}, poll = false) {
  const scope = useRepository();
  const search = new URLSearchParams(params).toString();
  return useQuery({
    queryKey: [...scope.prefix, ...identity],
    queryFn: ({ signal }) => scope.access.request<T>(`${scope.path}${suffix}${search ? `?${search}` : ""}`, { signal }),
    enabled: scope.access.active && !scope.access.denied,
    retry: false, refetchOnWindowFocus: false, refetchOnReconnect: false,
    meta: { poll },
  });
}

// A single finite timer refetches only queries used by the visible repository UI.
export function useRepositoryPolling(prefix: readonly string[], access: ReturnType<typeof createAccess>, enabled: boolean) {
  const client = useQueryClient();
  const [paused, setPaused] = useState(false);
  const recover = useRef(() => {});
  useEffect(() => {
    if (!enabled) return;
    let live = true, fetching = false, failures = 0, polled = false;
    let deadline = Date.now() + 300_000, next = Date.now() + 2000;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const queries = () => client.getQueryCache().findAll({ queryKey: prefix }).filter(query => query.isActive() && query.meta?.poll);
    function schedule() {
      clearTimeout(timer);
      if (!live || fetching || access.denied || !access.active || document.visibilityState !== "visible") return;
      if (Date.now() >= deadline || failures >= 3) { setPaused(true); return; }
      const active = queries();
      if (!polled) failures = active.some(query => query.state.error && (!(query.state.error instanceof ApiError) || query.state.error.status >= 500)) ? 1 : 0;
      const pending = active.some(query => {
        const data = query.state.data as { status?: string; refresh_status?: string } | undefined;
        return pendingStatus(data?.status ?? data?.refresh_status);
      });
      if (!pending) return;
      let floor = Math.max(next, access.retryAt);
      for (const query of active) {
        const data = query.state.data as { retry_at?: string | null } | undefined;
        const error = query.state.error;
        floor = Math.max(floor, data?.retry_at ? Date.parse(data.retry_at) || 0 : 0, error instanceof ApiError ? error.retryAt ?? 0 : 0);
      }
      timer = setTimeout(() => void tick(), Math.max(0, Math.min(deadline, floor) - Date.now()));
    }
    async function tick() {
      if (Date.now() >= deadline || failures >= 3) { setPaused(true); return; }
      if (document.visibilityState !== "visible" || !live || access.denied) return;
      fetching = true; polled = true;
      const results = await Promise.allSettled(queries().map(query => client.refetchQueries({ queryKey: query.queryKey, exact: true }, { throwOnError: true })));
      if (!live) return;
      const transportError = results.some(result => result.status === "rejected" && (!(result.reason instanceof ApiError) || result.reason.status >= 500));
      failures = transportError ? failures + 1 : 0;
      fetching = false; next = Date.now() + 2000; schedule();
    }
    function visibility() {
      clearTimeout(timer);
      if (document.visibilityState !== "visible") {
        for (const query of queries()) void client.cancelQueries({ queryKey: query.queryKey, exact: true });
      } else schedule();
    }
    recover.current = () => { failures = 0; polled = true; deadline = Date.now() + 300_000; next = Date.now() + 2000; setPaused(false); schedule(); };
    const unsubscribe = client.getQueryCache().subscribe(schedule);
    const unsubscribeAccess = access.subscribe(schedule);
    document.addEventListener("visibilitychange", visibility);
    schedule();
    return () => { live = false; clearTimeout(timer); unsubscribe(); unsubscribeAccess(); document.removeEventListener("visibilitychange", visibility); recover.current = () => {}; };
  }, [client, prefix, access, enabled]);
  return { paused, recover: () => recover.current() };
}

export function useRepositoryCommand<T>(operation: string, resource: string) {
  const scope = useRepository(); const client = useQueryClient();
  const logical = useRef<{ body: string; key: string } | null>(null);
  const pendingRef = useRef(false);
  const [pending, setPending] = useState(false), [error, setError] = useState<unknown>(null);
  async function submit(payload: object) {
    if (pendingRef.current || scope.access.denied || !scope.access.active) return;
    const body = JSON.stringify(payload);
    if (logical.current?.body !== body) logical.current = { body, key: crypto.randomUUID() };
    pendingRef.current = true; setPending(true); setError(null);
    try {
      const value = await scope.access.request<T>(`${scope.path}/${operation}`, { method: "POST", body, headers: { "Idempotency-Key": logical.current.key } });
      client.setQueryData([...scope.prefix, resource], value);
      logical.current = null;
    } catch (failure) { if (scope.access.active && !scope.access.denied) setError(failure); }
    finally { pendingRef.current = false; if (scope.access.active && !scope.access.denied) setPending(false); }
  }
  return { submit, pending, error };
}
