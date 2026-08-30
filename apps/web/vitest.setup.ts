import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

/**
 * Unmount between tests, and leave no `fetch` stub behind.
 *
 * Without the cleanup a component from the previous test is still in the document and
 * `getByRole` finds two of everything; without the restore, one test's scripted response
 * silently answers the next test's request.
 */
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
