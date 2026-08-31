"use client";

import { QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

import { createQueryClient } from "@/lib/query";

/**
 * The server-state cache, mounted once at the root.
 *
 * `useState` with an initialiser, not a module-level `new QueryClient()`. On the server a
 * module singleton is shared across concurrent requests, so one tenant's cached
 * organisation profile would be served to whoever asked next — a cross-tenant leak
 * introduced by a caching library, in an application whose entire data layer is built to
 * make that impossible (§4.7). Holding it in state binds one client to one mount.
 *
 * Children stay server components: passing them through as `children` from the root
 * layout means this boundary wraps them without pulling them across it.
 */
export function QueryProvider({ children }: { children: React.ReactNode }) {
  const [client] = useState(createQueryClient);

  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
