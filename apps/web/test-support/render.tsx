import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";

/**
 * Render a component that reads server state.
 *
 * A **fresh** `QueryClient` per render, never a shared one. A client reused across tests
 * carries the previous test's cache, so the second test asserting "it fetches on mount"
 * passes without a fetch happening at all — and it passes whether or not the code still
 * works.
 *
 * Retries are off. The production policy already refuses to retry a decision
 * (`lib/query.ts`), but it does retry a 500 once, and a test asserting the error state
 * would otherwise wait out a backoff before seeing it.
 */
export function renderWithQuery(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });

  return {
    client,
    ...render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>),
  };
}
