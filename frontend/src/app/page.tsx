import { ProtectedShell } from "@/features/session/shell";

export default function Page() {
  return <ProtectedShell><main className="content"><span className="eyebrow">工作台 / 概览</span><h1>欢迎使用 DevFlow</h1><p>从导航进入团队管理。仓库与检查页面将在后续阶段接入。</p></main></ProtectedShell>;
}
