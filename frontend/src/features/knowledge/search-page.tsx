"use client";

import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api";
import { errorMessage, Pagination } from "@/features/admin/common";
import { useSession } from "@/features/session/use-session";
import { CitationPreview } from "./citation-preview";
import {
  knowledgeSearchKey, scopeSignature, submitQuery, useKnowledgeOverviews, useQueryView,
  useRepositoryCatalog, type QueryRun,
} from "./queries";
import type {
  AvailabilityReason, AvailabilityStatus, CoverageView, IndexSelection, KnowledgeView,
  QuerySelection, QueryTarget, QueryView,
} from "./types";

type IndexMode = QueryTarget | "source" | null;
type SelectedRepository = { id: string; full_name: string };

const statusLabels: Record<AvailabilityStatus, string> = {
  not_indexed: "尚未建立索引",
  building: "索引正在构建",
  ready: "索引已就绪",
  stale: "索引已过期",
};

const reasonLabels: Record<AvailabilityReason, string> = {
  no_target: "没有可检索的固定目标",
  target_not_indexed: "当前目标尚未建立索引",
  target_building: "目标版本索引正在构建",
  target_failed: "目标版本索引构建失败",
  target_pending: "目标版本索引等待构建",
  active_unavailable: "没有可用的旧 active 索引",
  vectors_reclaimed: "索引向量已回收",
  fingerprint_mismatch: "模型指纹不匹配",
  schema_unsupported: "索引模式不受支持",
  collection_missing: "索引集合不存在",
  schema_mismatch: "索引模式不匹配",
  vectors_missing: "索引向量缺失",
};

const jobLabels: Record<QueryView["status"], string> = {
  queued: "等待执行",
  running: "正在检索",
  retry_wait: "等待重试",
  succeeded: "检索完成",
  failed: "检索失败",
};

const failureCodes: Record<string, string> = {
  index_not_ready: "所选索引尚未就绪或已被替换，请重新核对版本。",
  version_changed: "语料版本已变化，请重新选择检索范围。",
  retrieval_unavailable: "本地检索暂不可用，请稍后重试。",
  budget_exhausted: "活跃检索数量已达上限，请稍后重试或等待旧查询回收。",
  storage_exhausted: "检索结果存储空间不足，请稍后重试。",
  validation_error: "问题或检索范围无效，请检查后重试。",
};

const revokedCodes = ["resource_not_found", "permission_revoked", "reauth_required", "permission_missing", "github_installation_unavailable"];

function revoked(error: unknown) {
  return error instanceof ApiError && (error.status === 401 || revokedCodes.includes(error.code));
}

function failureText(error: unknown) {
  if (error instanceof ApiError) return failureCodes[error.code] ?? errorMessage(error);
  return errorMessage(error);
}

function defaultMode(view: KnowledgeView): IndexMode {
  if (view.index_status === "ready" && view.target_sha) return "target";
  // A source-only repository owns no selectable index; it still contributes
  // its explicitly selected source documents to the query scope.
  if (view.index_status === "not_indexed" && !view.active_index_sha) return "source";
  return null;
}

function modeFor(item: SelectedRepository, view: KnowledgeView | undefined, overrides: Record<string, IndexMode>): IndexMode {
  if (item.id in overrides) return overrides[item.id];
  return view ? defaultMode(view) : null;
}

