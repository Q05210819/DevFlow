"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api";
import { errorMessage, Pagination } from "@/features/admin/common";
import { shown } from "./issues";
import { pendingStatus, useRepository, useRepositoryQuery } from "./queries";
import type { CodeCitation, FileView, ReadReason, ReadScope, SearchView, TreePage, WorkspacePage, WorkspaceSelection, WorkspaceView } from "./types";

export function selectionLabel(selection: WorkspaceSelection) {
  if (selection.kind === "ci") return `GitHub Actions Run ${selection.run_id} / Attempt ${selection.attempt}`;
  if (selection.kind === "pull") return `PR #${selection.number} / ${selection.side}`;
  return `${selection.kind === "branch" ? "分支" : "标签"} ${selection.name}`;
}
function sameSelection(a: WorkspaceSelection, b: WorkspaceSelection) {
  if (a.kind !== b.kind) return false;
  if ("name" in a && "name" in b) return a.name === b.name;
  if (a.kind === "pull" && b.kind === "pull") return a.number === b.number && a.side === b.side;
  return a.kind === "ci" && b.kind === "ci" && a.run_id === b.run_id && a.attempt === b.attempt;
}
function sameVersion(a: WorkspaceView, b: WorkspaceView) {
  return a.id === b.id && a.repository_id === b.repository_id && a.source_repository_id === b.source_repository_id && a.commit_sha === b.commit_sha && a.tree_oid === b.tree_oid
    && sameSelection(a.source.selection, b.source.selection) && a.source.ref === b.source.ref && a.source.source_repository_id === b.source.source_repository_id && a.source.transfer_repository_id === b.source.transfer_repository_id && a.source.base_sha === b.source.base_sha && a.source.head_sha === b.source.head_sha;
}
const reasons: Record<ReadReason, string> = { unsupported_path: "路径不受支持", too_large: "文件超过预览大小", binary: "二进制文件", non_utf8: "非 UTF-8 文本", lfs_pointer: "LFS 指针（不下载对象）", symlink: "符号链接（不跟随）", gitlink: "子模块引用（不跟随）", tree_limit: "目录扫描上限，范围未知", response_limit: "响应文本预算已到达", line_limit: "行数上限", file_limit: "文本文件扫描上限", byte_limit: "搜索字节预算已到达", match_limit: "匹配行数上限" };
const statuses: Record<WorkspaceView["status"], string> = { queued: "等待准备", running: "正在准备", retry_wait: "等待重试", ready: "已准备", failed: "准备失败", unavailable: "版本不可用" };
function CodeError({ error, retry, restart }: { error: unknown; retry: () => void; restart?: () => void }) {
  const code = error instanceof ApiError ? error.code : "";
  const labels: Record<string, string> = { workspace_not_found: "此固定版本不存在或已失效。", fork_source_unavailable: "Fork 来源不可用，完整代码已隐藏；仍可查看基础仓库 PR 事实。", workspace_busy: "代码读取繁忙，请稍后重试。", workspace_response_mismatch: "返回的代码版本与当前选择不一致，代码已隐藏。", version_unavailable: "无法证明该来源的代码版本。", version_changed: "来源版本已变化，请重新选择并准备。", invalid_cursor: "分页已失效，请重新确认固定版本。", storage_exhausted: "代码存储预算已用尽，请联系管理员。" };
  return <><p role="alert" className="banner error">{labels[code] ?? errorMessage(error)}</p><button type="button" onClick={code === "invalid_cursor" && restart ? restart : retry}>{code === "invalid_cursor" && restart ? "返回第一页" : "重新加载"}</button></>;
}
function ReadEvidence({ complete, truncated, reason, skipped }: { complete: boolean; truncated: boolean; reason: ReadReason | null; skipped: Record<string, number> }) {
  return <p className="banner">{complete ? "此读取范围完整" : "此读取范围不完整或未知"}{truncated ? " · 已截断" : ""}{reason ? ` · ${reasons[reason]}` : ""}{Object.entries(skipped).map(([kind, count]) => <span key={kind}> · 跳过 {kind}：{count}</span>)}</p>;
}
function sameScope(data: ReadScope, version: WorkspaceView, path?: string) {
  return data.repository_id === version.repository_id && data.workspace_id === version.id && data.source_repository_id === version.source_repository_id && data.commit_sha === version.commit_sha && (path === undefined || data.path === path);
}
type AttemptAccess = { request: <T>(path: string, init?: RequestInit) => Promise<T>; invalidate: () => never; live: () => boolean };

