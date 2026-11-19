"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api";
import { errorMessage } from "@/features/admin/common";
import { isTerminal, useJobEvents } from "@/features/jobs/use-job-events";
import { useKnowledgeOverviews, useRepositoryCatalog } from "@/features/knowledge/queries";
import type { IndexSelection, QuerySelection } from "@/features/knowledge/types";
import { useSession } from "@/features/session/use-session";
import {
  cancelJob, chatKey, createConversation, deleteConversation, submitMessage,
  useCitationPreview, useConversations, useMessagePages,
} from "./queries";
import type { CitationView, RunReceipt } from "./types";

type IndexMode = "target" | "active" | "source";

const revokedCodes = [
  "resource_not_found", "permission_revoked", "reauth_required", "permission_missing",
  "github_installation_unavailable",
];

function revoked(error: unknown) {
  return error instanceof ApiError && (error.status === 401 || revokedCodes.includes(error.code));
}

function failureText(error: unknown) {
  if (error instanceof ApiError) {
    switch (error.code) {
      case "model_not_configured": return "云模型尚未配置；本地检索仍可用，配置完成后再提问。";
      case "budget_exhausted": return "本月或本次预算不足，已停止调用云模型。";
      case "run_in_progress": return "该会话已有进行中的回答，请等待或取消后再提交。";
      case "conversation_archived": return "该会话已删除，请新建会话。";
      case "index_not_ready": return "所选索引尚未就绪，请核对仓库版本。";
      case "version_changed": return "语料版本在回答过程中变化，请重新提交本轮问题。";
      case "context_limit": return "证据超出单次请求上限，未能发送；可缩小仓库范围后重试。";
      case "answer_validation_failed": return "模型输出未通过引用校验，未发布答案。";
      case "model_call_outcome_unknown": return "模型请求结果未知，已保守保留预占，请勿立即重发。";
      case "cancelled": return "本次回答已取消。";
    }
    return errorMessage(error);
  }
  return errorMessage(error);
}

const chatStatusLabels: Record<string, string> = {
  queued: "等待执行",
  running: "正在生成回答",
  retry_wait: "等待重试",
  succeeded: "回答完成",
  failed: "回答失败",
};

function runStatusLabel(status: string | undefined) {
  if (!status) return "尚未运行";
  return chatStatusLabels[status] ?? status;
}

function short(value: string) {
  return value.length > 8 ? value.slice(0, 8) : value;
}

export function ConversationsIndex() {
  const router = useRouter();
  const client = useQueryClient();
  const { session } = useSession();
  const userId = session?.user.id ?? "anonymous";
  const catalog = useRepositoryCatalog(userId, 30, undefined);
  const conversations = useConversations(userId);
  const [selected, setSelected] = useState<string[]>([]);
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<unknown>(null);
  const logical = useRef<{ signature: string; key: string } | null>(null);

  const revokedNow = revoked(conversations.error);
  useEffect(() => {
    if (!revokedNow) return;
    void client.cancelQueries({ queryKey: chatKey(userId) });
    client.removeQueries({ queryKey: chatKey(userId) });
  }, [revokedNow, client, userId]);

  const names = useMemo(() => {
    const map = new Map<string, string>();
    catalog.data?.items.forEach(item => map.set(item.id, item.full_name));
    return map;
  }, [catalog.data]);

  function toggle(id: string) {
    setFailure(null);
    if (selected.includes(id)) {
      setSelected(selected.filter(value => value !== id));
      return;
    }
    if (selected.length >= 3) return;
    setSelected([...selected, id]);
  }

  async function create() {
    if (pending || selected.length === 0) return;
    const signature = [...selected].sort().join(",");
    if (logical.current?.signature !== signature) {
      logical.current = { signature, key: crypto.randomUUID() };
    }
    setPending(true);
    setFailure(null);
    try {
      const created = await createConversation(selected, logical.current.key);
      logical.current = null;
      await client.invalidateQueries({ queryKey: [...chatKey(userId), "conversations"] });
      router.push(`/conversations/${created.id}`);
    } catch (error) {
      if (!(error instanceof DOMException && error.name === "AbortError")) setFailure(error);
    } finally {
      setPending(false);
    }
  }

  return <section aria-label="私有会话">
    <h2>私有会话</h2>
    <p>会话仅自己可见，创建时固定 1–3 个仓库。更换仓库请新建会话；历史消息不会自动加入下一轮云请求。</p>
    {revokedNow && <p role="alert" className="banner error">权限已撤销或会话已失效，相关缓存已清除。</p>}

    <section className="panel" aria-label="新建会话">
      <h3>新建会话</h3>
      {catalog.isPending ? <p role="status">正在读取仓库列表…</p>
        : catalog.isError ? <p role="alert" className="banner error">{failureText(catalog.error)}</p>
          : <ul className="fact-list">{(catalog.data?.items ?? []).map(item => <li key={item.id}>
            <label>
              <input
                type="checkbox"
                aria-label={`选择仓库 ${item.full_name}`}
                checked={selected.includes(item.id)}
                disabled={!selected.includes(item.id) && selected.length >= 3}
                onChange={() => toggle(item.id)}
              />
              {" "}{item.full_name}{item.private ? " · 私有" : ""}
            </label>
          </li>)}</ul>}
      <button type="button" disabled={pending || selected.length === 0} onClick={() => void create()}>
        {pending ? "正在创建…" : "创建会话"}
      </button>
      {!!failure && <p role="alert" className="banner error">{failureText(failure)}</p>}
    </section>

    <section className="panel" aria-label="会话列表">
      <h3>我的会话</h3>
      {conversations.isPending ? <p role="status">正在读取会话…</p>
        : conversations.isError ? <p role="alert" className="banner error">{failureText(conversations.error)}</p>
          : (conversations.data?.conversations.length ?? 0) === 0 ? <p>还没有会话。选择仓库后创建。</p>
            : <ul className="fact-list">{conversations.data!.conversations.map(item => <li key={item.id}>
              <Link href={`/conversations/${item.id}`}>{item.selected_repository_ids.map(id => names.get(id) ?? short(id)).join(" · ")}</Link>
              <span> · {runStatusLabel((item.active_run ?? item.latest_run)?.status)}</span>
              {item.active_run && <span> · 进行中</span>}
            </li>)}</ul>}
    </section>
  </section>;
}

