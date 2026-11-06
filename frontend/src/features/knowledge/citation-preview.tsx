"use client";

import { useState } from "react";
import { errorMessage } from "@/features/admin/common";
import { useCitationPreview, type QueryRun } from "./queries";

export function CitationPreview({ userId, run, citationId }: {
  userId: string;
  run: QueryRun;
  citationId: string;
}) {
  const [cursors, setCursors] = useState<string[]>([]);
  const preview = useCitationPreview(userId, run, citationId, cursors.at(-1));
  const data = preview.data;
  return <section className="panel fact-collection" aria-label="引用预览">
    <h3>引用预览</h3>
    <p>引用正文只由服务端按引用身份读取；不会抓取或执行来源 URL，也不会渲染 HTML。</p>
    {preview.isPending ? <p role="status">正在读取引用片段…</p> : preview.isError ? <p role="alert" className="banner error">{errorMessage(preview.error)}</p> : data && <>
      <p role="status">
        {data.reason === "source_expired" ? "正文已回收或过期，引用身份仍保留。"
          : data.reason === "source_unavailable" ? "来源当前不可用。"
          : data.is_current ? "当前语料修订" : "历史修订（结果生成时的版本）；当前语料可能已更新。"}
        {data.partial && data.location ? ` · 仅显示返回区间（第 ${data.location.start_line}–${data.location.end_line} 行）` : ""}
      </p>
      <dl className="fact-fields">
        <dt>来源路径</dt><dd>{data.identity.path ?? "（上传文档）"}</dd>
        <dt>固定 SHA</dt><dd>{data.identity.sha ?? "上传文档无 Git SHA"}</dd>
      </dl>
      {data.text !== null && data.text !== "" && <pre className="code-text">{data.text}</pre>}
      {data.text === "" && <p>此范围为空白文本。</p>}
      {data.text === null && <p>此范围没有可预览的正文。</p>}
      {data.next_cursor && <button type="button" onClick={() => setCursors([...cursors, data.next_cursor!])}>继续读取后续内容</button>}
      {cursors.length > 0 && <button type="button" onClick={() => setCursors(cursors.slice(0, -1))}>返回上一段</button>}
    </>}
  </section>;
}
