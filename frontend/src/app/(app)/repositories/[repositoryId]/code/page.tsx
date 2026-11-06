import { Workspace } from "@/features/repositories/workspace";

export default async function Page({ searchParams }: { searchParams: Promise<{ workspace?: string | string[] }> }) {
  const params = await searchParams;
  const id = typeof params.workspace === "string" ? params.workspace : undefined;
  return <Workspace key={id ?? "selection"} workspaceId={id} />;
}
