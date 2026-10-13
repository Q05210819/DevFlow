"use client";

import Link from "next/link";
import { isTerminal, statusLabels, useJobEvents } from "./use-job-events";

function failureMessage(code: string | null) {
  switch (code) {
    case "unauthenticated":
    case "reauth_required": return "请重新登录并授权 GitHub，然后再次检查。";
    case "resource_not_found":
    case "permission_revoked": return "仓库不可访问，请确认仓库权限或联系管理员。";
    case "github_rate_limited": return "GitHub 请求额度已用尽，请稍后再次检查。";
    case "github_unavailable": return "GitHub 服务暂时不可用，请稍后再次检查。";
    case "github_configuration_error": return "请联系管理员检查 GitHub App 配置，修复后再次检查。";
    case "attempt_timeout": return "本次检查超时，请稍后再次检查。";
    case "lease_expired": return "检查执行中断，请稍后再次检查；如持续发生，请联系管理员。";
    case "validation_error": return "检查参数无效，请返回仓库页面重新提交。";
    case "internal_error":
    default: return "检查未完成，请稍后重试或联系管理员。";
  }
}

export function JobView({ jobId }: { jobId: string }) {
  const { job, events, connection, recover } = useJobEvents(jobId, 0, false);
  if (connection === "denied") return <main className="content"><h1>检查不可访问</h1><p role="alert">登录已过期，或您已无权查看此检查。私有详情已清除。</p><Link href="/repositories">返回仓库</Link></main>;
  const status = job && (isTerminal(job.status) ? job.status : events.at(-1)?.status ?? job.status);
  return <main className="content">
    <span className="eyebrow">工作台 / 仓库检查</span><h1>访问检查</h1><p>确认仓库访问权限，进度与最终结果由服务端保存。</p>
    {connection === "loading" && <p role="status">正在加载检查…</p>}
    {connection === "retrying" && <p className="banner" role="status">连接暂时中断，正在恢复…</p>}
    {connection === "paused" && <div className="banner error"><p role="alert">服务暂时不可用，自动恢复已暂停。可以手动重试。</p><button onClick={recover}>恢复连接</button></div>}
    {job && <section className="panel job-details" aria-label="检查详情">
      <h2 aria-live="polite">{status && statusLabels[status]}</h2>
      <dl><dt>检查编号</dt><dd>{job.id}</dd><dt>尝试次数</dt><dd>{isTerminal(job.status) ? job.attempt : events.at(-1)?.attempt ?? job.attempt} / {job.max_attempts}</dd><dt>提交时间</dt><dd><time dateTime={job.created_at}>{new Date(job.created_at).toLocaleString("zh-CN")}</time></dd></dl>
      {job.status === "failed" && <p className="banner error" role="alert">{failureMessage(job.error_code)}</p>}
      {job.status === "succeeded" && job.result && <div className="probe-result"><h3>检查结果</h3><dl><dt>仓库</dt><dd>{job.result.full_name}</dd><dt>可见性</dt><dd>{job.result.private ? "私有仓库" : "公开仓库"}</dd><dt>默认分支</dt><dd>{job.result.default_branch ?? "未设置"}</dd><dt>检查时间</dt><dd><time dateTime={job.result.checked_at}>{new Date(job.result.checked_at).toLocaleString("zh-CN")}</time></dd></dl></div>}
    </section>}
    {!!events.length && <section className="job-progress"><h2>检查进度</h2><ol aria-label="检查进度">{events.map(event => <li key={event.sequence}><span className="sequence">{event.sequence}</span> {statusLabels[event.status]} · 第 {event.attempt} 次尝试</li>)}</ol></section>}
    <Link className="back-link" href="/repositories">返回仓库</Link>
  </main>;
}
