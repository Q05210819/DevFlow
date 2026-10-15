import { Actions } from "@/features/repositories/actions";

export default async function Page({ params }: { params: Promise<{ runId: string; attempt: string }> }) {
  const { runId, attempt } = await params;
  return <Actions key={`${runId}:${attempt}`} runId={runId} attempt={attempt} />;
}