export function KnowledgeSearch() {
  const client = useQueryClient();
  const { session } = useSession();
  const userId = session?.user.id ?? "anonymous";
  const [pageCursors, setPageCursors] = useState<string[]>([]);
  const [limit, setLimit] = useState(30);
  const [selected, setSelected] = useState<SelectedRepository[]>([]);
  const [overrides, setOverrides] = useState<Record<string, IndexMode>>({});
  const [question, setQuestion] = useState("");
  const [pending, setPending] = useState(false);
  const [submitError, setSubmitError] = useState<unknown>(null);
  const [run, setRun] = useState<QueryRun | null>(null);
  const [citationId, setCitationId] = useState<string | null>(null);
  const logical = useRef<{ signature: string; key: string } | null>(null);

  const catalog = useRepositoryCatalog(userId, limit, pageCursors.at(-1));
  const overviews = useKnowledgeOverviews(userId, selected.map(item => item.id));
  const view = useQueryView(userId, run);

  function clearRun() {
    const key = knowledgeSearchKey(userId);
    setRun(null);
    setCitationId(null);
    setSubmitError(null);
    void client.cancelQueries({ queryKey: [...key, "query"] });
    client.removeQueries({ queryKey: [...key, "query"] });
    void client.cancelQueries({ queryKey: [...key, "citation"] });
    client.removeQueries({ queryKey: [...key, "citation"] });
  }

  // Revocation is derived from the current query error; the effect only
  // clears external caches, never the render state.
  const revokedNow = revoked(view.error);
  useEffect(() => {
    if (!revokedNow) return;
    const key = knowledgeSearchKey(userId);
    void client.cancelQueries({ queryKey: [...key, "query"] });
    client.removeQueries({ queryKey: [...key, "query"] });
    void client.cancelQueries({ queryKey: [...key, "citation"] });
    client.removeQueries({ queryKey: [...key, "citation"] });
  }, [revokedNow, client, userId]);

  useEffect(() => {
    const key = knowledgeSearchKey(userId);
    return () => {
      void client.cancelQueries({ queryKey: key });
      client.removeQueries({ queryKey: key });
    };
  }, [client, userId]);

  function toggle(item: { id: string; full_name: string }) {
    clearRun();
    if (selected.some(value => value.id === item.id)) {
      setSelected(selected.filter(value => value.id !== item.id));
      setOverrides(current => { const next = { ...current }; delete next[item.id]; return next; });
      return;
    }
    if (selected.length >= 3) return;
    setSelected([...selected, { id: item.id, full_name: item.full_name }]);
  }

  function choose(item: SelectedRepository, mode: IndexMode) {
    clearRun();
    setOverrides(current => ({ ...current, [item.id]: mode }));
  }

  const indexes: IndexSelection[] = [];
  selected.forEach((item, index) => {
    const knowledge = overviews[index]?.data;
    const mode = modeFor(item, knowledge, overrides);
    if (!knowledge || (mode !== "target" && mode !== "active")) return;
    const sha = mode === "target" ? knowledge.target_sha : knowledge.active_index_sha;
    if (sha) indexes.push({ repository_id: item.id, sha, target: mode });
  });
  const signature = scopeSignature(selected.map(item => item.id), indexes);
  const bytes = new TextEncoder().encode(question).length;
  const allLoaded = selected.every((item, index) => !!overviews[index]?.data);
  const unresolved = selected.some((item, index) => {
    const knowledge = overviews[index]?.data;
    return !!knowledge && modeFor(item, knowledge, overrides) === null;
  });
  const selectionError = selected.length === 0 ? "至少选择一个仓库。"
    : !allLoaded ? "正在确认所选仓库的索引状态…"
      : unresolved ? "请为每个已选仓库明确选择索引版本或仅作为来源。"
        : indexes.length === 0 ? "至少选择一个可检索的索引；仅来源仓库不提供索引。"
          : question.trim() === "" ? "请输入问题。"
            : bytes > 2048 ? "问题超过 2048 字节，请缩短后再提交。"
              : null;
  const canSubmit = !selectionError && !pending;
  const names = new Map(selected.map(item => [item.id, item.full_name]));
  catalog.data?.items.forEach(item => names.set(item.id, item.full_name));

  async function submit() {
    if (!canSubmit) return;
    const selection: QuerySelection = { selected_repository_ids: selected.map(item => item.id), indexes };
    const logicalSignature = `${signature}|${question}`;
    if (logical.current?.signature !== logicalSignature) {
      logical.current = { signature: logicalSignature, key: crypto.randomUUID() };
    }
    setPending(true);
    setSubmitError(null);
    setRun(null);
    setCitationId(null);
    try {
      const receipt = await submitQuery(selected[0].id, selection, question, logical.current.key);
      logical.current = null;
      setRun({
        queryId: receipt.query_id,
        jobId: receipt.job_id,
        anchorId: selected[0].id,
        signature,
        question,
      });
    } catch (failure) {
      if (!(failure instanceof DOMException && failure.name === "AbortError")) setSubmitError(failure);
    } finally {
      setPending(false);
    }
  }

  const data = view.data;
  const current = !revokedNow && run && data && scopeSignature(data.selection.selected_repository_ids, data.selection.indexes) === run.signature ? data : null;
  const result = current?.result ?? null;
  const manifest = current?.manifest ?? null;
  const catalogItems = catalog.data?.items ?? [];

  return <section aria-label="检索与引用">
    <h2>检索与引用</h2>
    <p>检索在本地索引上执行，只返回你明确选择范围内的证据。检索不读取云模型配置；云未配置不会阻止检索。</p>
    {revokedNow && <p role="alert" className="banner error">权限已撤销或会话已失效，检索结果与缓存已清除。</p>}

    <section className="panel" aria-label="检索范围">
      <h3>检索范围</h3>
      <p>团队安装授权不会自动扩大范围；只有下面勾选的仓库和明确选择的索引会参与检索。最多选择 3 个仓库。</p>
      <div className="member-form">
        <label>每页数量<select value={limit} onChange={event => { setLimit(Number(event.target.value)); setPageCursors([]); }}>
          <option value="30">30</option>
          <option value="100">100</option>
        </select></label>
      </div>
      {catalog.isPending ? <p role="status">正在读取仓库列表…</p> : catalog.isError ? <p role="alert" className="banner error">{errorMessage(catalog.error)}</p> : <>
        {!catalogItems.length && <p>没有可选择的仓库。</p>}
        <ul className="fact-list">{catalogItems.map(item => {
          const checked = selected.some(value => value.id === item.id);
          return <li key={item.id}>
            <label>
              <input
                type="checkbox"
                aria-label={`选择仓库 ${item.full_name}`}
                checked={checked}
                disabled={!checked && selected.length >= 3}
                onChange={() => toggle(item)}
              />
              {" "}{item.full_name}{item.private ? " · 私有" : ""}
            </label>
          </li>;
        })}</ul>
        <Pagination
          page={pageCursors.length + 1}
          busy={catalog.isFetching}
          hasNext={!!catalog.data?.next_cursor}
          previous={() => setPageCursors(pageCursors.slice(0, -1))}
          next={() => { if (catalog.data?.next_cursor) setPageCursors([...pageCursors, catalog.data.next_cursor]); }}
        />
      </>}
    </section>

    {selected.map((item, index) => {
      const knowledge = overviews[index]?.data;
      const mode = modeFor(item, knowledge, overrides);
      const activeDistinct = !!knowledge && !!knowledge.active_index_sha && knowledge.active_index_sha !== knowledge.target_sha;
      return <section className="panel fact-collection" aria-label={`索引选择 ${item.full_name}`} key={item.id}>
        <h3>索引选择 {item.full_name}</h3>
        {!knowledge ? <p role="status">正在确认索引状态…</p> : <>
          <dl className="fact-fields">
            <dt>固定目标</dt><dd>{knowledge.index_status === "not_indexed"
              ? `来源 SHA ${knowledge.target_sha ?? "未固定 Workspace（仅上传）"}`
              : `目标代次 ${knowledge.target_generation} · 目标 SHA ${knowledge.target_sha ?? "未固定 Workspace（仅上传）"}`}</dd>
            <dt>索引状态</dt><dd>{statusLabels[knowledge.index_status]}{knowledge.index_reason ? ` · ${reasonLabels[knowledge.index_reason] ?? knowledge.index_reason}` : ""}</dd>
            {knowledge.active_index_sha && <><dt>旧 active 版本</dt><dd>当前 active SHA {knowledge.active_index_sha}{knowledge.active_index_generation ? ` · 代次 ${knowledge.active_index_generation}` : ""}</dd></>}
          </dl>
          <div className="member-form">
            <label><input type="radio" name={`index-${item.id}`} checked={mode === "target"} disabled={knowledge.index_status !== "ready" || !knowledge.target_sha} onChange={() => choose(item, "target")} /> 目标版本（固定目标 SHA）</label>
            {activeDistinct && <label><input type="radio" name={`index-${item.id}`} checked={mode === "active"} onChange={() => choose(item, "active")} /> 使用旧 active SHA</label>}
            <label><input type="radio" name={`index-${item.id}`} checked={mode === "source"} onChange={() => choose(item, "source")} /> {knowledge.index_status === "not_indexed" && !knowledge.active_index_sha ? "仅来源（无索引）" : "仅作为来源（不检索此仓库索引）"}</label>
          </div>
          {mode === "target" && knowledge.index_status !== "ready" && <p role="alert" className="banner error">目标索引尚未就绪，不能选择目标版本。</p>}
          {mode === "active" && knowledge.active_index_sha && <p role="status">已明确选择旧 active SHA {knowledge.active_index_sha}；目标 SHA 为 {knowledge.target_sha ?? "未固定"}。此版本可能落后于目标源码。</p>}
          {mode === "source" && <p>此仓库仅提供被选来源的片段，不建立或使用索引。</p>}
          {mode === null && <p role="status">请明确选择一个可检索索引，或选择“仅作为来源”。</p>}
        </>}
      </section>;
    })}

    <section className="panel" aria-label="提问">
      <h3>检索问题</h3>
      <label>问题<textarea value={question} rows={3} onChange={event => setQuestion(event.target.value)} /></label>
      <p>{bytes} / 2048 字节。问题不会发送到云模型，只用于本地检索与重排。</p>
      <button type="button" disabled={!canSubmit} onClick={() => void submit()}>{pending ? "正在提交…" : "开始检索"}</button>
      {selectionError && <p role="status">{selectionError}</p>}
      {!!submitError && <p role="alert" className="banner error">{failureText(submitError)}</p>}
    </section>

    {run && <section className="panel" aria-label="检索进度">
      <h3>检索进度</h3>
      {view.isPending ? <p role="status">正在读取检索状态…</p>
        : view.isError ? <p role="alert" className="banner error">{failureText(view.error)}</p>
          : data ? <p role="status">{jobLabels[data.status]}{data.status === "failed" && data.error_code ? ` · ${failureCodes[data.error_code] ?? `失败码 ${data.error_code}`}` : ""}</p> : null}
      <p>离开页面会停止轮询，但不会取消后端检索任务；浏览器取消请求不等于取消任务。</p>
    </section>}

    {run && current && result && <section className="panel fact-collection" aria-label="检索结果">
      <h3>检索结果</h3>
      {result.status === "no_evidence" ? <p>没有找到可引用的证据。请查看覆盖范围；空结果不代表范围内没有内容。</p> : <>
        <p>最多显示 6 条证据；排序为检索顺序，不代表事实概率或相关性确信度。</p>
        <ol className="fact-list">{result.evidence.map(item => <li key={item.citation_id}>
          <p>证据 {item.rank}</p>
          <pre className="code-text">{item.text}</pre>
          {manifest?.entries.some(entry => entry.citation_id === item.citation_id) && (
            <button type="button" onClick={() => setCitationId(item.citation_id)}>查看引用</button>
          )}
        </li>)}</ol>
      </>}
      <section className="panel" aria-label="覆盖范围">
        <h3>覆盖范围</h3>
        <ul className="fact-list">{result.coverage.map((item: CoverageView) => <li key={item.repository_id}>
          <span>{names.get(item.repository_id) ?? item.repository_id}</span>
          <span> · SHA {item.sha ?? "未固定"} · 代次 {item.target_generation}</span>
          <span> · 已导入 {item.included} · 跳过 {item.skipped} · 不支持 {item.unsupported} · 超限 {item.over_limit}</span>
          <span> · {item.unscanned ? "存在未扫描范围" : "未发现未扫描范围"} · {item.complete ? "枚举完整" : "枚举未完成"}</span>
          {(!item.complete || item.unscanned) && <strong> · 范围不完整</strong>}
        </li>)}</ul>
        <p>覆盖范围说明实际参与检索的语料；不完整范围不会被当作完整结果。</p>
      </section>
    </section>}

    {run && current && citationId && <CitationPreview key={citationId} userId={userId} run={run} citationId={citationId} />}
  </section>;
}
