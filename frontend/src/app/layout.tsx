import type { Metadata } from "next";
import { SessionProvider } from "@/features/session/provider";
import "./globals.css";

export const metadata: Metadata = { title: "DevFlow", description: "团队仓库检查" };

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-CN"><body><SessionProvider>{children}</SessionProvider></body></html>;
}
