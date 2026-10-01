import { ProtectedShell } from "@/features/session/shell";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return <ProtectedShell adminOnly>{children}</ProtectedShell>;
}
