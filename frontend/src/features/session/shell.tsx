"use client";

import Link from "next/link";
import { Fragment, useState } from "react";
import { useSession } from "./use-session";

export function LoginEntry() {
  return <main className="login-card" aria-labelledby="login-title">
    <span className="eyebrow">DEVFLOW · 内部工作台</span>
    <h1 id="login-title">登录 DevFlow</h1>
    <p>使用已获团队授权的 GitHub 帐号继续。</p>
    <a className="primary-link" href="/api/v1/auth/github/start">使用 GitHub 登录</a>
  </main>;
}

export function ProtectedShell({ children, adminOnly = false }: { children: React.ReactNode; adminOnly?: boolean }) {
  const { session, isLoading, error, logout } = useSession();
  const [logoutError, setLogoutError] = useState(false);
  if (isLoading) return <main className="message-card" role="status">正在确认会话…</main>;
  if (error) return <main className="message-card" role="alert">会话暂时不可用，请稍后刷新重试。</main>;
  if (!session) return <LoginEntry />;
  return <div className="app-shell">
    <header className="topbar">
      <Link className="brand" href="/">DEVFLOW</Link>
      <nav aria-label="主导航">
        <Link href="/">概览</Link>
        <Link href="/repositories">仓库</Link>
        <Link href="/knowledge">检索</Link>
        <Link href="/conversations">会话</Link>
        {session.user.role === "admin" && <>
          <Link href="/admin/users">成员管理</Link>
          <Link href="/admin/audit">审计日志</Link>
        </>}
      </nav>
      <div className="account">
        <span>当前用户：<strong>{session.user.login ?? session.user.github_id}</strong></span>
        <button type="button" className="text-button" onClick={() => { void logout().catch(() => setLogoutError(true)); }}>退出登录</button>
      </div>
    </header>
    {logoutError && <p className="banner error" role="alert">退出失败，请重试。</p>}
    {adminOnly && session.user.role !== "admin"
      ? <main className="message-card" role="alert"><h1>无权访问管理页面</h1><p>该页面仅供管理员使用。</p></main>
      : <Fragment key={session.user.id}>{children}</Fragment>}
  </div>;
}
