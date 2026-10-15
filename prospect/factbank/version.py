"""factbank_version = git blob hash of factbank.yaml (docs/FACTBANK.md "Versioning")."""

from __future__ import annotations

import hashlib
from pathlib import Path


def blob_hash(path: Path) -> str:
    """Same value as `git hash-object`: CRLF is normalized to LF first, which is
    what git stores when core.autocrlf is on (Windows checkouts)."""
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
