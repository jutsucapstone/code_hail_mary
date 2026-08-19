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

/** First runner that actually exists on this machine. */
function resolveRunner() {
  for (const bin of ["make", "mingw32-make", "gmake"]) {
    const probe = spawnSync(bin, ["--version"], { stdio: "ignore", shell: false });
    if (probe.status === 0) return { bin, args: ["preflight"] };
  }
  // No make anywhere — fall back to the mirrored pnpm script.
  return { bin: "pnpm", args: ["run", "preflight"] };
}

function main() {
  const payload = readPayload();
  const command = payload?.tool_input?.command;

  if (!isGitCommit(command)) process.exit(0);

  const { bin, args } = resolveRunner();

  try {
    execFileSync(bin, args, { stdio: "inherit", shell: process.platform === "win32" });
    process.exit(0);
  } catch {
    process.stderr.write(
      `\nCommit blocked: \`${bin} ${args.join(" ")}\` failed.\n\n` +
        `Spec §4.15 requires preflight (lint, typecheck, tests, migration drift) to pass\n` +
        `before any commit. Fix the failures above and commit again.\n`,
    );
    process.exit(BLOCK);
  }
}

// Only run as a hook, never on import — the test module imports isGitCommit.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main();
}
