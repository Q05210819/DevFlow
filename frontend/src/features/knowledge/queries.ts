"use client";

import { useQueries, useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Page, RepositoryView } from "@/lib/types";
import { pendingStatus, useRepository, useRepositoryQuery } from "@/features/repositories/queries";
import type { SyncSummary } from "@/features/repositories/types";
import type {
  DocumentView, ImportReceipt, IndexSelection, KnowledgeView, PathPage, QueryReceipt,
  QuerySelection, QueryView, SourcePreview, StageReceipt,
} from "./types";

export const MAX_UPLOAD_BYTES = 1048576;

export function useKnowledge(cursor: string | undefined, poll: boolean) {
  return useRepositoryQuery<KnowledgeView>(["knowledge", cursor ?? ""], "/knowledge", cursor ? { cursor } : {}, poll);
}

export function useKnowledgeDocument(documentId: string | null, revisionId: string | null, cursor: string | null, revisionCursor: string | null) {
  const params: Record<string, string> = {};
  if (revisionId) params.revision_id = revisionId;
  if (cursor) params.cursor = cursor;
  if (revisionCursor) params.revision_cursor = revisionCursor;
  return useRepositoryQuery<DocumentView>(
    ["knowledge-document", documentId ?? "", revisionId ?? "", cursor ?? "", revisionCursor ?? ""],
    `/knowledge/documents/${encodeURIComponent(documentId ?? "")}`,
    params,
    false,
    documentId !== null,
  );
}

export function pathsParams(workspaceId: string, sha: string, documentPaths: string[], codePaths: string[], cursor?: string) {
  const search = new URLSearchParams({ workspace_id: workspaceId, sha, document_paths_explicit: "true" });
  for (const path of documentPaths) search.append("document_paths", path);
  for (const path of codePaths) search.append("code_paths", path);
  if (cursor) search.set("cursor", cursor);
  return search;
}

export function usePathPreview(workspaceId: string | null, sha: string | null, documentPaths: string[], codePaths: string[], cursor: string | undefined) {
  const search = workspaceId && sha ? pathsParams(workspaceId, sha, documentPaths, codePaths, cursor).toString() : "";
  return useRepositoryQuery<PathPage>(
    ["knowledge-paths", workspaceId ?? "", sha ?? "", search],
    `/knowledge/paths${search ? `?${search}` : ""}`,
    {},
    false,
    !!(workspaceId && sha && (documentPaths.length || codePaths.length)),
  );
}

// Independent retrieval is not bound to one repository scope. Every key carries
// the session actor so a different identity can never read the previous cache.
export const knowledgeSearchKey = (userId: string) => ["knowledge-search", userId] as const;

export type QueryRun = {
  queryId: string;
  jobId: string;
  anchorId: string;
  signature: string;
  question: string;
};

export function scopeSignature(selected: string[], indexes: IndexSelection[]) {
  const repositories = [...selected].sort();
  const ordered = [...indexes].sort((left, right) =>
    `${left.repository_id}:${left.sha}:${left.target}`.localeCompare(`${right.repository_id}:${right.sha}:${right.target}`));
  return JSON.stringify({ repositories, indexes: ordered });
}

export function useRepositoryCatalog(userId: string, limit: number, cursor: string | undefined) {
  return useQuery({
    queryKey: [...knowledgeSearchKey(userId), "repositories", limit, cursor ?? ""],
    queryFn: ({ signal }) => {
      const search = new URLSearchParams({ limit: String(limit) });
      if (cursor) search.set("cursor", cursor);
      return api<Page<RepositoryView & { sync: SyncSummary }>>(`/api/v1/repositories?${search.toString()}`, { signal });
    },
    retry: false, refetchOnWindowFocus: false, refetchOnReconnect: false,
  });
}

