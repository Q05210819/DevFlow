"use client";

import Link from "next/link";
import { useState } from "react";
import { errorMessage } from "@/features/admin/common";
import { useSession } from "@/features/session/use-session";
import { CoverageEvidence, FactError, shown } from "./issues";
import { pendingStatus, useRepository, useRepositoryCommand, useRepositoryQuery } from "./queries";
import { selectionLabel } from "./workspace";
import type { HealthSnapshot, HealthView, Stream, SyncView } from "./types";

export const syncLabels: Record<SyncView["status"], string> = { idle: "尚未同步", queued: "等待同步", running: "正在同步", retry_wait: "等待重试", completed: "本轮已完成", failed: "同步失败", budget_exhausted: "同步预算已用尽" };
function SyncPanel() {
  const { sync } = useRepository(); const { session } = useSession();
  const refresh = useRepositoryCommand<SyncView>("sync/refresh", "sync");
  const resume = useRepositoryCommand<SyncView>("sync/refresh", "sync");
  return <section className="panel" aria-label="同步状态"><h2>同步状态</h2><p role="status">{syncLabels[sync.status]} · 第 {sync.generation} 代 · 已处理 {sync.processed_sequence} / 请求 {sync.requested_sequence}</p>
    <p>本轮完成不代表所有流覆盖完整。重试时间：{shown(sync.retry_at)} · 周期调度时间：{shown(sync.next_due_at)}</p>
    {sync.error_code && <p className="banner">同步存在未完成证据（{sync.error_code}）。请查看各流覆盖范围；预算用尽时联系管理员恢复。</p>}
    <div className="repository-controls"><button disabled={refresh.pending} onClick={() => void refresh.submit({ resume: false })}>{refresh.pending ? "正在提交…" : refresh.error ? "重试同步提交" : "刷新同步"}</button>
    {session?.user.role === "admin" && <button disabled={resume.pending} onClick={() => void resume.submit({ resume: true })}>恢复同步</button>}</div>
    {(!!refresh.error || !!resume.error) && <p role="alert" className="banner error">{errorMessage(refresh.error ?? resume.error)}</p>}
    {sync.streams.map(stream => <div className="stream-evidence" key={stream.stream}><CoverageEvidence coverage={{ ...stream.coverage, stream: stream.stream, status: stream.status, last_success_at: stream.last_success_at, error_code: stream.error_code }} />{stream.remaining ? <p>已知剩余：目标 {stream.remaining.pending_targets} · 窗口 {stream.remaining.pending_windows} · 未解决窗口 {stream.remaining.unresolved_windows} · 当前目标：{stream.remaining.current_target ? "有" : "无"} · 枚举完成：{stream.remaining.enumeration_complete ? "是" : "未知"} · {stream.remaining.mode} / {stream.remaining.stage}{stream.remaining.reason ? ` / ${stream.remaining.reason}` : ""}（不是远端总量）</p> : <p>剩余工作未知</p>}</div>)}
  </section>;
}
function Backfill() {
  const command = useRepositoryCommand<SyncView>("sync/backfills", "sync");
  const [start, setStart] = useState(""), [end, setEnd] = useState("");
  const [streams, setStreams] = useState<Stream[]>(["issues", "pulls", "actions"]);
  const [invalid, setInvalid] = useState(false);
  function submit(event: React.FormEvent) {
    event.preventDefault();
    const since = new Date(`${start}Z`), until = new Date(`${end}Z`);
    if (!Number.isFinite(since.getTime()) || !Number.isFinite(until.getTime()) || since > until || until.getTime() - since.getTime() > 365 * 86_400_000 || until.getTime() > Date.now() || !streams.length) { setInvalid(true); return; }
    setInvalid(false);
    void command.submit({ start: since.toISOString(), end: until.toISOString(), streams: ["issues", "pulls", "actions"].filter(stream => streams.includes(stream as Stream)) });
  }
  return <section className="panel" aria-label="管理员回填"><h2>管理员回填</h2><p>所有输入均为 UTC。开始不得晚于结束，最多 365 天，结束不得在未来。</p>
    <form onSubmit={submit}><div className="member-form"><label>开始时间（UTC）<input type="datetime-local" required value={start} onChange={event => setStart(event.target.value)} disabled={command.pending} /></label><label>结束时间（UTC）<input type="datetime-local" required value={end} onChange={event => setEnd(event.target.value)} disabled={command.pending} /></label></div>
    <fieldset disabled={command.pending}><legend>采集流（至少一项）</legend>{(["issues", "pulls", "actions"] as Stream[]).map(stream => <label className="stream-choice" key={stream}><input type="checkbox" checked={streams.includes(stream)} onChange={event => setStreams(event.target.checked ? [...streams, stream] : streams.filter(value => value !== stream))} />{stream}</label>)}</fieldset>
    <button type="submit" disabled={command.pending}>{command.pending ? "正在提交…" : command.error ? "重试回填提交" : "提交回填"}</button></form>
    {invalid && <p role="alert" className="banner error">请检查 UTC 时间顺序、365 天上限、未来时间和采集流。</p>}
    {!!command.error && <p role="alert" className="banner error">{errorMessage(command.error)}</p>}
  </section>;
}
function HealthEvidence({ snapshot }: { snapshot: HealthSnapshot }) {
  const { repository } = useRepository(); const root = `/repositories/${repository.id}`;
  const counts = snapshot.counts; const manifest = snapshot.manifest;
  return <><p>快照：{snapshot.id} · 第 {snapshot.generation} 代 · 规则：{snapshot.rule_version}</p><p>生成时间（UTC）：<time>{snapshot.generated_at}</time></p>
    <p>统计仅代表已观察并持久化的记录，不代表 GitHub 总量。缺少覆盖时为未知。过期阈值：{snapshot.thresholds.stale_days} 天。</p>
    <dl className="fact-fields">{([ ["open_issues", "Open Issues"], ["open_pulls", "Open PRs"], ["unassigned_issues", "未分配 Issues"], ["draft_pulls", "草稿 PRs"], ["stale_issues", "过期 Issues"], ["stale_pulls", "过期 PRs"] ] as const).map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{shown(counts[key])}</dd></div>)}</dl>
    {snapshot.coverage.map(value => <CoverageEvidence key={value.stream} coverage={value} />)}
    <h3>记录创建/更新活动（不是事件历史）</h3><ul className="fact-list">{counts.activity.map(window => <li key={`${window.stream}:${window.days}`}>{window.stream} · {window.days} 天 · 创建 {shown(window.created_records)} / 更新 {shown(window.updated_records)}<p>请求区间：{window.requested_start} — {window.requested_end}<br />实际区间：{shown(window.actual_start)} — {shown(window.actual_end)} · {window.complete ? "完整覆盖" : "不完整覆盖"}</p></li>)}</ul>
    <h3>已观察 Actions 分类</h3>{counts.actions_categories === null ? <p>未知</p> : Object.keys(counts.actions_categories).length === 0 ? <p>已观察总体为空</p> : <ul>{Object.entries(counts.actions_categories).map(([category, count]) => <li key={category}>{category}：{count}</li>)}</ul>}
    <p>该分类不是总体健康评分或 PR 合并许可。</p><h3>可追溯输入清单</h3><p>事实代次：{manifest.sync_generation} · 已处理 {manifest.processed_sequence} / 请求 {manifest.requested_sequence} · 冻结：{manifest.frozen_at}</p>
    <ul>{manifest.populations.map(value => <li key={value.kind}>{value.kind}：{value.count} · {shown(value.observed_from)} — {shown(value.observed_until)}</li>)}</ul>
    <p>PR 版本：{manifest.pulls_selection.returned_count} / {manifest.pulls_selection.total_candidates} · {manifest.pulls_selection.truncated ? "明细已截断，未列出的证据未知" : "明细未截断"} · 选择顺序：{manifest.pulls_selection.selection_order}</p>
    <ul className="fact-list">{manifest.pulls.map(pr => <li className="panel" key={pr.source_id}><Link href={`${root}/pulls/${pr.number}`}>PR #{pr.number}</Link><dl className="fact-fields"><dt>Base SHA</dt><dd>{pr.base_sha}</dd><dt>Head SHA</dt><dd>{pr.head_sha}</dd></dl><p>GitHub ID：{pr.source_id} · 观察：{pr.observed_at}</p></li>)}</ul>
    <p>CI 版本：{manifest.actions_selection.returned_count} / {manifest.actions_selection.total_candidates} · {manifest.actions_selection.truncated ? "明细已截断，未列出的证据未知" : "明细未截断"} · 选择顺序：{manifest.actions_selection.selection_order}</p>
    <ul className="fact-list">{manifest.actions.map(run => <li className="panel" key={`${run.source_id}:${run.attempt}`}><Link href={`${root}/actions/${run.source_id}/attempts/${run.attempt}`}>Run {run.source_id} / Attempt {run.attempt}</Link><p>{run.status} / {shown(run.conclusion)} · Workflow {run.workflow_id} · 分支 {shown(run.branch)}</p><p className="fact-text">Head SHA：{run.head_sha}</p><p>{run.default_branch_latest ? "默认分支最新创建的 Run" : "PR Head 证据"} · 精确匹配 PR：{run.pr_numbers.length ? run.pr_numbers.map(number => `#${number}`).join("、") : "无"} · 观察：{run.observed_at}</p></li>)}</ul>
    <p>未列出的证据：未知。其他 SHA 的成功不能证明当前 PR Head 成功。</p><WorkspaceHealth snapshot={snapshot} />
  </>;
}
function WorkspaceHealth({ snapshot }: { snapshot: HealthSnapshot }) {
  const { repository } = useRepository();
  const summary = snapshot.manifest.workspace_selection;
  const versions = snapshot.manifest.workspace_versions ?? [];
  const labels = { available: "可用", partial: "部分可用", unavailable: "不可用", unknown: "未知", unprepared: "未准备" };
  const reasons = { not_prepared: "尚未准备", version_unavailable: "版本不可用", scope_unavailable: "来源授权不可用", verification_limit: "验证预算不足或尚未验证", verification_unavailable: "暂时无法验证", version_changed: "验证期间版本变化" };
  return <section className="panel" aria-label="代码版本证据"><h3>代码版本证据：{labels[snapshot.workspace ?? "unknown"]}</h3>
    <p>证据冻结于 {snapshot.manifest.frozen_at}；仅检查本地 pin、commit 与根 tree 身份，不证明全部文件完整、当前远端版本或工作流实际 checkout。</p>
    {summary ? <><p>冻结可见候选 {summary.total_candidates} · 返回 {summary.returned_count} · {summary.truncated ? "代码证据已截断，未列出部分未知" : "代码证据明细未截断"}</p>
      <p>物理检查 {summary.checked_pins} · 已得结论的逻辑版本 {summary.checked_count} · 待验证 {summary.unverified_count}</p>
      <p>候选只来自本摘要可见的默认分支、PR 两侧和 CI；PR/CI 上游截断仍适用，不代表远端版本总量。一次最多检查 16 个不同物理 pin；同一 pin 可支持多个逻辑版本。已知未准备不计入待验证。</p>
    </> : <p>该历史摘要没有记录逐版本验证清单，检查数量未知。</p>}
    <ul className="fact-list">{versions.map((value, index) => <li key={index}><p>{selectionLabel(value.selection)} · {labels[value.availability]}{value.reason ? ` · ${reasons[value.reason]}` : ""}</p><dl className="fact-fields"><dt>固定 SHA</dt><dd>{shown(value.commit_sha)}</dd><dt>检查时间（UTC）</dt><dd>{shown(value.checked_at)}</dd></dl>
      {value.workspace_id && <Link href={`/repositories/${encodeURIComponent(repository.id)}/code?${new URLSearchParams({ workspace: value.workspace_id })}`}>重新确认该固定版本</Link>}
    </li>)}</ul>
  </section>;
}
function HealthPanel() {
  const query = useRepositoryQuery<HealthView>(["health"], "/health", {}, true);
  const command = useRepositoryCommand<HealthView>("health/refresh", "health");
  return <section className="panel" aria-label="健康事实"><h2>健康事实</h2><button disabled={command.pending} onClick={() => void command.submit({})}>{command.pending ? "正在提交…" : command.error ? "重试健康提交" : "刷新健康摘要"}</button>
    {!!command.error && <p role="alert" className="banner error">{errorMessage(command.error)}</p>}
    {query.isError && <FactError error={query.error} retry={() => void query.refetch()} />}
    {query.isPending ? <p role="status">正在加载健康事实…</p> : query.data && <><p role="status">刷新状态：{query.data.refresh_status} · 重试时间：{shown(query.data.retry_at)}</p>{query.data.refresh_status === "failed" && <p className="banner error" role="alert">刷新失败，保留上次成功快照与原生成时间。</p>}{pendingStatus(query.data.refresh_status) && <p>新摘要处理中，下方保留的快照是先前证据。</p>}{query.data.snapshot ? <HealthEvidence snapshot={query.data.snapshot} /> : <p>尚无健康快照，事实数量与代码版本证据未知。</p>}</>}
  </section>;
}
export function RepositoryOverview() {
  const { session } = useSession();
  return <div className="repository-sections"><SyncPanel /><HealthPanel />{session?.user.role === "admin" && <Backfill />}</div>;
}
