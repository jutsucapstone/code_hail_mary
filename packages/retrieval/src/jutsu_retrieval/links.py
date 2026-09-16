"""A link to a document where its source keeps it — only ever the address the source gave.

A citation that opens the original — the GitHub issue, the Drive file, the Zoom recording — is
only as trustworthy as the address behind it, and that address arrived from a provider's API,
or is a file path from a local corpus. So a link leaves retrieval only when it is an absolute
`https` or `http` address with a host:

- never a `javascript:`, `data:` or `file:` scheme a browser would execute or resolve locally;
- never a relative path, or a path that names somebody's machine;
- never an address carrying a user name or password;
- never whitespace or control characters, which is how one string becomes two meanings.

Nothing here composes an address. A document with no usable one has no link, and its citation
still opens the passage itself through the evidence door.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import urlsplit

__all__ = ["MAX_SOURCE_URI_CHARS", "safe_source_uri"]

#: Longer than any address a provider returns for a document, and short enough that a stored
#: value can never turn into a large response field.
MAX_SOURCE_URI_CHARS: Final = 2048

_SCHEMES: Final = frozenset({"https", "http"})


def safe_source_uri(value: object) -> str | None:
    """`value` when a browser may safely open it as a link to the source, otherwise None."""
    if not isinstance(value, str) or not value or len(value) > MAX_SOURCE_URI_CHARS:
        return None
    if any(character.isspace() or not character.isprintable() for character in value):
        return None
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
    except ValueError:
        return None
    if parts.scheme.lower() not in _SCHEMES or not hostname:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    return value