export function useKnowledgeOverviews(userId: string, repositoryIds: string[]) {
  return useQueries({
    queries: repositoryIds.map(id => ({
      queryKey: [...knowledgeSearchKey(userId), "overview", id],
      queryFn: ({ signal }: { signal: AbortSignal }) => api<KnowledgeView>(`/api/v1/repositories/${encodeURIComponent(id)}/knowledge`, { signal }),
      retry: false, refetchOnWindowFocus: false, refetchOnReconnect: false,
    })),
  });
}

export function submitQuery(anchorId: string, selection: QuerySelection, question: string, key: string) {
  return api<QueryReceipt>(`/api/v1/repositories/${encodeURIComponent(anchorId)}/knowledge/queries`, {
    method: "POST",
    body: JSON.stringify({ selection, question }),
    headers: { "Idempotency-Key": key },
  });
}

export function useQueryView(userId: string, run: QueryRun | null) {
  return useQuery({
    queryKey: [...knowledgeSearchKey(userId), "query", run?.signature ?? "", run?.queryId ?? ""],
    queryFn: ({ signal }) => api<QueryView>(
      `/api/v1/repositories/${encodeURIComponent(run!.anchorId)}/knowledge/queries/${encodeURIComponent(run!.queryId)}`,
      { signal },
    ),
    enabled: run !== null,
    retry: false, refetchOnWindowFocus: false, refetchOnReconnect: false,
    refetchInterval: query => pendingStatus(query.state.data?.status) ? 1000 : false,
    meta: { poll: true },
  });
}

export function useCitationPreview(userId: string, run: QueryRun | null, citationId: string | null, cursor: string | undefined) {
  return useQuery({
    queryKey: [...knowledgeSearchKey(userId), "citation", run?.queryId ?? "", citationId ?? "", cursor ?? ""],
    queryFn: ({ signal }) => {
      const search = cursor ? `?${new URLSearchParams({ cursor }).toString()}` : "";
      return api<SourcePreview>(
        `/api/v1/repositories/${encodeURIComponent(run!.anchorId)}/knowledge/queries/${encodeURIComponent(run!.queryId)}/citations/${encodeURIComponent(citationId!)}${search}`,
        { signal },
      );
    },
    enabled: run !== null && citationId !== null,
    // A citation preview is immutable for its query, citation and cursor.
    // Never refetch on mount: the App Router's StrictMode double-mount would
    // otherwise cancel and repeat the initial request.
    staleTime: Infinity, refetchOnMount: false,
    retry: false, refetchOnWindowFocus: false, refetchOnReconnect: false,
  });
}

export function useKnowledgeActions() {
  const scope = useRepository();
  const base = `${scope.path}/knowledge`;
  return {
    async stage(file: File, filename: string, key: string, documentId?: string): Promise<StageReceipt> {
      const query = new URLSearchParams({ filename });
      if (documentId) query.set("document_id", documentId);
      return scope.access.request<StageReceipt>(`${base}/uploads?${query.toString()}`, {
        method: "POST",
        body: await file.arrayBuffer(),
        headers: { "Content-Type": "application/octet-stream", "Idempotency-Key": key },
      });
    },
    admit(body: unknown, key: string): Promise<ImportReceipt> {
      return scope.access.request<ImportReceipt>(`${base}/imports`, {
        method: "POST",
        body: JSON.stringify(body),
        headers: { "Idempotency-Key": key },
      });
    },
    replace(documentId: string, filename: string, stagingId: string, key: string): Promise<ImportReceipt> {
      return scope.access.request<ImportReceipt>(`${base}/documents/${encodeURIComponent(documentId)}/replacements`, {
        method: "POST",
        body: JSON.stringify({ filename, staging_id: stagingId }),
        headers: { "Idempotency-Key": key },
      });
    },
    remove(documentId: string, key: string): Promise<ImportReceipt> {
      return scope.access.request<ImportReceipt>(`${base}/documents/${encodeURIComponent(documentId)}/deletions`, {
        method: "POST",
        headers: { "Idempotency-Key": key },
      });
    },
  };
}
