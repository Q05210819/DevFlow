"use client";

import { useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api";
import { errorMessage, Pagination } from "@/features/admin/common";
import { useRepository, useRepositoryQuery } from "@/features/repositories/queries";
import type { WorkspaceView } from "@/features/repositories/types";
import { MAX_UPLOAD_BYTES, useKnowledge, useKnowledgeActions, useKnowledgeDocument, usePathPreview } from "./queries";
import type { CorpusState, DocumentSummary, ImportReceipt, KnowledgeView, PathEntry, RevisionSummary } from "./types";

const corpusLabels: Record<CorpusState["status"], string> = { queued: "等待构建", building: "正在构建", ready: "已就绪", ready_partial: "部分就绪", failed: "构建失败", superseded: "已被新目标取代" };
const reasonLabels: Record<string, string> = { unsupported_path: "路径无法解析", excluded_hidden: "隐藏路径", excluded_directory: "排除目录", excluded_credential: "凭据文件名", unsupported: "类型不受支持", not_selected: "不在所选范围", source_deleted: "来源文档已删除", invalid_path: "路径无法解析", tree_limit: "目录扫描上限，范围未知", scan_limit: "扫描预算已到达", too_large: "文件超过 1 MiB", unknown_size: "无法确定文件大小", symlink: "符号链接（不跟随）", gitlink: "子模块引用（不跟随）", lfs_pointer: "LFS 指针（不下载对象）", non_utf8: "非 UTF-8 文本", binary: "二进制内容" };
const availabilityLabels: Record<RevisionSummary["availability"], string> = { available: "", expired: " · 正文已回收", deleted: " · 已删除", unavailable: " · 来源不可用" };
const indexLabels: Record<KnowledgeView["index_status"], string> = { not_indexed: "尚未建立索引", building: "索引正在构建", ready: "索引已就绪", stale: "索引已过期" };
const outcomeLabels: Record<PathEntry["outcome"], string> = { selected: "将导入", unsupported: "不支持", excluded: "已排除" };

function reason(value: string | null | undefined) {
  return value ? reasonLabels[value] ?? value : "";
}

function bytes(value: number) {
  return value >= 1048576 ? `${(value / 1048576).toFixed(1)} MiB` : value >= 1024 ? `${(value / 1024).toFixed(0)} KiB` : `${value} B`;
}

function CorpusPanel({ view }: { view: KnowledgeView }) {
  const corpus = view.corpus;
  return <section className="panel fact-collection" aria-label="语料状态"><h3>语料状态</h3>
    <dl className="fact-fields">
      <dt>目标代次 / 固定 SHA</dt><dd>{view.target_generation} / {view.target_sha ?? "未固定 Workspace（仅上传）"}</dd>
      <dt>路径规则</dt><dd>{view.path_rule_version}</dd>
      <dt>检索索引</dt><dd>{indexLabels[view.index_status]}{view.index_reason ? ` · ${view.index_reason}` : ""}{view.active_index_sha ? ` · 旧 active SHA ${view.active_index_sha}` : ""}</dd>
    </dl>
    {view.needs_reimport && <p role="alert" className="banner error">此代次由旧版导入产生，缺少完整组合语义，需要重新导入后才能作为完整语料。</p>}
    {!corpus ? <p>尚未提交任何知识来源。上传文档或选择固定 Workspace 版本后开始导入。</p> : <>
      <p role="status">{corpusLabels[corpus.status]} · 已处理 {corpus.processed_chunks} 个分块{corpus.scan_reason ? ` · ${reason(corpus.scan_reason)}` : ""}</p>
      <p>已导入 {corpus.included} · 跳过 {corpus.skipped} · 不支持 {corpus.unsupported} · 超限 {corpus.over_limit} · {corpus.unscanned ? "存在未扫描范围" : "枚举完整"}{corpus.complete ? " · 枚举已完成" : " · 枚举未完成"}</p>
      {["queued", "building"].includes(corpus.status) && <p>构建在后台分批执行；页面可见时有限轮询，暂停后可手动重新确认。</p>}
    </>}
    <p>扫描上限：{view.limits.max_files} 个文件 / {bytes(view.limits.max_scan_bytes)} / 单文件 {bytes(view.limits.max_file_bytes)}。这些是范围筛选，不是秘密内容检测保证。</p>
  </section>;
}

function UploadPanel({ onDone }: { onDone: () => void }) {
  const scope = useRepository();
  const actions = useKnowledgeActions();
  const [file, setFile] = useState<File | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // One logical upload keeps its stage and admission keys across retries so a
  // lost response never stages the same bytes twice.
  const logical = useRef<{ signature: string; stageKey: string; admissionKey: string; stagingId: string | null } | null>(null);
  const size = file?.size ?? 0;
  const allowed = !!file && size > 0 && size <= MAX_UPLOAD_BYTES && /\.(md|txt)$/i.test(file.name) && !file.name.startsWith(".");
  async function submit() {
    if (!file || !allowed || pending) return;
    const signature = `${file.name}:${file.size}:${file.lastModified}`;
    if (logical.current?.signature !== signature) logical.current = { signature, stageKey: crypto.randomUUID(), admissionKey: crypto.randomUUID(), stagingId: null };
    const state = logical.current;
    setPending(true); setError(null); setNotice(null);
    try {
      let stagingId = state.stagingId;
      if (!stagingId) {
        const staged = await actions.stage(file, file.name, state.stageKey);
        stagingId = staged.staging_id; state.stagingId = stagingId;
      }
      const receipt: ImportReceipt = await actions.admit({ kind: "upload", repository_id: scope.repository.id, filename: file.name, staging_id: stagingId }, state.admissionKey);
      logical.current = null;
      setNotice(`已提交导入（目标代次 ${receipt.target_generation}）。资料将供该仓库获准成员在后续检索与问答中使用。`);
      setFile(null); onDone();
    } catch (failure) { setError(failure); }
    finally { setPending(false); }
  }
  return <section className="panel" aria-label="上传文档"><h3>上传文档</h3>
    <p>仅支持单个 <code>.md</code> 或 <code>.txt</code> 文件，UTF-8 编码，最大 {bytes(MAX_UPLOAD_BYTES)}。上传内容不会作为 HTML 执行。</p>
    <p>上传的资料将供该仓库已获准成员在检索与问答中使用；删除只使本文档退出语料，不会删除 Git 文件。</p>
    <div className="member-form"><label>选择文件<input type="file" accept=".md,.txt" onChange={event => { setFile(event.target.files?.[0] ?? null); logical.current = null; setNotice(null); }} /></label></div>
    {file && !allowed && <p role="alert" className="banner error">{size === 0 ? "文件为空。" : size > MAX_UPLOAD_BYTES ? `文件超过 ${bytes(MAX_UPLOAD_BYTES)}。` : "只允许单个 .md 或 .txt 文件。"}</p>}
    <button type="button" disabled={!allowed || pending} onClick={() => void submit()}>{pending ? "正在上传…" : "上传并提交导入"}</button>
    {!!error && <p role="alert" className="banner error">{error instanceof ApiError && error.code === "payload_too_large" ? "文件超过 1 MiB，请拆分后再上传。" : error instanceof ApiError && error.code === "stage_expired" ? "暂存已过期，请重新选择文件上传。" : errorMessage(error)}</p>}
    {notice && <p role="status" className="banner">{notice}</p>}
  </section>;
}

function PathRows({ items }: { items: PathEntry[] }) {
  if (!items.length) return <p>此页没有目录条目。</p>;
  return <ul className="fact-list">{items.map((item, index) => <li key={item.path ?? `unknown:${index}`}>
    <span>{item.path ?? "（无法解析的路径）"}</span>
    <span> · {outcomeLabels[item.outcome]}{item.reason ? ` · ${reason(item.reason)}` : ""}{item.size !== null ? ` · ${bytes(item.size)}` : ""}</span>
  </li>)}</ul>;
}

function WorkspacePanel({ onDone }: { onDone: () => void }) {
  const scope = useRepository();
  const actions = useKnowledgeActions();
  const [workspaceId, setWorkspaceId] = useState("");
  const [documentPaths, setDocumentPaths] = useState("README,README.md,README.txt,README.rst,docs");
  const [codePaths, setCodePaths] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [cursors, setCursors] = useState<string[]>([]);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const admission = useRef<{ signature: string; key: string } | null>(null);
  const valid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(workspaceId);
  const documents = useMemo(() => documentPaths.split(",").map(value => value.trim()).filter(Boolean), [documentPaths]);
  const code = useMemo(() => codePaths.split(",").map(value => value.trim()).filter(Boolean), [codePaths]);
  const selectionEmpty = !documents.length && !code.length;
  const version = useRepositoryQuery<WorkspaceView>(["knowledge-version", workspaceId], `/workspaces/${encodeURIComponent(workspaceId)}`, {}, false, valid);
  const ready = valid && version.isSuccess && version.data.status === "ready" ? version.data : null;
  const preview = usePathPreview(ready?.id ?? null, ready?.commit_sha ?? null, documents, code, cursors.at(-1));
  function reset() { setConfirmed(false); setCursors([]); admission.current = null; setError(null); setNotice(null); }
  async function submit() {
    if (!ready || pending) return;
    const signature = `${ready.id}:${ready.commit_sha}:${documents.join(",")}:${code.join(",")}`;
    if (admission.current?.signature !== signature) admission.current = { signature, key: crypto.randomUUID() };
    setPending(true); setError(null); setNotice(null);
    try {
      const receipt = await actions.admit({ kind: "workspace", repository_id: scope.repository.id, workspace_id: ready.id, document_paths: documents, code_paths: code }, admission.current.key);
      admission.current = null;
      setNotice(`已提交导入（目标代次 ${receipt.target_generation}），固定 SHA ${ready.commit_sha}。`);
      onDone();
    } catch (failure) { setError(failure); }
    finally { setPending(false); }
  }
  return <section className="panel" aria-label="导入固定版本"><h3>导入固定 Workspace 版本</h3>
    <p>只导入明确选择的文档与代码目录，不会扫描整个仓库。请先准备固定版本，再核对解析出的完整 SHA 与路径清单。</p>
    <div className="member-form">
      <label>Workspace 版本 ID<input value={workspaceId} placeholder="已准备版本的 UUID" onChange={event => { setWorkspaceId(event.target.value.trim()); reset(); }} /></label>
      <label>文档路径（逗号分隔）<input value={documentPaths} onChange={event => { setDocumentPaths(event.target.value); reset(); }} /></label>
      <label>代码目录（逗号分隔，可留空）<input value={codePaths} onChange={event => { setCodePaths(event.target.value); reset(); }} /></label>
    </div>
    {valid && version.isError && <p role="alert" className="banner error">{errorMessage(version.error)}</p>}
    {valid && version.isSuccess && version.data.status !== "ready" && <p role="alert" className="banner error">该版本尚未准备完成（{version.data.status}），不能作为导入来源。</p>}
    {ready && <>
      <p role="status">固定 SHA：{ready.commit_sha} · 来源仓库 ID：{ready.source_repository_id} · 路径规则：knowledge-paths-v1</p>
      {selectionEmpty && <p role="alert" className="banner error">至少选择一个文档路径或代码目录；空选择不会导入任何文件。</p>}
      {!selectionEmpty && (preview.isPending || preview.isFetching ? <p role="status">正在读取真实目录元数据…</p> : preview.isError ? <p role="alert" className="banner error">{errorMessage(preview.error)}</p> : preview.data && <>
        <PathRows items={preview.data.items} />
        <p>{preview.data.exhausted ? "此固定 SHA 的目录枚举完整。" : preview.data.truncated ? "目录已被截断，后续范围未知。" : "已到达已知前缀末尾，但未证明枚举完整。"} 目录元数据只是范围确认，不代表已完成 UTF-8 导入。</p>
        <Pagination page={cursors.length + 1} busy={preview.isFetching} hasNext={!!preview.data.next_cursor} previous={() => setCursors(cursors.slice(0, -1))} next={() => { if (preview.data?.next_cursor) setCursors([...cursors, preview.data.next_cursor]); }} />
      </>)}
      <label><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} /> 我已核对完整 SHA、来源仓库与路径范围</label>
      <button type="button" disabled={!confirmed || pending || selectionEmpty} onClick={() => void submit()}>{pending ? "正在提交…" : "确认并开始导入"}</button>
    </>}
    {!!error && <p role="alert" className="banner error">{errorMessage(error)}</p>}
    {notice && <p role="status" className="banner">{notice}</p>}
  </section>;
}