export function Workspace({ workspaceId }: { workspaceId?: string }) {
  const { repository } = useRepository();
  const [draft, setDraft] = useState({ kind: "branch", name: repository.default_branch ?? "", number: "", side: "head", run: "", attempt: "" });
  const [selection, setSelection] = useState({ generation: 0, id: workspaceId, controller: new AbortController() });
  function change(next: Partial<typeof draft>) {
    selection.controller.abort();
    setDraft({ ...draft, ...next });
    setSelection({ generation: selection.generation + 1, id: undefined, controller: new AbortController() });
  }
  const positive = (value: string) => /^\d+$/.test(value) && Number.isSafeInteger(Number(value)) && Number(value) > 0;
  let typed: WorkspaceSelection | null = null;
  if ((draft.kind === "branch" || draft.kind === "tag") && draft.name.length > 0 && draft.name.length <= 1024) typed = { kind: draft.kind, name: draft.name };
  if (draft.kind === "pull" && positive(draft.number)) typed = { kind: "pull", number: Number(draft.number), side: draft.side as "base" | "head" };
  if (draft.kind === "ci" && positive(draft.run) && positive(draft.attempt)) typed = { kind: "ci", run_id: Number(draft.run), attempt: Number(draft.attempt) };
  return <section aria-label="代码浏览"><h2>固定版本代码</h2><p>先选择并准备版本。目录、原始文本和字面搜索始终绑定同一完整 SHA；不会自动回退到默认分支。</p>
    <div className="member-form"><label>版本来源<select value={draft.kind} onChange={event => change({ kind: event.target.value })}><option value="branch">分支</option><option value="tag">标签</option><option value="pull">PR</option><option value="ci">GitHub Actions</option></select></label>
      {(draft.kind === "branch" || draft.kind === "tag") && <label>分支或标签名称<input value={draft.name} maxLength={1024} onChange={event => change({ name: event.target.value })} /></label>}
      {draft.kind === "pull" && <><label>PR 编号<input inputMode="numeric" value={draft.number} onChange={event => change({ number: event.target.value })} /></label><label>PR 侧<select value={draft.side} onChange={event => change({ side: event.target.value })}><option value="base">base</option><option value="head">head</option></select></label></>}
      {draft.kind === "ci" && <><label>Run ID<input inputMode="numeric" value={draft.run} onChange={event => change({ run: event.target.value })} /></label><label>Attempt<input inputMode="numeric" value={draft.attempt} onChange={event => change({ attempt: event.target.value })} /></label></>}
    </div>
    <WorkspaceAttempt key={selection.generation} selection={typed} generation={selection.generation} initialId={selection.id} selectionSignal={selection.controller.signal} choose={id => { selection.controller.abort(); setSelection({ generation: selection.generation + 1, id, controller: new AbortController() }); }} />
  </section>;
}

function WorkspaceSnapshots({ choose }: { choose: (id: string) => void }) {
  const [cursors, setCursors] = useState<string[]>([]); const cursor = cursors.at(-1);
  const query = useRepositoryQuery<WorkspacePage>(["workspace-list", cursor ?? ""], "/workspaces", cursor ? { cursor } : {});
  return <section className="panel fact-collection" aria-label="已准备版本"><h3>已准备版本</h3>{query.isFetching || query.isPending ? <p role="status">正在确认版本列表…</p> : query.isError ? <CodeError error={query.error} retry={() => void query.refetch()} restart={() => setCursors([])} /> : <>
    <p>本页观察到不可用来源：{query.data.unavailable_count}。列表仅包含当前授权版本，不代表远端总数。</p>
    {!query.data.items.length && <p>此页没有可显示的版本；若有后续页可继续。</p>}
    <ul className="fact-list">{query.data.items.map(version => <li key={version.id}><button type="button" onClick={() => choose(version.id)}>{selectionLabel(version.source.selection)} · {statuses[version.status]}</button><p>{version.commit_sha} · {version.created_at}</p></li>)}</ul>
  </>}<Pagination page={cursors.length + 1} busy={query.isFetching} hasNext={query.isSuccess && query.data.has_more && !!query.data.next_cursor} previous={() => setCursors(cursors.slice(0, -1))} next={() => { if (query.data?.next_cursor) setCursors([...cursors, query.data.next_cursor]); }} /></section>;
}

