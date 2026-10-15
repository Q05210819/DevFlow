"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useSession } from "@/features/session/use-session";
import { errorMessage } from "@/features/admin/common";
import { createAccess, RepositoryContext, useRepositoryPolling } from "./queries";
import type { RepositoryDetailView, SyncView } from "./types";

export function RepositoryScope({ repositoryId, children }: { repositoryId: string; children: React.ReactNode }) {
  const { session } = useSession();
  if (!session) return null;
  return <AuthorizedRepository key={`${session.user.id}:${repositoryId}`} userId={session.user.id} repositoryId={repositoryId}>{children}</AuthorizedRepository>;
}
function AuthorizedRepository({ userId, repositoryId, children }: { userId: string; repositoryId: string; children: React.ReactNode }) {
  const client = useQueryClient(); const pathname = usePathname();
  const prefix = useMemo(() => ["repository-content", userId, repositoryId], [userId, repositoryId]);
  const path = `/api/v1/repositories/${encodeURIComponent(repositoryId)}`;
  const [access] = useState(createAccess);
  const denied = useSyncExternalStore(access.subscribe, access.snapshot, () => false);
  // Set up the scope before query observers subscribe, including Strict Mode's replay.
  useEffect(() => {
    access.activate();
    return () => {
      access.deactivate();
      void client.cancelQueries({ queryKey: prefix }); client.removeQueries({ queryKey: prefix });
    };
  }, [access, client, prefix]);
  const repository = useQuery({ queryKey: [...prefix, "repository"], queryFn: async ({ signal }) => {
    const value = await access.request<RepositoryDetailView>(path, { signal });
    client.setQueryData([...prefix, "sync"], value.sync);
    access.confirm(); return value;
  }, retry: false, staleTime: 0, refetchOnWindowFocus: false, refetchOnReconnect: false });
  const sync = useQuery({ queryKey: [...prefix, "sync"], queryFn: ({ signal }) => access.request<SyncView>(`${path}/sync`, { signal }), enabled: access.confirmed && !denied, retry: false, refetchOnWindowFocus: false, refetchOnReconnect: false, meta: { poll: true } });
  const polling = useRepositoryPolling(prefix, access, access.confirmed && !denied);
  useEffect(() => {
    if (!denied) return;
    void client.cancelQueries({ queryKey: prefix }); client.removeQueries({ queryKey: prefix });
    client.removeQueries({ queryKey: ["repositories", userId] });
  }, [denied, client, prefix, userId]);
  // This render gate precedes descendants even when React Query retains stale data.
  if (denied) return <main className="content"><p role="alert" className="banner error">仓库不可访问，请确认权限或重新登录。</p><Link href="/repositories">返回仓库列表</Link></main>;
  if (!access.confirmed || !repository.data) return <main className="content">{repository.isError ? <><p role="alert" className="banner error">{errorMessage(repository.error)}</p><button onClick={() => void repository.refetch()}>重新加载</button></> : <p role="status">正在确认仓库权限…</p>}</main>;
  const root = `/repositories/${encodeURIComponent(repositoryId)}`;
  return <RepositoryContext.Provider value={{ prefix, path, repository: repository.data, sync: sync.data ?? repository.data.sync, access }}>
    <main className="content repository-detail">
      <span className="eyebrow">工作台 / 仓库事实</span><h1>{repository.data.full_name}</h1>
      <p>平台 ID：{repository.data.id} · GitHub ID：{repository.data.github_repository_id} · 默认分支：{repository.data.default_branch ?? "未知"}</p>
      <nav className="repository-tabs" aria-label="仓库导航">{[["", "概览"], ["/issues", "Issues"], ["/pulls", "PRs"], ["/actions", "Actions"]].map(([suffix, label]) => <Link key={suffix} href={`${root}${suffix}`} aria-current={pathname === `${root}${suffix}` ? "page" : undefined}>{label}</Link>)}</nav>
      {sync.isError && <p role="alert" className="banner error">同步状态刷新失败，保留上次已确认的状态与时间。{errorMessage(sync.error)}</p>}
      {polling.paused && <p role="status" className="banner">状态轮询已暂停（达到 5 分钟或连续 3 次传输失败）。<button onClick={polling.recover}>恢复状态轮询</button></p>}
      {children}
    </main>
  </RepositoryContext.Provider>;
}
