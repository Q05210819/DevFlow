"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/lib/api";
import type { AuditView, Page } from "@/lib/types";
import { useSession } from "@/features/session/use-session";
import { errorMessage, Pagination } from "./common";

function reasonLabel(reason: string | null) {
  switch (reason) {
    case null: return "—";
    case "last_admin": return "需要保留有效管理员";
    case "forbidden": return "权限不足";
    case "unauthenticated": return "会话无效";
    default: return "操作未完成";
  }
}

export function Audit() {
  const { session } = useSession();
  const [cursors, setCursors] = useState<string[]>([]);
  const cursor = cursors.at(-1);
  const audit = useQuery({
    queryKey: ["audit", session?.user.id, cursor],
    enabled: session?.user.role === "admin",
    queryFn: ({ signal }) => api<Page<AuditView>>(`/api/v1/admin/audit-logs?${new URLSearchParams({ limit: "20", ...(cursor ? { cursor } : {}) })}`, { signal }),
  });
  return <main className="content">
    <span className="eyebrow">团队管理</span><h1>审计日志</h1><p>查看团队操作记录。日志仅供管理员读取。</p>
    {audit.isPending ? <p role="status">正在加载日志…</p> : audit.isError ? <><p className="banner error" role="alert">{errorMessage(audit.error)}</p><button disabled={audit.isFetching} onClick={() => void audit.refetch()}>重新加载</button></> : <>
      <div className="table-wrap" tabIndex={0} role="region" aria-label="审计列表">
        <table><caption className="sr-only">团队操作审计记录</caption><thead><tr><th scope="col">时间</th><th scope="col">操作者</th><th scope="col">操作</th><th scope="col">资源</th><th scope="col">结果</th><th scope="col">原因</th></tr></thead>
          <tbody>{audit.data.items.map((entry) => <tr key={entry.id}>
            <td><time dateTime={entry.created_at}>{new Date(entry.created_at).toLocaleString("zh-CN", { hour12: false })}</time></td>
            <td>{entry.actor_id ?? "系统"}</td><td>{entry.action}</td><td>{entry.resource_type}{entry.resource_id ? ` / ${entry.resource_id}` : ""}</td>
            <td>{entry.outcome === "success" ? "成功" : entry.outcome === "failure" ? "失败" : "未完成"}</td><td>{reasonLabel(entry.reason_code)}</td>
          </tr>)}</tbody>
        </table>
        {!audit.data.items.length && <p className="empty-state">暂无审计记录。</p>}
      </div>
    </>}
      <Pagination page={cursors.length + 1} hasNext={audit.isSuccess && !!audit.data.next_cursor} busy={audit.isFetching} previous={() => setCursors(cursors.slice(0, -1))} next={() => { if (audit.isSuccess && audit.data.next_cursor) setCursors([...cursors, audit.data.next_cursor]); }} />
  </main>;
}
