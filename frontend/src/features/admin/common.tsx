import { ApiError } from "@/lib/api";

export function errorMessage(error: unknown) {
  if (error instanceof ApiError) {
    if (error.code === "last_admin") return "必须保留至少一位已启用的管理员，请先添加或启用其他管理员。";
    if (error.status === 401) return "登录已过期，请刷新页面重新登录。";
    if (error.status === 403) return "无权执行此操作，请确认管理员权限。";
    if (error.status === 409) return "数据发生冲突，请刷新后重试。";
    if (error.status === 400 || error.status === 422) return "输入无效，请检查后重试。";
  }
  return "服务暂时不可用，请稍后重试。";
}

export function Pagination({ page, hasNext, busy, previous, next }: {
  page: number; hasNext: boolean; busy: boolean; previous: () => void; next: () => void;
}) {
  return <nav className="pagination" aria-label="列表分页">
    <button type="button" disabled={page === 1 || busy} onClick={previous}>上一页</button>
    <span aria-live="polite">第 {page} 页</span>
    <button type="button" disabled={!hasNext || busy} onClick={next}>下一页</button>
  </nav>;
}
