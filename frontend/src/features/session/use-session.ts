"use client";

import { useContext } from "react";
import { SessionContext } from "./provider";

export function useSession() {
  const context = useContext(SessionContext);
  if (!context) throw new Error("SessionProvider is required");
  return context;
}
