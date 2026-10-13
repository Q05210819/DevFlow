"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/lib/api";
import type { CandidateRepository, Page, RepositoryView } from "@/lib/types";
import { useSession } from "@/features/session/use-session";
import { errorMessage, Pagination } from "@/features/admin/common";

export function ConnectRepository() {
  const { session } = useSession();
  const queryClient = useQueryClient();
  const [cursors, setCursors] = useState<string[]>([]);
  const cursor = cursors.at(-1);
  const candidates = useQuery({
    queryKey: ["candidates", session?.user.id, cursor],
    enabled: session?.user.role === "admin",
    queryFn: ({ signal }) => api<Page<CandidateRepository>>(`/api/v1/github/repositories?${new URLSearchParams({ limit: "20", ...(cursor ? { cursor } : {}) })}`, { signal }),
  });
  const connect = useMutation({
    mutationFn: (repository: CandidateRepository) => api<RepositoryView>("/api/v1/repositories", { method: "POST", body: JSON.stringify({ installation_id: repository.installation_id, github_repository_id: repository.github_repository_id }) }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["repositories"] }),
  });
  return <section className="panel repository-connect" aria-label="候选仓库">
    <h2>接入 GitHub 仓库</h2><p>仅显示当前帐号与 GitHub App 安装共同授权的仓库。</p>
    {connect.isSuccess && <p className="banner" role="status">仓库已接入。</p>}
    {connect.isError && <p className="banner error" role="alert">{errorMessage(connect.error)}</p>}
    {candidates.isPending ? <p role="status">正在加载候选仓库…</p> : candidates.isError ? <><p className="banner error" role="alert">{errorMessage(candidates.error)}</p><button onClick={() => void candidates.refetch()}>重新加载候选</button></> : <>
      {!candidates.data.items.length && <p>本页没有可接入的仓库。{candidates.data.next_cursor ? "请继续查看下一页。" : "请确认 GitHub App 安装范围和帐号权限。"}</p>}
      <ul className="repository-grid">{candidates.data.items.map(repository => <li className="repository-card" key={`${repository.installation_id}:${repository.github_repository_id}`}><h3>{repository.full_name}</h3><p>{repository.private ? "私有仓库" : "公开仓库"}</p><button disabled={connect.isPending} aria-label={`接入 ${repository.full_name}`} onClick={() => connect.mutate(repository)}>接入</button></li>)}</ul>
    </>}
      <Pagination page={cursors.length + 1} busy={candidates.isFetching || connect.isPending} hasNext={candidates.isSuccess && !!candidates.data.next_cursor} previous={() => setCursors(cursors.slice(0, -1))} next={() => { if (candidates.isSuccess && candidates.data.next_cursor) setCursors([...cursors, candidates.data.next_cursor]); }} />
  </section>;
}
