"use client";

import { useSession } from "@/features/session/use-session";

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  const { session } = useSession();
  return session?.user.role === "admin" ? children : <main className="message-card" role="alert"><h1>无权访问管理页面</h1><p>该页面仅供管理员使用。</p></main>;
}