function WorkspaceAttempt({ selection, generation, initialId, selectionSignal, choose }: { selection: WorkspaceSelection | null; generation: number; initialId?: string; selectionSignal: AbortSignal; choose: (id: string) => void }) {
  const scope = useRepository(); const client = useQueryClient();
  const prefix = useMemo(() => [...scope.prefix, "workspace", generation, initialId ?? "request"], [scope.prefix, generation, initialId]);
  const lifetime = useRef({ active: true, controller: new AbortController() });
  const [id, setId] = useState<string | null>(initialId ?? null);
  const [pending, setPending] = useState(false), [error, setError] = useState<unknown>(null), [blocked, setBlocked] = useState<unknown>(null);
  const [confirming, setConfirming] = useState(false);
  const logical = useRef<{ body: string; key: string } | null>(null), expected = useRef<WorkspaceView | null>(null);
  const inFlight = useRef(false);
  useEffect(() => {
    const current = lifetime.current;
    current.active = true; if (current.controller.signal.aborted) current.controller = new AbortController();
    return () => { current.active = false; current.controller.abort(); void client.cancelQueries({ queryKey: prefix }); client.removeQueries({ queryKey: prefix }); };
  }, [client, lifetime, prefix]);
  const access: AttemptAccess = {
    live: () => lifetime.current.active && !selectionSignal.aborted && !lifetime.current.controller.signal.aborted && scope.access.active && !scope.access.denied,
    invalidate() { const error = new ApiError(409, "workspace_response_mismatch"); setBlocked(error); throw error; },
    async request<T>(path: string, init: RequestInit = {}) {
      const signal = AbortSignal.any([selectionSignal, lifetime.current.controller.signal, ...(init.signal ? [init.signal] : [])]);
      try {
        const value = await scope.access.request<T>(path, { ...init, signal }); signal.throwIfAborted();
        if (!lifetime.current.active) throw new DOMException("Inactive workspace", "AbortError");
        return value;
      } catch (failure) {
        signal.throwIfAborted();
        if (lifetime.current.active && failure instanceof ApiError && ["workspace_not_found", "fork_source_unavailable"].includes(failure.code)) setBlocked(failure);
        throw failure;
      }
    },
  };
  const metadata = useQuery({ queryKey: [...prefix, "metadata", id], enabled: !!id && !blocked && scope.access.active && !scope.access.denied,
    queryFn: async ({ signal }) => {
      const value = await access.request<WorkspaceView>(`${scope.path}/workspaces/${encodeURIComponent(id!)}`, { signal });
      const previous = expected.current;
      if (value.id !== id || value.repository_id !== scope.repository.id || (previous && !sameVersion(value, previous))) access.invalidate();
      expected.current = value; return value;
    }, retry: false, refetchOnWindowFocus: false, refetchOnReconnect: false, meta: { poll: true } });
  async function prepare() {
    if (!selection || inFlight.current || !access.live()) return;
    const body = JSON.stringify(selection);
    if (logical.current?.body !== body) logical.current = { body, key: crypto.randomUUID() };
    inFlight.current = true; setPending(true); setError(null); setBlocked(null); setId(null); expected.current = null;
    try {
      const value = await access.request<WorkspaceView>(`${scope.path}/workspaces`, { method: "POST", body, headers: { "Idempotency-Key": logical.current.key } });
      if (value.repository_id !== scope.repository.id || !sameSelection(value.source.selection, selection)) access.invalidate();
      expected.current = value; logical.current = null; setId(value.id);
    } catch (failure) { if (access.live()) setError(failure); }
    finally { inFlight.current = false; if (access.live()) setPending(false); }
  }
  function reconfirm() { if (!access.live()) return; if (!id) { void prepare(); return; } setBlocked(null); setConfirming(true); void metadata.refetch().finally(() => { if (access.live()) setConfirming(false); }); }
  return <>
    <button type="button" disabled={!selection || pending} onClick={() => void prepare()}>{pending ? "正在提交…" : error ? "重试准备" : "准备版本"}</button>
    {!!error && !blocked && <p role="alert" className="banner error">{error instanceof ApiError && error.code === "version_unavailable" ? "无法证明该来源的代码版本。" : errorMessage(error)}</p>}
    {blocked ? <CodeError error={blocked} retry={reconfirm} /> : id && !pending ? confirming || metadata.isPending ? <p role="status">正在确认固定版本…</p> : metadata.isError ? <CodeError error={metadata.error} retry={reconfirm} /> : metadata.data && <>
      <p role="status">{statuses[metadata.data.status]}{metadata.data.error ? " · 来源或准备暂不可用" : ""}</p>
      {pendingStatus(metadata.data.status) && <p>正在后台准备；页面可见时有限轮询。暂停后可手动重新确认。</p>}
      <button type="button" onClick={reconfirm}>重新确认版本</button>
      {metadata.data.status === "ready" && <section className="panel fact-collection" aria-label="固定版本"><h3>{selectionLabel(metadata.data.source.selection)}</h3><dl className="fact-fields"><dt>固定 SHA</dt><dd>{metadata.data.commit_sha}</dd><dt>Workspace ID / 来源仓库 ID</dt><dd>{metadata.data.id} / {metadata.data.source_repository_id}</dd><dt>PR Base / Head SHA</dt><dd>{shown(metadata.data.source.base_sha)} / {shown(metadata.data.source.head_sha)}</dd></dl>
        {metadata.data.source.selection.kind === "ci" && <p>此 SHA 是该 Run/Attempt 关联的 Head，不推定为工作流实际 checkout 提交。</p>}
        <WorkspaceContent key={`${metadata.data.id}:${metadata.data.source_repository_id}:${metadata.data.commit_sha}`} version={metadata.data} prefix={prefix} access={access} reconfirm={reconfirm} />
      </section>}
    </> : null}
    {!blocked && <WorkspaceSnapshots choose={choose} />}
  </>;
}