function RunProgress({ jobId, onTerminal }: { jobId: string; onTerminal: () => void }) {
  const { job, events, connection } = useJobEvents(jobId, 0, false);
  const settled = job ? isTerminal(job.status) : false;
  const latest = events.at(-1)?.status;
  // The persisted Job snapshot lags the stream; progress must show the newest
  // safe status, and only a terminal snapshot may override it.
  const status = job && isTerminal(job.status) ? job.status : latest ?? job?.status;
  useEffect(() => {
    if (settled) onTerminal();
  }, [settled, onTerminal]);
  return <section className="panel" aria-label="回答进度">
    <h3>回答进度</h3>
    {connection === "denied" && <p role="alert" className="banner error">会话权限已撤销，进度与答案已停止加载。</p>}
    {connection === "loading" && <p role="status">正在读取回答状态…</p>}
    {connection === "retrying" && <p className="banner" role="status">连接暂时中断，正在恢复…</p>}
    <p role="status">
      {runStatusLabel(status)}
      {job?.status === "failed" && job.error_code ? ` · ${failureText(new ApiError(409, job.error_code))}` : ""}
    </p>
    <p>断开页面不会取消任务；最终答案只从私有历史读取，进度流不会传输未校验的模型内容。</p>
  </section>;
}

function CitationPanel({ conversationId, messageId, citation }: {
  conversationId: string; messageId: string; citation: CitationView;
}) {
  const { session } = useSession();
  const userId = session?.user.id ?? "anonymous";
  const [cursor, setCursor] = useState<string | null>(null);
  const preview = useCitationPreview(userId, conversationId, messageId, citation.citation_id, cursor);
  return <section className="panel" aria-label="引用预览">
    <h3>引用预览</h3>
    <p>{citation.path ?? "上传文档"} · 第 {citation.start_line}–{citation.end_line} 行{citation.sha ? ` · SHA ${citation.sha.slice(0, 12)}` : ""}</p>
    {preview.isPending ? <p role="status">正在读取引用…</p>
      : preview.isError ? <p role="alert" className="banner error">{failureText(preview.error)}</p>
        : preview.data ? <>
          {preview.data.reason === "source_expired"
            ? <p role="alert">来源正文已过期，仅保留引用标识。</p>
            : <pre className="code-text">{preview.data.text ?? ""}</pre>}
          {preview.data.is_current === false && <p>这是历史版本的引用，可能落后于当前索引。</p>}
          {preview.data.partial && preview.data.next_cursor && <button type="button" onClick={() => setCursor(preview.data!.next_cursor)}>继续读取</button>}
        </> : null}
  </section>;
}

