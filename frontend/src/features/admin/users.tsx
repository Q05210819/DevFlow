"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/lib/api";
import type { Page, Role, UserView } from "@/lib/types";
import { useSession } from "@/features/session/use-session";
import { errorMessage, Pagination } from "./common";

export function Users() {
  const { session } = useSession();
  const queryClient = useQueryClient();
  const [cursors, setCursors] = useState<string[]>([]);
  const [githubId, setGithubId] = useState("");
  const [role, setRole] = useState<Role>("member");
  const [notice, setNotice] = useState("");
  const [validation, setValidation] = useState("");
  const cursor = cursors.at(-1);
  const users = useQuery({
    queryKey: ["users", session?.user.id, cursor],
    enabled: session?.user.role === "admin",
    queryFn: ({ signal }) => api<Page<UserView>>(`/api/v1/admin/users?${new URLSearchParams({ limit: "20", ...(cursor ? { cursor } : {}) })}`, { signal }),
  });
  const save = useMutation({
    mutationFn: ({ id, body }: { id?: string; body: { github_id?: number; role?: Role; enabled?: boolean } }) =>
      api<UserView>(id ? `/api/v1/admin/users/${encodeURIComponent(id)}` : "/api/v1/admin/users", { method: id ? "PATCH" : "POST", body: JSON.stringify(body) }),
    onMutate: () => { setNotice(""); setValidation(""); },
    onSuccess: async (_, variables) => {
      if (!variables.id) { setGithubId(""); setCursors([]); }
      setNotice("已保存成员设置。");
      await queryClient.invalidateQueries({ queryKey: ["users"] });
      if (variables.id === session?.user.id) await queryClient.invalidateQueries({ queryKey: ["session"] });
    },
  });

  return <main className="content">
    <span className="eyebrow">团队管理</span><h1>成员管理</h1>
    <p>管理团队成员及角色。管理员角色不会授予额外的仓库访问权限。</p>
    <form className="panel member-form" onSubmit={(event) => {
      event.preventDefault();
      const id = Number(githubId);
      if (!/^\d+$/.test(githubId) || !Number.isSafeInteger(id) || id <= 0) { setValidation("请输入有效的 GitHub 数字 ID。"); return; }
      save.mutate({ body: { github_id: id, role } });
    }}>
      <label>GitHub 数字 ID<input name="github_id" inputMode="numeric" required value={githubId} onChange={(event) => setGithubId(event.target.value)} /></label>
      <label>初始角色<select value={role} onChange={(event) => setRole(event.target.value as Role)}><option value="member">普通成员</option><option value="admin">管理员</option></select></label>
      <button className="primary-button" disabled={save.isPending} type="submit">添加成员</button>
    </form>
    {(validation || save.error) && <p className="banner error" role="alert">{validation || errorMessage(save.error)}</p>}
    {notice && <p className="banner" role="status">{notice}</p>}
    {users.isPending ? <p role="status">正在加载成员…</p> : users.isError ? <p className="banner error" role="alert">{errorMessage(users.error)}</p> : <>
      <div className="table-wrap" tabIndex={0} role="region" aria-label="成员列表">
        <table><caption className="sr-only">团队成员及权限</caption><thead><tr><th scope="col">成员</th><th scope="col">GitHub ID</th><th scope="col">角色</th><th scope="col">状态</th><th scope="col">操作</th></tr></thead>
          <tbody>{users.data.items.map((user) => {
            const name = user.login ?? String(user.github_id);
            return <tr key={user.id}><th scope="row">{name}</th><td>{user.github_id}</td><td>{user.role === "admin" ? "管理员" : "普通成员"}</td><td>{user.enabled ? "已启用" : "已停用"}</td><td className="row-actions">
              <button type="button" disabled={save.isPending} aria-label={`${user.enabled ? "停用" : "启用"} ${name}`} onClick={() => save.mutate({ id: user.id, body: { enabled: !user.enabled } })}>{user.enabled ? "停用" : "启用"}</button>
              <button type="button" disabled={save.isPending} aria-label={`${user.role === "admin" ? "设为普通成员" : "设为管理员"} ${name}`} onClick={() => save.mutate({ id: user.id, body: { role: user.role === "admin" ? "member" : "admin" } })}>{user.role === "admin" ? "设为普通成员" : "设为管理员"}</button>
            </td></tr>;
          })}</tbody>
        </table>
        {!users.data.items.length && <p className="empty-state">暂无成员。</p>}
      </div>
      <Pagination page={cursors.length + 1} hasNext={!!users.data.next_cursor} busy={users.isFetching || save.isPending} previous={() => setCursors(cursors.slice(0, -1))} next={() => { if (users.data.next_cursor) setCursors([...cursors, users.data.next_cursor]); }} />
    </>}
  </main>;
}