type Location = { kind: "tree" | "file" | "search" | "idle"; path: string; cursors?: string[]; query?: string; start?: number; count?: number };
type ContentProps = { version: WorkspaceView; prefix: readonly unknown[]; access: AttemptAccess; reconfirm: () => void };
function WorkspaceContent({ version, prefix, access, reconfirm }: ContentProps) {
  const [directory, setDirectory] = useState(""), [query, setQuery] = useState("");
  const [current, setCurrent] = useState({ location: { kind: "tree", path: "" } as Location, generation: 0, controller: new AbortController() });
  function navigate(location: Location) { current.controller.abort(); setCurrent({ location, generation: current.generation + 1, controller: new AbortController() }); }
  const bytes = new TextEncoder().encode(query).length;
  const props = { version, prefix: [...prefix, "content", version.id, version.source_repository_id, version.commit_sha, current.generation], access, reconfirm, location: current.location, signal: current.controller.signal, navigate };
  return <>
    <nav className="repository-controls" aria-label="代码路径"><button type="button" onClick={() => { setDirectory(""); navigate({ kind: "tree", path: "" }); }}>根目录</button>{directory && <button type="button" onClick={() => navigate({ kind: "tree", path: directory })}>目录 {directory}</button>}</nav>
    <form className="member-form" onSubmit={event => { event.preventDefault(); if (bytes >= 1 && bytes <= 256 && !query.includes("\n")) navigate({ kind: "search", path: directory, query }); }}><label>字面文本搜索<input value={query} onChange={event => { setQuery(event.target.value); navigate({ kind: "idle", path: directory }); }} /></label><button disabled={bytes < 1 || bytes > 256 || query.includes("\n")} type="submit">搜索代码</button></form>
    <p>搜索区分大小写，查询最多 256 个 UTF-8 字节且不包含 LF。范围：{directory || "/"}。</p>
    {current.location.kind === "tree" && <TreePanel key={current.generation} {...props} open={(path, isDirectory) => { if (isDirectory) setDirectory(path); navigate({ kind: isDirectory ? "tree" : "file", path }); }} />}
    {current.location.kind === "file" && <FilePanel key={current.generation} {...props} />}
    {current.location.kind === "search" && <SearchPanel key={current.generation} {...props} />}
  </>;
}
type ReadProps = ContentProps & { location: Location; signal: AbortSignal; navigate: (location: Location) => void };
function useCodeRead<T extends ReadScope>(props: ReadProps, suffix: string, params: Record<string, string>, verify?: (value: T) => boolean) {
  const scope = useRepository(); const client = useQueryClient();
  const key = [...props.prefix, suffix, params];
  const query = useQuery({ queryKey: key, queryFn: async ({ signal }) => {
    const value = await props.access.request<T>(`${scope.path}/workspaces/${encodeURIComponent(props.version.id)}/${suffix}?${new URLSearchParams(params)}`, { signal: AbortSignal.any([signal, props.signal]) });
    if (!sameScope(value, props.version, props.location.path) || (verify && !verify(value))) props.access.invalidate();
    return value;
  }, retry: false, refetchOnWindowFocus: false, refetchOnReconnect: false });
  // Every mounted read owns its key; a departed view cannot refill a reusable cache entry.
  const identity = JSON.stringify(key);
  useEffect(() => () => { void client.cancelQueries({ queryKey: JSON.parse(identity), exact: true }); client.removeQueries({ queryKey: JSON.parse(identity), exact: true }); }, [client, identity]);
  return query;
}
function TreePanel(props: ReadProps & { open: (path: string, directory: boolean) => void }) {
  const cursors = props.location.cursors ?? [], cursor = cursors.at(-1);
  const query = useCodeRead<TreePage>(props, "tree", { path: props.location.path, ...(cursor ? { cursor } : {}) });
  return <section aria-label="目录"><h3>目录 {props.location.path || "/"}</h3>{query.isPending || query.isFetching ? <p role="status">正在读取目录…</p> : query.isError ? <CodeError error={query.error} retry={() => props.navigate(props.location)} restart={props.reconfirm} /> : <>
    <ReadEvidence {...query.data} /><p>已扫描原始目录条目：{query.data.scanned}。分页不等于扫描截断。</p>
    {!query.data.items.length && <p>此页没有目录条目</p>}
    <ul className="fact-list">{query.data.items.map((item, index) => <li key={item.path ?? `unsupported:${index}`}>{item.path !== null && item.kind !== "unsupported" ? <button type="button" onClick={() => props.open(item.path!, item.kind === "directory")}>{item.display_name}</button> : <span>{item.display_name}</span>}<span> · {item.kind}{item.reason ? ` · ${reasons[item.reason]}` : ""}</span></li>)}</ul>
  </>}<Pagination page={cursors.length + 1} busy={query.isFetching} hasNext={query.isSuccess && query.data.has_more && !!query.data.next_cursor} previous={() => props.navigate({ ...props.location, cursors: cursors.slice(0, -1) })} next={() => { if (query.data?.next_cursor) props.navigate({ ...props.location, cursors: [...cursors, query.data.next_cursor] }); }} /></section>;
}
function FilePanel(props: ReadProps) {
  const [start, setStart] = useState(props.location.start?.toString() ?? ""), [count, setCount] = useState(props.location.count?.toString() ?? ""), [editing, setEditing] = useState(false);
  const query = useCodeRead<FileView>(props, "file", { path: props.location.path, ...(props.location.start ? { start_line: String(props.location.start) } : {}), ...(props.location.count ? { line_count: String(props.location.count) } : {}) }, data => (props.location.start === undefined || data.requested_start_line === props.location.start) && (props.location.count === undefined || data.requested_line_count === props.location.count));
  return <section aria-label="文件"><h3>{props.location.path}</h3><button type="button" onClick={() => props.navigate(props.location)}>刷新代码</button>
    <form className="member-form" onSubmit={event => { event.preventDefault(); props.navigate({ ...props.location, start: start ? Number(start) : undefined, count: count ? Number(count) : undefined }); }}><label>起始行（留空使用 API 默认）<input type="number" min="1" value={start} onChange={event => { setStart(event.target.value); setEditing(true); }} /></label><label>行数（最多 500）<input type="number" min="1" max="500" value={count} onChange={event => { setCount(event.target.value); setEditing(true); }} /></label><button type="submit">读取行范围</button></form>
    {editing ? <p role="status">行范围已更改，请提交后读取。</p> : query.isPending || query.isFetching ? <p role="status">正在读取文件…</p> : query.isError ? <CodeError error={query.error} retry={() => props.navigate(props.location)} /> : <>
      <ReadEvidence {...query.data} /><p>类型：{query.data.kind} · 大小：{shown(query.data.size_bytes)} 字节 · 总行数：{shown(query.data.total_lines)}</p>
      <p>请求范围：{query.data.requested_start_line} 起 / {query.data.requested_line_count} 行 · 实际行：{shown(query.data.start_line)}–{shown(query.data.end_line)}</p>
      {query.data.kind === "text" && query.data.start_line !== null && query.data.end_line !== null && query.data.text !== "" && <pre className="code-text">{query.data.text}</pre>}
      {query.data.source_url && query.data.start_line !== null && query.data.end_line !== null && query.data.text !== "" && <a href={query.data.source_url} target="_blank" rel="noreferrer">查看源文件行 {query.data.start_line}–{query.data.end_line}</a>}
      {query.data.next_line !== null && <button type="button" onClick={() => props.navigate({ ...props.location, start: query.data.next_line! })}>继续读取第 {query.data.next_line} 行</button>}
    </>}
  </section>;
}
function SearchPanel(props: ReadProps) {
  const query = useCodeRead<SearchView>(props, "search", { path: props.location.path, query: props.location.query! }, data => data.query === props.location.query && data.items.every(hit => sameScope(hit.citation, props.version) && (!props.location.path || hit.citation.path.startsWith(`${props.location.path}/`))));
  function citationLabel(citation: CodeCitation) { return `${citation.path}:${citation.start_line}–${citation.end_line}`; }
  return <section aria-label="搜索结果"><h3>搜索结果</h3>{query.isPending || query.isFetching ? <p role="status">正在搜索…</p> : query.isError ? <CodeError error={query.error} retry={() => props.navigate(props.location)} /> : <>
    <ReadEvidence {...query.data} /><p>{query.data.complete ? "搜索覆盖完整" : "搜索覆盖不完整或未知"} · 原始条目 {query.data.scanned.tree_entries} / 候选 {query.data.scanned.candidates} / 文本文件 {query.data.scanned.text_files} / 已检查字节 {query.data.scanned.blob_bytes}</p>
    {query.data.snippets_truncated && <p>部分匹配文本因响应预算省略，引用仍保留；这不改变扫描覆盖状态。</p>}
    {!query.data.items.length && <p>{query.data.complete ? "该固定范围内没有匹配行。" : "已扫描部分未发现匹配，其他内容未知。"}</p>}
    <ul className="fact-list">{query.data.items.map(hit => <li className="panel" key={`${hit.citation.path}:${hit.citation.start_line}`}><a href={hit.citation.source_url} target="_blank" rel="noreferrer">{citationLabel(hit.citation)}</a>{hit.text === null ? <p>匹配行文本因响应预算省略</p> : <pre className="code-text">{hit.text}</pre>}<button type="button" onClick={() => props.navigate({ kind: "file", path: hit.citation.path, start: hit.citation.start_line, count: hit.citation.end_line - hit.citation.start_line + 1 })}>打开匹配行</button></li>)}</ul>
  </>}</section>;
}
