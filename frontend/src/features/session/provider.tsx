"use client";

import { QueryClient, QueryClientProvider, useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api, setCsrfToken } from "@/lib/api";
import type { Session } from "@/lib/types";

type SessionContextValue = {
  session: Session | null;
  isLoading: boolean;
  error: boolean;
  logout: () => Promise<void>;
};

export const SessionContext = createContext<SessionContextValue | null>(null);

const eventConnections = new Set<EventSource>();

export function registerEventConnection(connection: EventSource) {
  eventConnections.add(connection);
  return () => eventConnections.delete(connection);
}

function SessionState({ children }: { children: React.ReactNode }) {
  const queryClient = useQueryClient();
  const [signedOut, setSignedOut] = useState(false);
  const identity = useRef<string | null | undefined>(undefined);
  function acceptSession(value: Session | null) {
    const next = value?.user.id ?? null;
    if (identity.current !== undefined && identity.current !== next) {
      const privateQueries = { predicate: (query: { queryKey: readonly unknown[] }) => query.queryKey[0] !== "session" };
      void queryClient.cancelQueries(privateQueries);
      queryClient.removeQueries(privateQueries);
      for (const connection of eventConnections) connection.close();
      eventConnections.clear();
    }
    identity.current = next;
    // Install the new identity's token before private descendants can submit.
    setCsrfToken(value?.csrf_token ?? null);
    return value;
  }
  const sessionQuery = useQuery({
    queryKey: ["session"],
    queryFn: async ({ signal }): Promise<Session | null> => {
      try {
        const value = await api<Session>("/api/v1/session", { signal });
        signal.throwIfAborted();
        return acceptSession(value);
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) return acceptSession(null);
        throw error;
      }
    },
    retry: false,
    staleTime: 0,
  });

  useEffect(() => setCsrfToken(sessionQuery.data?.csrf_token ?? null), [sessionQuery.data?.csrf_token]);

  const logout = useCallback(async () => {
    await api<void>("/api/v1/auth/logout", { method: "POST" });
    // Hide private component state even if the document navigation is delayed.
    setSignedOut(true);
    await queryClient.cancelQueries();
    for (const connection of eventConnections) connection.close();
    eventConnections.clear();
    queryClient.clear();
    setCsrfToken(null);
    window.location.replace("/login");
  }, [queryClient]);

  return <SessionContext.Provider value={{
    session: signedOut ? null : sessionQuery.data ?? null,
    isLoading: !signedOut && sessionQuery.isLoading,
    error: !signedOut && sessionQuery.isError,
    logout,
  }}>{children}</SessionContext.Provider>;
}

export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(() => new QueryClient({ defaultOptions: { queries: { retry: false } } }));
  return <QueryClientProvider client={queryClient}><SessionState>{children}</SessionState></QueryClientProvider>;
}
