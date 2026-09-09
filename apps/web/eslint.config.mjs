import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
    // Node build tooling, not app source — CommonJS by design.
    "scripts/**",
    // Static assets, not source. Vendored third-party bundles live here (the Spline
    // viewer), and linting somebody else's minified output produces hundreds of
    // findings nobody may act on — the file is not ours to change, and reformatting it
    // would break the pinning that makes vendoring safe in the first place.
    "public/**",
  ]),
]);

export default eslintConfig;
