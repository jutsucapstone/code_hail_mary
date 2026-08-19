/**
 * Detection tests for the preflight commit hook.
 *
 * Lives in a file rather than an inline `node -e` because the fixtures below are
 * literally the strings the hook matches on — passing them on a shell command line
 * makes the hook block its own test run.
 *
 *   node scripts/hooks/preflight-on-commit.test.mjs
 */
import { isGitCommit } from "./preflight-on-commit.mjs";

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
process.exit(failed ? 1 : 0);
