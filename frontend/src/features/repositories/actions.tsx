"use client";

import { FactDetails, FactList } from "./issues";
export function Actions({ runId, attempt }: { runId?: string; attempt?: string }) {
  return runId && attempt ? <FactDetails resource="actions" endpoint={`/actions/runs/${encodeURIComponent(runId)}/attempts/${encodeURIComponent(attempt)}`} identity={`${runId}:${attempt}`} collections={["jobs", "attempts"]} /> : <FactList resource="actions" endpoint="/actions/runs" title="Actions" />;
}
