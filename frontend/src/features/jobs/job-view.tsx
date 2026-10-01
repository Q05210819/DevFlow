"use client";

import Link from "next/link";
import { isTerminal, statusLabels, useJobEvents } from "./use-job-events";

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
      <dl><dt>检查编号</dt><dd>{job.id}</dd><dt>尝试次数</dt><dd>{events.at(-1)?.attempt ?? job.attempt} / {job.max_attempts}</dd><dt>提交时间</dt><dd><time dateTime={job.created_at}>{new Date(job.created_at).toLocaleString("zh-CN")}</time></dd></dl>
      {job.status === "failed" && <p className="banner error" role="alert">检查未完成。请确认仓库权限和 GitHub 服务状态后，从仓库页面再次检查。</p>}
      {job.status === "succeeded" && job.result && <div className="probe-result"><h3>检查结果</h3><dl><dt>仓库</dt><dd>{job.result.full_name}</dd><dt>可见性</dt><dd>{job.result.private ? "私有仓库" : "公开仓库"}</dd><dt>默认分支</dt><dd>{job.result.default_branch ?? "未设置"}</dd><dt>检查时间</dt><dd><time dateTime={job.result.checked_at}>{new Date(job.result.checked_at).toLocaleString("zh-CN")}</time></dd></dl></div>}
    </section>}
    {!!events.length && <section className="job-progress"><h2>检查进度</h2><ol aria-label="检查进度">{events.map(event => <li key={event.sequence}><span className="sequence">{event.sequence}</span> {statusLabels[event.status]} · 第 {event.attempt} 次尝试</li>)}</ol></section>}
    <Link className="back-link" href="/repositories">返回仓库</Link>
  </main>;
}