function DocumentDetail({ document, onChanged }: { document: DocumentSummary; onChanged: () => void }) {
  const actions = useKnowledgeActions();
  const [revisionId, setRevisionId] = useState<string | null>(null);
  const [revisionCursors, setRevisionCursors] = useState<string[]>([]);
  const [cursors, setCursors] = useState<string[]>([]);
  const [confirming, setConfirming] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const query = useKnowledgeDocument(document.document_id, revisionId, cursors.at(-1) ?? null, revisionCursors.at(-1) ?? null);
  async function remove() {
    if (pending) return;
    setPending(true); setError(null); setNotice(null);
    try {
      const receipt = await actions.remove(document.document_id, crypto.randomUUID());
      setNotice(`已提交删除（目标代次 ${receipt.target_generation}）。此来源文档会立即退出新检索，正文回收由后续清理任务处理。`);
      setConfirming(false); onChanged();
    } catch (failure) { setError(failure); }
    finally { setPending(false); }
  }
  return <section className="panel" aria-label="文档详情"><h3>{document.path ?? document.filename ?? document.document_id}</h3>
    <dl className="fact-fields"><dt>文档 ID / 类型</dt><dd>{document.document_id} / {document.kind === "upload" ? "上传文档" : "Workspace 文件"}</dd><dt>已发布修订</dt><dd>{document.published_revisions}</dd></dl>
    {query.isPending ? <p role="status">正在读取文档元数据…</p> : query.isError ? <p role="alert" className="banner error">{errorMessage(query.error)}</p> : query.data && <>
      {!query.data.revisions.length && <p>此文档尚无已发布修订；暂存或未发布的修订不会出现在这里，也不能被读取。</p>}
      {!!query.data.revisions.length && <ul className="fact-list">{query.data.revisions.map(item => <li key={item.revision_id}>
        <button type="button" onClick={() => { setCursors([]); setRevisionId(item.revision_id); }}>{item.revision_hash.slice(0, 12)} · {item.published_at}{item.is_current ? " · 当前语料" : " · 历史修订"}{availabilityLabels[item.availability]}</button>
      </li>)}</ul>}
      {!!query.data.revisions.length && <Pagination page={revisionCursors.length + 1} busy={query.isFetching} hasNext={!!query.data.revisions_next_cursor} previous={() => setRevisionCursors(revisionCursors.slice(0, -1))} next={() => { if (query.data?.revisions_next_cursor) setRevisionCursors([...revisionCursors, query.data.revisions_next_cursor]); }} />}
      {query.data.preview && <>
        <p role="status">{query.data.preview.reason === "source_expired" ? "正文已回收或过期，引用仍保留。" : query.data.preview.reason === "source_unavailable" ? "来源当前不可用。" : query.data.preview.is_current ? "当前语料修订" : "历史修订预览"}{query.data.preview.partial ? " · 仅显示部分内容" : ""}</p>
        {query.data.preview.text !== null && query.data.preview.text !== "" && <pre className="code-text">{query.data.preview.text}</pre>}
        {query.data.preview.text === "" && <p>此范围为空白文本。</p>}
        {query.data.preview.next_cursor && <button type="button" onClick={() => setCursors([...cursors, query.data!.preview!.next_cursor!])}>继续读取后续内容</button>}
        {cursors.length > 0 && <button type="button" onClick={() => setCursors(cursors.slice(0, -1))}>返回上一段</button>}
      </>}
    </>}
    {!document.deleted && (confirming ? <><p role="alert" className="banner">删除会使此来源文档立即退出新的检索与问答语料，且本阶段没有恢复入口。确认删除？</p>
      <button type="button" disabled={pending} onClick={() => void remove()}>{pending ? "正在提交…" : "确认删除"}</button>
      <button type="button" disabled={pending} onClick={() => setConfirming(false)}>取消</button></> : <button type="button" onClick={() => setConfirming(true)}>删除此来源文档</button>)}
    {document.deleted && <p className="banner">此来源文档已逻辑删除，正文回收由后续清理任务处理。</p>}
    {!!error && <p role="alert" className="banner error">{errorMessage(error)}</p>}
    {notice && <p role="status" className="banner">{notice}</p>}
  </section>;
}

