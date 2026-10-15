"use client";

import Link from "next/link";
import { useState } from "react";
import { ApiError } from "@/lib/api";
import { errorMessage, Pagination } from "@/features/admin/common";
import { useRepository, useRepositoryQuery } from "./queries";
import type { CollectionKind, CollectionView, Coverage, FactDetail, FactPage, FactView, Stream } from "./types";

export const shown = (value: string | number | null | undefined) => value ?? "未知";
const coverageLabels: Record<Coverage["status"], string> = { not_synced: "尚未同步", pending: "等待采集", running: "正在采集", complete: "覆盖完整", partial: "部分覆盖", failed: "采集失败", permission_missing: "源权限不足" };
export function CoverageEvidence({ coverage }: { coverage: Coverage }) {
  return <p className="banner">{coverage.stream}：{coverageLabels[coverage.status]} · 区间：{shown(coverage.start)} — {shown(coverage.end)} · 完成水位：{shown(coverage.completed_watermark)} · 最近成功：{shown(coverage.last_success_at)}</p>;
}
export function FactError({ error, retry, restart }: { error: unknown; retry: () => void; restart?: () => void }) {
  const missing = error instanceof ApiError && error.code === "fact_not_found";
  const cursor = error instanceof ApiError && error.code === "invalid_cursor";
  return <><p role="alert" className="banner error">{missing ? "事实不存在或尚未采集。" : cursor ? "分页版本已变化，请从第一页重新加载。" : errorMessage(error)}</p>{!missing && <button onClick={cursor && restart ? restart : retry}>{cursor ? "返回第一页" : "重新加载"}</button>}</>;
}
function CollectionEvidence({ value, empty }: { value: CollectionView | null; empty: boolean }) {
  return <p>{value?.complete ? empty ? "已完整采集，暂无记录" : "已完整采集" : value?.reason === "not_collected" || !value ? "尚未采集，记录数量未知" : `部分采集（${value.reason ?? "未知"}），不能视为完整记录`} · 观察时间：{shown(value?.observed_at)}</p>;
}
export function FactRecord({ item, heading = false }: { item: FactView; heading?: boolean }) {
  const payload = item.payload;
  const title = item.kind === "issue" || item.kind === "pull" ? `#${item.number} ${item.payload.title}` : item.kind === "ci_run" ? `${item.payload.name ?? "工作流"} · ${item.source_id} / ${item.payload.attempt}` : item.kind === "pr_file" ? item.payload.filename : item.kind === "ci_job" ? item.payload.name : `GitHub ID：${item.source_id}`;
  return <article className="panel fact-record">
    {heading ? <h2>{title}</h2> : <h3>{title}</h3>}
    <p>GitHub ID：{item.source_id}</p><p>本地事实 ID：{item.id} · 版本：{item.version_key || "当前"}</p>
    <p>源更新时间：{shown(item.source_updated_at)} · 观察时间：{item.observed_at}{item.unavailable ? " · 源记录已不可用" : ""}</p>
    {item.source_url && <a href={item.source_url} target="_blank" rel="noreferrer">在 GitHub 查看</a>}
    {"body" in payload && <><p>正文以纯文本显示，Markdown 暂不排版。</p><div className="fact-text">{payload.body ?? "正文未知"}</div><p>作者：{payload.author?.login ?? payload.author?.id ?? "未知"} · 创建时间：{shown(payload.created_at)}</p></>}
    {"normalization_reasons" in payload && payload.normalization_reasons.length > 0 && <p className="banner">字段规范化说明：{payload.normalization_reasons.join("、")}</p>}
    {(item.kind === "issue" || item.kind === "pull") && <><p>状态：{item.payload.state} · 标签：{item.payload.labels.join("、") || "无"} · 负责人：{item.payload.assignees.map(user => user.login ?? user.id).join("、") || "无"} · 关闭时间：{shown(item.payload.closed_at)}</p></>}
    {item.kind === "pull" && <><p>关联 Issue ID：{item.payload.issue_id} · 草稿：{item.payload.draft ? "是" : "否"} · 已合并：{item.payload.merged ? "是" : "否"}</p><dl className="fact-fields"><dt>Base 仓库 / Ref</dt><dd>{shown(item.payload.base_repository_id)} / {shown(item.payload.base_ref)}</dd><dt>Base SHA</dt><dd>{item.payload.base_sha}</dd><dt>Head 仓库 / Ref</dt><dd>{shown(item.payload.head_repository_id)} / {shown(item.payload.head_ref)}</dd><dt>Head SHA</dt><dd>{item.payload.head_sha}</dd><dt>合并 SHA / 时间</dt><dd>{shown(item.payload.merge_commit_sha)} / {shown(item.payload.merged_at)}</dd></dl></>}
    {item.kind === "review" && <p>评审状态：{item.payload.state} · Commit：{shown(item.payload.commit_id)} · 提交时间：{shown(item.payload.submitted_at)}（保留原提交，不代表当前 Head）</p>}
    {item.kind === "review_comment" && <dl className="fact-fields">{Object.entries({ "Review ID": item.payload.review_id, Commit: item.payload.commit_id, "原 Commit": item.payload.original_commit_id, 路径: item.payload.path, Side: item.payload.side, 行: item.payload.line, 原行: item.payload.original_line, 起始行: item.payload.start_line, 原起始行: item.payload.original_start_line, "回复 ID": item.payload.in_reply_to_id }).map(([name, value]) => <div key={name}><dt>{name}</dt><dd>{shown(value)}</dd></div>)}</dl>}
    {item.kind === "pr_file" && <><p>状态：{item.payload.status} · 原路径：{shown(item.payload.previous_filename)} · +{item.payload.additions} / -{item.payload.deletions} · 变更：{item.payload.changes}</p><dl className="fact-fields"><dt>Base SHA</dt><dd>{item.payload.base_sha}</dd><dt>Head SHA</dt><dd>{item.payload.head_sha}</dd><dt>文件 SHA</dt><dd>{shown(item.payload.sha)}</dd></dl><p>补丁：{({ available: "可用", missing: "缺失", truncated: "已截断，不完整" })[item.payload.patch_status]}</p>{item.payload.patch !== null && <pre className="fact-text">{item.payload.patch}</pre>}</>}
    {(item.kind === "ci_run" || item.kind === "ci_job") && <><dl className="fact-fields"><dt>状态</dt><dd>{item.payload.status}</dd><dt>结论</dt><dd>{shown(item.payload.conclusion)}</dd><dt>Head SHA</dt><dd>{item.payload.head_sha}</dd></dl>{item.kind === "ci_run" ? <p>Workflow ID：{item.payload.workflow_id} · 路径：{shown(item.payload.workflow_path)} · 事件：{item.payload.event} · 分支：{shown(item.payload.branch)} · 创建：{item.payload.created_at} · 更新：{item.payload.updated_at} · 开始：{shown(item.payload.run_started_at)}</p> : <><p>Run ID：{item.payload.run_id} · Attempt：{item.payload.attempt} · 开始：{shown(item.payload.started_at)} · 完成：{shown(item.payload.completed_at)}</p><ol aria-label="任务步骤">{item.payload.steps.map(step => <li key={step.number}>{step.number}. {step.name} · {step.status} / {shown(step.conclusion)} · {shown(step.started_at)} — {shown(step.completed_at)}</li>)}</ol></>}</>}
  </article>;
}
export function FactList({ resource, endpoint, title }: { resource: Stream; endpoint: string; title: string }) {
  const [state, setState] = useState(""), [sha, setSha] = useState(""), [branch, setBranch] = useState("");
  const filters = { ...(state ? { state } : {}), ...(sha ? { head_sha: sha } : {}), ...(branch ? { branch } : {}) };
  return <section aria-label={title}><h2>{title}</h2><form className="member-form" onSubmit={event => event.preventDefault()}>
    {resource !== "actions" && <label>状态筛选<select value={state} onChange={event => setState(event.target.value)}><option value="">全部</option><option value="open">open</option><option value="closed">closed</option><option value="unknown">unknown</option></select></label>}
    {resource !== "issues" && <label>Head SHA（完整 40 位）<input value={sha} onChange={event => setSha(event.target.value)} maxLength={40} pattern="[0-9a-f]{40}" /></label>}
    {resource === "actions" && <label>分支筛选<input value={branch} onChange={event => setBranch(event.target.value)} maxLength={255} /></label>}
  </form>{sha && !/^[0-9a-f]{40}$/.test(sha) ? <p role="alert" className="banner error">请输入完整的小写 40 位 SHA。</p> : <FactListPage key={JSON.stringify(filters)} resource={resource} endpoint={endpoint} filters={filters} />}</section>;
}
function FactListPage({ resource, endpoint, filters }: { resource: Stream; endpoint: string; filters: Record<string, string> }) {
  const scope = useRepository(); const [cursors, setCursors] = useState<string[]>([]);
  const cursor = cursors.at(-1);
  const query = useRepositoryQuery<FactPage>([resource, "list", filters, cursor ?? ""], endpoint, { limit: "30", ...filters, ...(cursor ? { cursor } : {}) });
  return <>{query.isPending ? <p role="status">正在加载事实…</p> : query.isError ? <FactError error={query.error} retry={() => void query.refetch()} restart={() => setCursors([])} /> : <>
    <CoverageEvidence coverage={query.data.coverage} />
    {!query.data.items.length && <p>{query.data.coverage.status === "complete" ? "当前已采集事实中暂无匹配记录" : "暂无已采集记录，远端数量未知"}</p>}
    <ul className="fact-list">{query.data.items.map(item => <li className="panel" key={item.id}><Link href={`/repositories/${scope.repository.id}/${resource}/${item.kind === "ci_run" ? `${item.source_id}/attempts/${item.payload.attempt}` : item.number}`}>{item.kind === "ci_run" ? `${item.payload.name ?? "工作流"} · ${item.source_id} / ${item.payload.attempt}` : item.kind === "issue" || item.kind === "pull" ? `#${item.number} ${item.payload.title}` : item.source_id}</Link><p>GitHub ID：{item.source_id} · 观察：{item.observed_at}</p></li>)}</ul>
  </>}<Pagination page={cursors.length + 1} busy={query.isFetching} hasNext={query.isSuccess && !!query.data.next_cursor} previous={() => setCursors(cursors.slice(0, -1))} next={() => { if (query.data?.next_cursor) setCursors([...cursors, query.data.next_cursor]); }} /></>;
}
const names: Record<CollectionKind, string> = { comments: "评论", reviews: "评审", review_comments: "行内评论", files: "文件与补丁", jobs: "任务", attempts: "尝试历史" };
function FactCollection({ endpoint, resource, identity, kind }: { endpoint: string; resource: Stream; identity: string; kind: CollectionKind }) {
  const scope = useRepository(); const [cursors, setCursors] = useState<string[]>([]); const cursor = cursors.at(-1);
  const query = useRepositoryQuery<FactPage>([resource, identity, kind, cursor ?? ""], endpoint, { collection: kind, limit: "30", ...(cursor ? { cursor } : {}) });
  return <section className="fact-collection" aria-label={names[kind]}><h3>{names[kind]}</h3>{query.isPending ? <p role="status">正在加载{names[kind]}…</p> : query.isError ? <FactError error={query.error} retry={() => void query.refetch()} restart={() => setCursors([])} /> : <><CollectionEvidence value={query.data.collection} empty={!query.data.items.length} />{query.data.items.map(item => <div key={item.id}>{item.kind === "ci_run" && <Link href={`/repositories/${scope.repository.id}/actions/${item.source_id}/attempts/${item.payload.attempt}`}>打开 Attempt {item.payload.attempt}</Link>}<FactRecord item={item} /></div>)}</>}
    <Pagination page={cursors.length + 1} busy={query.isFetching} hasNext={query.isSuccess && !!query.data.next_cursor} previous={() => setCursors(cursors.slice(0, -1))} next={() => { if (query.data?.next_cursor) setCursors([...cursors, query.data.next_cursor]); }} />
  </section>;
}
export function FactDetails({ resource, endpoint, identity, collections }: { resource: Stream; endpoint: string; identity: string; collections: CollectionKind[] }) {
  const query = useRepositoryQuery<FactDetail>([resource, "detail", identity], endpoint);
  const item = query.error instanceof ApiError && query.error.code === "fact_not_found" ? undefined : query.data?.item;
  const version = item ? `${identity}:${item.version_key}:${item.observed_at}:${item.kind === "pull" ? `${item.payload.base_sha}:${item.payload.head_sha}` : ""}` : identity;
  return <section aria-label="事实详情"><button disabled={query.isFetching} onClick={() => void query.refetch()}>刷新事实</button>
    {query.isPending ? <p role="status">正在加载事实…</p> : <>{query.isError && <FactError error={query.error} retry={() => void query.refetch()} />}{item && <><CoverageEvidence coverage={query.data!.coverage} /><FactRecord item={item} heading />{collections.map(kind => <FactCollection key={`${version}:${kind}`} identity={version} endpoint={endpoint} resource={resource} kind={kind} />)}</>}</>}
  </section>;
}
export function Issues({ number }: { number?: string }) {
  return number ? <FactDetails resource="issues" endpoint={`/issues/${encodeURIComponent(number)}`} identity={number} collections={["comments"]} /> : <FactList resource="issues" endpoint="/issues" title="Issues" />;
}
