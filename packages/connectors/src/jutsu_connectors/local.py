"""A read-only connector over a directory of mail (spec §9, §19).

Implements the `Connector` protocol against `SourceSystem.LOCAL`, which is what the pilot
corpora arrive as. It is the reference implementation: everything a provider connector
does in Phase 4 — cursors, ACL capture, idempotent identifiers — it does here, without
OAuth in the way.

**Read-only by construction.** There is no write method on the protocol and none here.
§4.8 is absolute about this and the shape of the code should make it obvious rather than
merely true.

**Path containment is a security control, not tidiness.** `external_id` is a
corpus-relative path, and a path that arrives from anywhere other than this connector's
own listing is untrusted input to a file read. Two separate escapes are closed:

  * `..` and absolute paths in an `external_id`, rejected before any filesystem call.
  * Symlinks pointing outside the corpus, rejected after resolution — including a
    symlinked *file*, which `os.walk(followlinks=False)` does not protect against
    because that flag only governs directory recursion.

Both are checked against the **resolved** root, so a symlinked corpus root is still a
valid root and only escapes from it are refused.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Final

from jutsu_core import AclEntry, RawDocument, SourceSystem

from jutsu_connectors.rfc822 import (
    ParsedMessage,
    UnparsableMessage,
    acls_for,
    parse_message,
    to_raw_document,
    utc_from_timestamp,
)

__all__ = ["LocalConnector", "PathEscape"]

#: Files that are never mail. Skipped before opening, so a corpus directory can hold a
#: README without every walk having to parse and reject it.
_IGNORED_NAMES: Final = frozenset({".DS_Store", "Thumbs.db", ".gitkeep", "sample_manifest.json"})

#: A hard ceiling on a single message. The Enron corpus's largest messages are a few
#: megabytes; anything past this is a corrupted download or something that is not mail,
#: and reading it into memory during a corpus walk is a denial of service against the
#: worker (§30 — source content is untrusted).
MAX_MESSAGE_BYTES: Final = 32 * 1024 * 1024


class PathEscape(ValueError):
    """An identifier resolved outside the corpus root.

    Its own class because it is a security refusal rather than a missing file, and the
    two should not be caught together — a caller that swallows "not found" must not
    silently swallow "tried to read /etc/passwd".
    """


class LocalConnector:
    """Mail files under a directory, exposed as documents.

    `external_id` is the corpus-relative POSIX path. For a file corpus the path is the
    stable identifier — it is what `fetch` needs, it is unique, and it survives a
    re-walk — so it is what §4.14's idempotency key is built on. The `Message-ID` is
    carried in `raw_metadata` and drives threading; it is deliberately not the external
    id, because a third of a real corpus has none.
    """

    system = SourceSystem.LOCAL

    __slots__ = ("_root",)

    def __init__(self, root: Path) -> None:
        resolved = root.resolve(strict=True)
        if not resolved.is_dir():
            raise NotADirectoryError(f"{root} is not a directory")
        self._root = resolved

    @property
    def root(self) -> Path:
        return self._root

    # ----------------------------------------------------------------- containment

    def resolve(self, external_id: str) -> Path:
        """A corpus-relative identifier to an absolute path inside the root, or raise.

        Ordered so the cheap syntactic refusals happen before any filesystem call: an
        absolute path or one containing `..` is rejected outright, and only then is the
        result resolved and checked for a symlink escape.
        """
        candidate = PurePosixPath(external_id)
        if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
            raise PathEscape("identifier must be a relative path inside the corpus")
        if not external_id or external_id.startswith("/") or "\\" in external_id:
            # Backslashes are refused rather than normalised. On Windows they are
            # separators, on POSIX they are legal filename characters, and a rule that
            # means different things on the developer's machine and in the worker is not
            # a rule.
            raise PathEscape("identifier must be a POSIX-relative path")

        target = (self._root / candidate).resolve()
        if not target.is_relative_to(self._root):
            raise PathEscape("identifier resolves outside the corpus root")
        return target

    def _relative_id(self, path: Path) -> str | None:
        """A path inside the corpus as its identifier, or None if it escapes.

        Applied to every file the walk yields, because `followlinks=False` stops the walk
        descending through a symlinked *directory* and does nothing about a symlinked
        file that points somewhere else entirely.
        """
        try:
            resolved = path.resolve()
        except OSError:
            return None
        if not resolved.is_relative_to(self._root):
            return None
        return PurePosixPath(resolved.relative_to(self._root)).as_posix()

    # ----------------------------------------------------------------- protocol

    async def list_since(self, cursor: str | None) -> AsyncIterator[str]:
        """Identifiers modified at or after `cursor`, in deterministic order.

        The cursor is an ISO-8601 instant taken from the filesystem's modification time.
        That is as reliable as the filesystem clock and no more — good enough to resume a
        local corpus walk, and the reason a provider connector uses the provider's own
        change token instead.

        Sorted rather than yielded in `os.walk` order, so two runs over the same corpus
        produce the same sequence. Sampling determinism depends on it.
        """
        since = datetime.fromisoformat(cursor) if cursor else None

        for directory, subdirectories, filenames in os.walk(self._root, followlinks=False):
            subdirectories.sort()
            for filename in sorted(filenames):
                if filename in _IGNORED_NAMES:
                    continue
                path = Path(directory) / filename
                identifier = self._relative_id(path)
                if identifier is None:
                    continue
                if since is not None:
                    try:
                        modified = utc_from_timestamp(path.stat().st_mtime)
                    except OSError:
                        continue
                    if modified < since:
                        continue
                yield identifier

    async def fetch(self, external_id: str) -> RawDocument:
        """One document. `thread_id` is best-effort; the corpus knows better.

        A message can name its parent but not its thread, so the value here is the first
        reference it carries, falling back to its own id. `sample_enron` overrides it with
        the canonical thread from `build_thread_index`, which has seen the whole corpus.
        The difference is documented rather than hidden because the two disagree by
        design.
        """
        path = self.resolve(external_id)
        parsed = self._parse(path)
        local_thread = parsed.references[0] if parsed.references else parsed.message_id

        return to_raw_document(
            parsed,
            external_id=external_id,
            source_system=self.system,
            uri=external_id,
            thread_id=local_thread,
            fallback_sent_at=utc_from_timestamp(path.stat().st_mtime),
        )

    async def acls(self, external_id: str) -> list[AclEntry]:
        """Read grants for one document, derived from its participants (ADR 0008)."""
        return acls_for(self._parse(self.resolve(external_id)))

    # ----------------------------------------------------------------- internals

    def _parse(self, path: Path) -> ParsedMessage:
        size = path.stat().st_size
        if size > MAX_MESSAGE_BYTES:
            raise UnparsableMessage("message exceeds the maximum size")
        return parse_message(path.read_bytes())
