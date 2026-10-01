"use client";

import { QueryClient, QueryClientProvider, useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useEffect, useState } from "react";
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
  const sessionQuery = useQuery({
    queryKey: ["session"],
    queryFn: async ({ signal }): Promise<Session | null> => {
      try {
        return await api<Session>("/api/v1/session", { signal });
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) return null;
        throw error;
      }
    },
    retry: false,
    staleTime: 0,
  });

  useEffect(() => setCsrfToken(sessionQuery.data?.csrf_token ?? null), [sessionQuery.data?.csrf_token]);

  const logout = useCallback(async () => {
    await api<void>("/api/v1/auth/logout", { method: "POST" });
    await queryClient.cancelQueries();
    for (const connection of eventConnections) connection.close();
    eventConnections.clear();
    queryClient.clear();
    setCsrfToken(null);
    window.location.replace("/login");
  }, [queryClient]);

  return <SessionContext.Provider value={{
    session: sessionQuery.data ?? null,
    isLoading: sessionQuery.isLoading,
    error: sessionQuery.isError,
    logout,
  }}>{children}</SessionContext.Provider>;
}

export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(() => new QueryClient({ defaultOptions: { queries: { retry: false } } }));
  return <QueryClientProvider client={queryClient}><SessionState>{children}</SessionState></QueryClientProvider>;
}
