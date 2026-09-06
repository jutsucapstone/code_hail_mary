/**
 * Detection tests for the preflight commit hook.
 *
 * Lives in a file rather than an inline `node -e` because the fixtures below are
 * literally the strings the hook matches on — passing them on a shell command line
 * makes the hook block its own test run.
 *
 *   node scripts/hooks/preflight-on-commit.test.mjs
 */
import { isGitCommit, resolveCommitDirectory } from "./preflight-on-commit.mjs";
import { tmpdir } from "node:os";

const G = "g" + "it";
const C = "com" + "mit";

const cases = [
  [`${G} ${C} -m x`, true],
  [`${G} -c user.name=a ${C} -m x`, true],
  [`cd /tmp && ${G} ${C} -am y`, true],
  [`${G} -C repo ${C}`, true],
  [`${G} --no-pager ${C}`, true],
  // the false positive that motivated tokenising: a path that merely ends in the word
  [`${G} mv a scripts/hooks/preflight-on-${C}.mjs`, false],
  [`${G} add -A`, false],
  [`${G} status --short`, false],
  [`${G} log --oneline -3`, false],
  [`echo ${C}`, false],
  [`node -e "x = '${G} ${C}'"`, false],
  ["", false],
];

let failed = 0;
for (const [command, want] of cases) {
  const got = isGitCommit(command);
  const ok = got === want;
  if (!ok) failed++;
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${JSON.stringify(command)} -> ${got} (want ${want})`);
}

console.log(`\n  ${cases.length - failed} passed, ${failed} failed`);

// ---------------------------------------------------------------- where it runs
//
// The hook inherits the session's directory, which with git worktrees is routinely a
// different tree at a different commit from the one being committed. Preflight run
// there certifies code that is not being committed, and blocks on failures belonging
// to a tree nobody touched. These pin the directory it actually chooses.

const HERE = process.cwd();
const TMP = tmpdir();
const ABSENT = "/definitely/not/a/directory/on/this/machine";

const dirCases = [
  [`cd "${HERE}" && ${G} ${C} -m x`, HERE, HERE, "a quoted cd wins over the payload"],
  [`cd ${TMP} && ${G} ${C} -m x`, HERE, TMP, "an unquoted cd is honoured too"],
  [`${G} -C "${TMP}" ${C} -m x`, HERE, TMP, "an explicit -C wins"],
  [`${G} ${C} -m x`, HERE, HERE, "with no cd, the payload directory stands"],
  [`cd ${ABSENT} && ${G} ${C} -m x`, HERE, HERE, "a path that is not a directory is ignored"],
  [`${G} ${C} -m x`, undefined, undefined, "nothing to resolve stays undefined"],
];

for (const [command, fallback, want, label] of dirCases) {
  const got = resolveCommitDirectory(command, fallback);
  const ok = got === want;
  if (!ok) failed++;
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${label} -> ${got} (want ${want})`);
}

console.log(`
  ${cases.length + dirCases.length - failed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
