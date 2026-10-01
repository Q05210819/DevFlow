import { ProtectedShell } from "@/features/session/shell";

export default function Page() {
  return <ProtectedShell><main className="content"><span className="eyebrow">工作台 / 概览</span><h1>欢迎使用 DevFlow</h1><p>从仓库页面提交只读访问检查，查看检查进度与结果。</p></main></ProtectedShell>;
}
