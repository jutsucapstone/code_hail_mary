#!/usr/bin/env node
/**
 * PreToolUse hook — enforces spec §4.15: `make preflight` passes before any commit.
 *
 * The spec is explicit that enforcement which must not be talked past belongs in a
 * hook rather than in prose, so this blocks the Bash tool call itself rather than
 * asking nicely in CLAUDE.md.
 *
 * Reads the hook payload on stdin. If the command is not a `git commit`, exits 0
 * immediately and stays out of the way. Exit code 2 blocks the tool call and returns
 * stderr to the model.
 *
 * `make` is not on PATH on every dev machine (this one only has mingw32-make), so the
 * runner is resolved rather than assumed.
 */
import { execFileSync, spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const BLOCK = 2;

function readPayload() {
  try {
    return JSON.parse(readFileSync(0, "utf8"));
  } catch {
    return null;
  }
}

/**
 * True only when `commit` is git's *subcommand*.
 *
 * Deliberately tokenised rather than a regex: a pattern like /git.*commit/ matches
 * any command that merely mentions a path such as `preflight-on-commit.mjs`, which
 * blocks unrelated work. The subcommand is the first token after git's own global
 * flags, so that is what gets checked.
 */
export function isGitCommit(command) {
  if (typeof command !== "string") return false;

  for (const segment of command.split(/[;&|]+/)) {
    const tokens = segment.trim().split(/\s+/).filter(Boolean);
    if (tokens[0] !== "git") continue;

    let i = 1;
    while (i < tokens.length) {
      const t = tokens[i];
      // `-c key=value` and `--git-dir path` consume a following argument
      if (t === "-c" || t === "--git-dir" || t === "--work-tree" || t === "-C") {
        i += 2;
        continue;
      }
      if (t.startsWith("-")) {
        i += 1;
        continue;
      }
      break;
    }
    if (tokens[i] === "commit") return true;
  }
  return false;
}

/**
 * The command sequence for the first runner that actually exists on this machine.
 *
 * With any make present this is one command: `make preflight`, the target §4.15 names.
 * Without one, the fallback used to be `pnpm run preflight` alone — which covers only
 * the Node half, so on a machine with no make a commit could pass the gate having never
 * run ruff, mypy or pytest. The Python half is therefore mirrored here command by
 * command from the Makefile's lint-py / format-check-py / typecheck-py / test-py
 * targets (--no-cache for the same stale-verdict reason lint-py documents; three mypy
 * invocations because one sees several modules named "conftest" and checks nothing).
 * `api-types-check` is the one preflight stage not mirrored — it needs shell
 * redirection this runner deliberately avoids — and CI runs it on every pull request.
 */
function resolveRunner() {
  for (const bin of ["make", "mingw32-make", "gmake"]) {
    const probe = spawnSync(bin, ["--version"], { stdio: "ignore", shell: false });
    if (probe.status === 0) return [{ bin, args: ["preflight"] }];
  }
  return [
    { bin: "pnpm", args: ["run", "preflight"] },
    { bin: "uv", args: ["run", "ruff", "check", "--no-cache", "."] },
    { bin: "uv", args: ["run", "ruff", "format", "--check", "."] },
    { bin: "uv", args: ["run", "mypy", "packages"] },
    { bin: "uv", args: ["run", "mypy", "apps"] },
    { bin: "uv", args: ["run", "mypy", "conftest.py"] },
    { bin: "uv", args: ["run", "pytest", "-x", "-q"] },
  ];
}

function main() {
  const payload = readPayload();
  const command = payload?.tool_input?.command;

  if (!isGitCommit(command)) process.exit(0);

  for (const { bin, args } of resolveRunner()) {
    try {
      execFileSync(bin, args, { stdio: "inherit", shell: process.platform === "win32" });
    } catch {
      process.stderr.write(
        `\nCommit blocked: \`${bin} ${args.join(" ")}\` failed.\n\n` +
          `Spec §4.15 requires preflight (lint, typecheck, tests, migration drift) to pass\n` +
          `before any commit. Fix the failures above and commit again.\n`,
      );
      process.exit(BLOCK);
    }
  }
  process.exit(0);
}

// Only run as a hook, never on import — the test module imports isGitCommit.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main();
}
