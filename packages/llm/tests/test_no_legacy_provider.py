"""The removal, asserted rather than remembered (ADR 0024).

A vendor comes out of a codebase in four places, and three of them are easy to miss: the
import survives in a module nobody re-read, the dependency survives in a `pyproject.toml`,
the secret mount survives in the deploy workflow, and the environment variable survives in
`.env.example` where the next operator copies it into a real deployment.

This file is the gate. It walks the repository as *files* — no imports, no reflection —
because the thing being asserted is that certain strings are not there, and a test that
imported the code could only ever see the half of the repository that is Python.

**Documentation is deliberately exempt.** `docs/adr/` records why JUTSU moved off a single
vendor, and an architecture decision record that may not name the decision would be
useless. The exemption is by directory, so it cannot quietly widen: a source file does not
become documentation by containing a paragraph.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The vendor and its model family, lower-cased. `claude` also matches `CLAUDE.md`, which
#: is why the walk skips that filename explicitly rather than dropping the term.
FORBIDDEN = ("anthropic", "claude")

#: Where a reference would be a live dependency rather than a record of history.
SEARCHED = (
    Path("apps/api/src"),
    Path("apps/worker/src"),
    Path("apps/web/app"),
    Path("apps/web/components"),
    Path("apps/web/lib"),
    Path("packages/core/src"),
    Path("packages/connectors/src"),
    Path("packages/db/src"),
    Path("packages/evals/src"),
    Path("packages/graph/src"),
    Path("packages/llm/src"),
    Path("packages/retrieval/src"),
)

#: History and rationale. An ADR that could not name what it superseded would be a record
#: of nothing, and the commit message is already permanent.
EXEMPT_NAMES = frozenset({"CLAUDE.md", "AGENTS.md"})

_SOURCE_SUFFIXES = frozenset({".py", ".ts", ".tsx", ".js", ".mjs", ".toml", ".yml", ".yaml"})


def _sources() -> list[Path]:
    found: list[Path] = []
    for relative in SEARCHED:
        root = REPO_ROOT / relative
        if not root.exists():
            continue
        found.extend(
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix in _SOURCE_SUFFIXES
            and path.name not in EXEMPT_NAMES
            and "node_modules" not in path.parts
            and ".next" not in path.parts
            and "__pycache__" not in path.parts
        )
    return sorted(found)


def _offending_lines(path: Path) -> list[str]:
    """Lines naming the vendor, minus the ones naming this repository's own guide.

    `CLAUDE.md` is a filename in this project — the instructions file every module's
    docstrings cite for a recorded trap — and dropping the search term to avoid it would
    remove the half of this gate that matters most.
    """
    offending: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        lowered = line.lower()
        if not any(term in lowered for term in FORBIDDEN):
            continue
        if "claude.md" in lowered and "anthropic" not in lowered:
            continue
        offending.append(line.strip())
    return offending


class TestTheVendorIsGone:
    def test_the_walk_finds_something_to_walk(self) -> None:
        """Without this, every assertion below passes by finding no files at all."""
        sources = _sources()

        assert len(sources) > 100, f"only {len(sources)} sources found — the gate is vacuous"

    def test_no_production_source_names_the_removed_vendor(self) -> None:
        offenders = {
            path.relative_to(REPO_ROOT).as_posix(): lines
            for path in _sources()
            if (lines := _offending_lines(path))
        }

        assert offenders == {}, f"removed vendor still referenced: {offenders}"

    def test_no_package_depends_on_the_removed_sdk(self) -> None:
        """A dependency nothing imports still ships in the image and still needs patching."""
        manifests = [*sorted(REPO_ROOT.glob("*/*/pyproject.toml")), REPO_ROOT / "pyproject.toml"]

        assert manifests, "no manifests found — the gate is vacuous"
        for manifest in manifests:
            text = manifest.read_text(encoding="utf-8")
            assert "anthropic" not in text.lower(), f"anthropic is a dependency of {manifest}"

    def test_the_lockfile_holds_no_resolution_for_it(self) -> None:
        """The manifests and the lock can disagree, and the lock is what gets installed."""
        lock = REPO_ROOT / "uv.lock"
        if not lock.exists():
            pytest.skip("no uv.lock in this checkout")

        assert 'name = "anthropic"' not in lock.read_text(encoding="utf-8")

    @pytest.mark.parametrize("relative", [".env.example", ".github/workflows/deploy.yml"])
    def test_no_deployment_surface_still_carries_the_key(self, relative: str) -> None:
        """The two files that would put the variable back into a real deployment.

        `.env.example` is what an operator copies, and the workflow is what mounts
        secrets into Cloud Run. A stale name in either is not inert: it is an instruction
        to configure a provider that no code can use, and the failure it produces — a
        deployment that looks configured and cannot answer — is the one this whole
        change exists to make impossible.
        """
        path = REPO_ROOT / relative
        if not path.exists():
            pytest.skip(f"{relative} is not in this checkout")

        assert "ANTHROPIC" not in path.read_text(encoding="utf-8").upper()
