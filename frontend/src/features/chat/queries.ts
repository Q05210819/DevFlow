"use client";

import { useQueries, useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { QuerySelection, SourcePreview } from "@/features/knowledge/types";
import type {
  ConversationList, DeletionReceipt, MessagePage, RunReceipt,
} from "./types";

export const HISTORY_LIMIT = 30;

// Every chat cache key carries the session actor and the conversation, so a
// different identity or a different conversation can never read this cache.
export const chatKey = (userId: string) => ["chat", userId] as const;

export function useConversations(userId: string) {
  return useQuery({
    queryKey: [...chatKey(userId), "conversations"],
    queryFn: ({ signal }) => api<ConversationList>("/api/v1/conversations", { signal }),
    retry: false, refetchOnWindowFocus: false, refetchOnReconnect: false,
  });
}

export function useMessagePages(userId: string, conversationId: string, cursors: (string | null)[]) {
  return useQueries({
    queries: cursors.map(cursor => ({
      queryKey: [...chatKey(userId), "messages", conversationId, cursor ?? "first"],
      queryFn: ({ signal }: { signal: AbortSignal }) => {
        const search = new URLSearchParams({ limit: String(HISTORY_LIMIT) });
        if (cursor) search.set("cursor", cursor);
        return api<MessagePage>(
          `/api/v1/conversations/${encodeURIComponent(conversationId)}/messages?${search.toString()}`,
          { signal },
        );
      },
      retry: false, refetchOnWindowFocus: false, refetchOnReconnect: false,
    })),
  });
}

export function useCitationPreview(
  userId: string,
  conversationId: string,
  messageId: string | null,
  citationId: string | null,
  cursor: string | null,
) {
  return useQuery({
    queryKey: [...chatKey(userId), "citation", conversationId, messageId ?? "", citationId ?? "", cursor ?? ""],
    queryFn: ({ signal }) => {
      const search = cursor ? `?${new URLSearchParams({ cursor }).toString()}` : "";
      return api<SourcePreview>(
        `/api/v1/conversations/${encodeURIComponent(conversationId)}/messages/` +
        `${encodeURIComponent(messageId!)}/citations/${encodeURIComponent(citationId!)}${search}`,
        { signal },
      );
    },
    enabled: messageId !== null && citationId !== null,
    // A stored citation is immutable for its version, message and cursor.
    staleTime: Infinity, refetchOnMount: false,
    retry: false, refetchOnWindowFocus: false, refetchOnReconnect: false,
  });
}

export function createConversation(selectedRepositoryIds: string[], key: string) {
  return api<{ id: string; selected_repository_ids: string[]; archived: boolean; created_at: string }>(
    "/api/v1/conversations",
    {
      method: "POST",
      body: JSON.stringify({ selected_repository_ids: selectedRepositoryIds }),
      headers: { "Idempotency-Key": key },
    },
  );
}

export function submitMessage(conversationId: string, selection: QuerySelection, question: string, key: string) {
  return api<RunReceipt>(`/api/v1/conversations/${encodeURIComponent(conversationId)}/messages`, {
    method: "POST",
    body: JSON.stringify({ selection, question }),
    headers: { "Idempotency-Key": key },
  });
}

export function cancelJob(jobId: string, key: string) {
  return api<{ id: string; status: string; error_code: string | null }>(
    `/api/v1/jobs/${encodeURIComponent(jobId)}/cancel`,
    { method: "POST", headers: { "Idempotency-Key": key } },
  );
}

export function deleteConversation(conversationId: string, key: string) {
  return api<DeletionReceipt>(`/api/v1/conversations/${encodeURIComponent(conversationId)}/deletions`, {
    method: "POST",
    headers: { "Idempotency-Key": key },
  });
}