export function Knowledge() {
  const client = useQueryClient();
  const scope = useRepository();
  const [cursors, setCursors] = useState<string[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const cursor = cursors.at(-1);
  const overview = useKnowledge(cursor, true);
  const refresh = () => {
    void client.invalidateQueries({ queryKey: [...scope.prefix, "knowledge"] });
    void client.invalidateQueries({ queryKey: [...scope.prefix, "knowledge-document"] });
  };
  const mutated = () => { setCursors([]); setSelected(null); refresh(); };
  const documentChanged = () => { setCursors([]); refresh(); };
  const staleCursor = overview.error instanceof ApiError && ["invalid_cursor", "version_changed"].includes(overview.error.code);
  const document = overview.data?.documents.items.find(item => item.document_id === selected) ?? null;
  return <section aria-label="知识库"><h2>知识库</h2>
    <p>知识语料来自明确选择的上传文档或固定 Workspace 版本；检索在“检索”页按明确选择的仓库与索引版本独立执行，不沿用此页面范围。</p>
    {overview.isPending ? <p role="status">正在确认知识状态…</p> : overview.isError ? <><p role="alert" className="banner error">{errorMessage(overview.error)}</p><button type="button" onClick={() => { if (staleCursor) { setCursors([]); setSelected(null); } else { void overview.refetch(); } }}>重新加载</button>{cursors.length > 0 && <button type="button" onClick={() => { setCursors([]); setSelected(null); }}>返回第一页</button>}</> : overview.data && <>
      <CorpusPanel view={overview.data} />
      <div className="repository-controls"><button type="button" onClick={refresh}>刷新知识状态</button></div>
      <section className="panel fact-collection" aria-label="来源文档"><h3>来源文档</h3>
        {!overview.data.documents.items.length && <p>此页没有来源文档；若有后续页可继续。</p>}
        <ul className="fact-list">{overview.data.documents.items.map(item => <li key={item.document_id}>
          <button type="button" onClick={() => setSelected(item.document_id)}>{item.path ?? item.filename ?? item.document_id}</button>
          <span> · {item.kind === "upload" ? "上传" : "Workspace"}{item.deleted ? " · 已删除" : ""} · 已发布修订 {item.published_revisions}</span>
        </li>)}</ul>
        <Pagination page={cursors.length + 1} busy={overview.isFetching} hasNext={!!overview.data.documents.next_cursor} previous={() => setCursors(cursors.slice(0, -1))} next={() => { if (overview.data?.documents.next_cursor) setCursors([...cursors, overview.data.documents.next_cursor]); }} />
      </section>
      {document && <DocumentDetail key={document.document_id} document={document} onChanged={documentChanged} />}
      <UploadPanel onDone={mutated} />
      <WorkspacePanel onDone={mutated} />
    </>}
  </section>;
}
