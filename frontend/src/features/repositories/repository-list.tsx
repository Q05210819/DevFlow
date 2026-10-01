"use client";

import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { ApiError, api } from "@/lib/api";
import type { JobView, Page, RepositoryView } from "@/lib/types";
import { useSession } from "@/features/session/use-session";
import { errorMessage, Pagination } from "@/features/admin/common";
import { ConnectRepository } from "./connect-repository";

function CheckButton({ repository }: { repository: RepositoryView }) {
  const router = useRouter();
  const key = useRef<string | null>(null);
  const controller = useRef<AbortController | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => () => controller.current?.abort(), []);
  const denied = error instanceof ApiError && [401, 403, 404, 409, 422].includes(error.status);
  async function submit() {
    if (controller.current && !controller.current.signal.aborted && pending) return;
    key.current ??= crypto.randomUUID();
    const request = new AbortController();
    controller.current = request;
    setPending(true); setError(null);
    try {
      const job = await api<JobView>(`/api/v1/repositories/${encodeURIComponent(repository.id)}/access-checks`, { method: "POST", headers: { "Idempotency-Key": key.current }, signal: request.signal });
      if (!request.signal.aborted) router.push(`/jobs/${encodeURIComponent(job.id)}`);
    } catch (error) { if (!request.signal.aborted) setError(error); }
    finally { if (!request.signal.aborted) setPending(false); }
  }
  return <div>
    <button className="primary-button" disabled={pending || denied} aria-label={error && !denied ? "重试提交" : `检查 ${repository.full_name}`} onClick={() => void submit()}>{pending ? "正在提交…" : error && !denied ? "重试提交" : "检查访问权限"}</button>
    {!!error && <p role="alert" className="error banner">{error instanceof ApiError && error.status === 404 ? "仓库不可访问，请重新加载列表。" : errorMessage(error)}</p>}
  </div>;
}

export function RepositoryList() {
  const { session } = useSession();
  const [showConnect, setShowConnect] = useState(false);
  const [cursors, setCursors] = useState<string[]>([]);
  const cursor = cursors.at(-1);
  const repositories = useQuery({
    queryKey: ["repositories", session?.user.id, cursor],
    queryFn: ({ signal }) => api<Page<RepositoryView>>(`/api/v1/repositories?${new URLSearchParams({ limit: "20", ...(cursor ? { cursor } : {}) })}`, { signal }),
    enabled: !!session,
  });
  return <main className="content">
    <span className="eyebrow">工作台 / 仓库</span><h1>仓库</h1><p>查看已授权的仓库，提交只读访问检查。检查不会修改仓库内容。</p>
    {session?.user.role === "admin" && <button onClick={() => setShowConnect(!showConnect)} aria-expanded={showConnect}>{showConnect ? "收起接入列表" : "接入仓库"}</button>}
    {showConnect && session?.user.role === "admin" && <ConnectRepository />}
    {repositories.isPending ? <p role="status">正在加载仓库…</p> : repositories.isError ? <><p className="banner error" role="alert">{errorMessage(repositories.error)}</p><button onClick={() => void repositories.refetch()}>重新加载</button></> : <>
      {!repositories.data.items.length && <div className="panel empty-state"><h2>{session?.user.role === "admin" ? "尚未接入仓库" : "暂无可访问的仓库"}</h2><p>{session?.user.role === "admin" ? "选择接入仓库以查看可用的 GitHub 仓库。" : "请确认帐号权限，或联系管理员接入仓库。"}</p></div>}
      <ul className="repository-grid" aria-label="已授权仓库">{repositories.data.items.map(repository => <li className="panel repository-card" key={repository.id}><h2>{repository.full_name}</h2><p>{repository.private ? "私有仓库" : "公开仓库"} · 默认分支：{repository.default_branch ?? "未设置"}</p><CheckButton repository={repository} /></li>)}</ul>
      <Pagination page={cursors.length + 1} busy={repositories.isFetching} hasNext={!!repositories.data.next_cursor} previous={() => setCursors(cursors.slice(0, -1))} next={() => { if (repositories.data.next_cursor) setCursors([...cursors, repositories.data.next_cursor]); }} />
    </>}
  </main>;
}