export function ConversationDetail({ conversationId }: { conversationId: string }) {
  const router = useRouter();
  const client = useQueryClient();
  const { session } = useSession();
  const userId = session?.user.id ?? "anonymous";
  const conversations = useConversations(userId);
  const summary = conversations.data?.conversations.find(item => item.id === conversationId);
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const pages = useMessagePages(userId, conversationId, cursors);
  const overviews = useKnowledgeOverviews(userId, summary?.selected_repository_ids ?? []);
  const [modes, setModes] = useState<Record<string, IndexMode>>({});
  const [question, setQuestion] = useState("");
  const [receipt, setReceipt] = useState<RunReceipt | null>(null);
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<unknown>(null);
  const [deleting, setDeleting] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [citation, setCitation] = useState<{ messageId: string; citation: CitationView } | null>(null);
  const logical = useRef<{ signature: string; key: string } | null>(null);
  const deleteKey = useRef<string | null>(null);

  const messages = pages.flatMap(page => page.data?.messages ?? []);
  const nextCursor = pages.at(-1)?.data?.next_cursor ?? null;
  const pageError = pages.find(page => page.error)?.error ?? conversations.error;
  const revokedNow = revoked(pageError);
  useEffect(() => {
    if (!revokedNow) return;
    void client.cancelQueries({ queryKey: chatKey(userId) });
    client.removeQueries({ queryKey: chatKey(userId) });
  }, [revokedNow, client, userId]);

  const indexes: IndexSelection[] = [];
  const scopeRows = (summary?.selected_repository_ids ?? []).map((id, index) => {
    const knowledge = overviews[index]?.data;
    const chosen = modes[id];
    const mode: IndexMode | null = chosen ?? (
      knowledge?.index_status === "ready" && knowledge.target_sha ? "target"
        : knowledge?.active_index_sha ? "active" : knowledge ? "source" : null
    );
    const sha = mode === "target" ? knowledge?.target_sha ?? null : mode === "active" ? knowledge?.active_index_sha ?? null : null;
    if (knowledge && (mode === "target" || mode === "active") && sha) {
      indexes.push({ repository_id: id, sha, target: mode });
    }
    return { id, knowledge, mode };
  });
  const allLoaded = (summary?.selected_repository_ids.length ?? 0) > 0
    && scopeRows.every(row => !!row.knowledge);
  const questionBytes = new TextEncoder().encode(question).length;
  const selectionError = !summary ? "正在确认会话…"
    : !allLoaded ? "正在确认所选仓库的索引状态…"
      : question.trim() === "" ? "请输入问题。"
        : questionBytes > 2048 ? "问题超过 2048 字节，请缩短后再提交。"
          : null;
  const canSubmit = !selectionError && !pending;

  const refreshHistory = useCallback(() => {
    void client.invalidateQueries({ queryKey: [...chatKey(userId), "messages", conversationId] });
    void client.invalidateQueries({ queryKey: [...chatKey(userId), "conversations"] });
  }, [client, userId, conversationId]);

  const settled = useCallback(() => {
    // A terminal Job must not keep the old receipt as an "active" run.
    setReceipt(null);
    refreshHistory();
  }, [refreshHistory]);

  const activeJob = receipt?.job_id ?? summary?.active_run?.job_id ?? null;
  const latestRun = summary?.active_run ?? summary?.latest_run ?? null;
  const showGap = !activeJob && latestRun?.status === "succeeded" && messages[0]?.role === "user";

  async function submit() {
    if (!canSubmit || !summary) return;
    const selection: QuerySelection = {
      selected_repository_ids: summary.selected_repository_ids,
      indexes,
    };
    const signature = JSON.stringify({ conversationId, indexes, question });
    if (logical.current?.signature !== signature) {
      logical.current = { signature, key: crypto.randomUUID() };
    }
    setPending(true);
    setFailure(null);
    setCitation(null);
    try {
      const accepted = await submitMessage(conversationId, selection, question, logical.current.key);
      logical.current = null;
      setReceipt(accepted);
      setQuestion("");
      refreshHistory();
    } catch (error) {
      if (!(error instanceof DOMException && error.name === "AbortError")) setFailure(error);
    } finally {
      setPending(false);
    }
  }

  async function cancel() {
    if (!activeJob) return;
    setFailure(null);
    try {
      await cancelJob(activeJob, crypto.randomUUID());
      setReceipt(null);
      refreshHistory();
    } catch (error) {
      setFailure(error);
    }
  }

  async function remove() {
    if (!confirmDelete) {
      setConfirmDelete(true);
      return;
    }
    deleteKey.current ??= crypto.randomUUID();
    setDeleting(true);
    setFailure(null);
    try {
      await deleteConversation(conversationId, deleteKey.current);
      await client.invalidateQueries({ queryKey: [...chatKey(userId), "conversations"] });
      client.removeQueries({ queryKey: [...chatKey(userId), "messages", conversationId] });
      router.push("/conversations");
    } catch (error) {
      setFailure(error);
    } finally {
      setDeleting(false);
    }
  }

  if (conversations.isSuccess && !summary) {
    return <section aria-label="会话不可用">
      <h2>会话不可用</h2>
      <p role="alert" className="banner error">该会话不存在、已删除，或你的仓库权限已被撤销。</p>
      <Link href="/conversations">返回会话列表</Link>
    </section>;
  }

  return <section aria-label="会话">
    <p><Link href="/conversations">← 返回会话列表</Link></p>
    <h2>私有会话</h2>
    <p>会话仅自己可见；回答会附带可核验的引用。当前问题会发送至阿里云（仅在云模型已配置时），历史消息、附件和 CI 日志不会自动回传。</p>
    {revokedNow && <p role="alert" className="banner error">仓库权限已撤销，历史、引用、进度和答案均已停止加载。</p>}

    <section className="panel" aria-label="会话范围">
      <h3>仓库范围与版本</h3>
      <p>范围在创建会话时固定，本轮使用的索引 SHA 单独固定。最多 3 个仓库。</p>
      {scopeRows.map(row => <div key={row.id}>
        <h4>{row.id.slice(0, 8)}</h4>
        {!row.knowledge ? <p role="status">正在确认索引状态…</p> : <>
          <dl className="fact-fields">
            <dt>目标 SHA</dt><dd>{row.knowledge.target_sha ?? "未固定 Workspace"}</dd>
            <dt>旧 active SHA</dt><dd>{row.knowledge.active_index_sha ?? "无"}</dd>
          </dl>
          <div className="member-form">
            <label><input type="radio" name={`chat-index-${row.id}`} checked={row.mode === "target"} disabled={row.knowledge.index_status !== "ready" || !row.knowledge.target_sha} onChange={() => setModes(current => ({ ...current, [row.id]: "target" }))} /> 目标版本</label>
            <label><input type="radio" name={`chat-index-${row.id}`} checked={row.mode === "active"} disabled={!row.knowledge.active_index_sha} onChange={() => setModes(current => ({ ...current, [row.id]: "active" }))} /> 旧 active 版本</label>
            <label><input type="radio" name={`chat-index-${row.id}`} checked={row.mode === "source"} onChange={() => setModes(current => ({ ...current, [row.id]: "source" }))} /> 仅作为来源</label>
          </div>
        </>}
      </div>)}
    </section>

    <section className="panel" aria-label="历史消息">
      <h3>历史消息</h3>
      {nextCursor && <button type="button" onClick={() => setNextPage()}>加载更早消息</button>}
      {pages.some(page => page.isPending) ? <p role="status">正在读取历史…</p> : null}
      {messages.length === 0 ? <p>还没有消息。</p> : <ol className="fact-list" reversed>{[...messages].reverse().map(message => <li key={message.id}>
        <p><strong>{message.role === "assistant" ? "助手" : "我"}</strong> · <time dateTime={message.created_at}>{new Date(message.created_at).toLocaleString("zh-CN")}</time></p>
        <pre className="code-text">{message.body}</pre>
        {message.citations.length > 0 && <div>
          <p>引用（本轮实际收到的证据）</p>
          <ul className="fact-list">{message.citations.map(entry => <li key={entry.citation_id}>
            <button type="button" onClick={() => setCitation({ messageId: message.id, citation: entry })}>
              {entry.path ?? "上传文档"} · 第 {entry.start_line}–{entry.end_line} 行
            </button>
            {entry.sha && <span> · SHA {entry.sha.slice(0, 12)}</span>}
          </li>)}</ul>
        </div>}
      </li>)}</ol>}
    </section>

    <section className="panel" aria-label="提问">
      <h3>提问</h3>
      <label>问题<textarea rows={3} value={question} onChange={event => setQuestion(event.target.value)} /></label>
      <p>{questionBytes} / 2048 字节。只会发送当前问题与本轮检索到的证据。</p>
      <button type="button" disabled={!canSubmit} onClick={() => void submit()}>{pending ? "正在提交…" : "提交问题"}</button>
      {selectionError && <p role="status">{selectionError}</p>}
      {!!failure && <p role="alert" className="banner error">{failureText(failure)}</p>}
    </section>

    {activeJob && <RunProgress key={activeJob} jobId={activeJob} onTerminal={settled} />}
    {activeJob && <p><button type="button" onClick={() => void cancel()}>取消本次回答</button></p>}
    {showGap && <p role="status" className="banner">本轮没有找到可用证据，未发送云请求；可以调整仓库范围后重新提问。</p>}

    {citation && <CitationPanel conversationId={conversationId} messageId={citation.messageId} citation={citation.citation} />}

    <section className="panel" aria-label="删除会话">
      <h3>删除会话</h3>
      <p>删除会立即清除消息正文并回收检查点；费用与请求身份按策略保留，未确认的预占不会释放。</p>
      <button type="button" disabled={deleting} onClick={() => void remove()}>
        {deleting ? "正在删除…" : confirmDelete ? "确认删除会话" : "删除会话"}
      </button>
    </section>
  </section>;

  function setNextPage() {
    if (!nextCursor || cursors.includes(nextCursor)) return;
    setCursors([...cursors, nextCursor]);
  }
}
