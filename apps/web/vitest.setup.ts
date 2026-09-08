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

/**
 * `Blob.text()` and `Blob.arrayBuffer()`, which jsdom does not implement.
 *
 * Both are Baseline in every browser this application targets and are how the bulk
 * onboarding panel reads the file a person dropped — a CSV as text, a workbook as bytes.
 * jsdom's `Blob` still has neither, so without these the only tests that could cover
 * reading a file would be tests that mocked the reading away.
 *
 * Deliberately in the setup rather than in the component: reaching for `FileReader` in
 * application code to satisfy a test environment would put a 2010 API into a 2026
 * codebase for reasons no reader could infer.
 */
if (typeof Blob.prototype.text !== "function") {
  Object.defineProperty(Blob.prototype, "arrayBuffer", {
    configurable: true,
    writable: true,
    value(this: Blob) {
      return new Promise<ArrayBuffer>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result as ArrayBuffer);
        reader.onerror = () => reject(reader.error);
        reader.readAsArrayBuffer(this);
      });
    },
  });
  Object.defineProperty(Blob.prototype, "text", {
    configurable: true,
    writable: true,
    value(this: Blob) {
      return new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.onerror = () => reject(reader.error);
        reader.readAsText(this);
      });
    },
  });
}
