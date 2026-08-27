# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Issued-token store for ``nova serve`` (ADR-0252).

``POST /api/admin/tokens`` minted a token, told the operator *"Save this token —
it will not be shown again"*, and wrote it verbatim to
``~/.novafabric/tokens.jsonl``. Two things were wrong with that, both measured:

* the token authenticated nothing — ``verify_token`` compared only against the
  single server token, so every request with an issued token got **401**;
* the file was written at mode **0664** with the secret in cleartext, while
  ``auth.py`` takes deliberate care to create ``.serve-token`` at 0600
  atomically, with a comment explaining why.

This module is the fix. A record stores a **digest**, never the secret, the file
is created 0600 and rewritten 0600, and ``find_active()`` gives ``verify_token``
something to check against so an issued token works and a revoked one does not.

**No privilege differentiation.** An issued token is exactly as powerful as the
server token. `serve` authenticates; it does not authorize. Anything that implies
a scoped or lesser credential would be a false claim.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: Mode for any file holding (or having held) a bearer secret.
_SECRET_MODE = stat.S_IRUSR | stat.S_IWUSR  # 0600


def tokens_path() -> Path:
    """Path to the issued-token file."""
    return Path.home() / ".novafabric" / "tokens.jsonl"


def fingerprint(token: str) -> str:
    """Short, non-secret handle for a token — what the API and audit log show."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def digest(token: str) -> str:
    """Full digest stored in place of the token itself."""
    return "sha256:" + hashlib.sha256(token.encode()).hexdigest()


def _write_all(path: Path, records: list[dict[str, Any]]) -> None:
    """Replace *path* with *records*, never widening the mode.

    The previous rewrite used ``tmp.write_text()``, which creates the temp file
    at the process umask — so even a 0600 original came back 0664 after one
    revoke. Create the temp file with the mode we want, then rename.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _SECRET_MODE)
    with os.fdopen(fd, "w") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
    os.chmod(tmp, _SECRET_MODE)
    os.replace(tmp, path)


def load() -> list[dict[str, Any]]:
    """Every record in the store, oldest first. Malformed lines are skipped."""
    path = tokens_path()
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def issue(label: str, token: str) -> dict[str, Any]:
    """Append a hash-only record for *token* and return it.

    The returned record is what the API may echo: it carries no secret.
    """
    record = {
        "label": label,
        "fingerprint": fingerprint(token),
        "token_digest": digest(token),
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "revoked": False,
    }
    path = tokens_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_APPEND with an explicit mode: a write-then-chmod leaves a window where
    # the file is readable at the umask default (auth.py makes the same point).
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, _SECRET_MODE)
    with os.fdopen(fd, "a") as fh:
        fh.write(json.dumps(record) + "\n")
    harden(path)
    return record


def harden(path: Path | None = None) -> bool:
    """Narrow the store to 0600 if it is wider. Returns True if it changed.

    Called on every write, and on the first read after startup, so a file left
    world-readable by an older version is repaired rather than merely reported.
    """
    target = path or tokens_path()
    try:
        current = stat.S_IMODE(target.stat().st_mode)
    except OSError:
        return False
    if current == _SECRET_MODE:
        return False
    try:
        os.chmod(target, _SECRET_MODE)
    except OSError:
        return False
    return True


def find_active(candidate: str) -> dict[str, Any] | None:
    """Return the non-revoked record matching *candidate*, or None.

    Legacy records written before ADR-0252 hold the secret under ``token``
    instead of a digest. They are still accepted — locking an operator out of
    their own dashboard to punish them for an old file would be the wrong trade
    — but nothing writes that shape any more, and the file is hardened to 0600
    on the way past.
    """
    if not candidate:
        return None
    harden()
    wanted = digest(candidate).encode()
    for record in load():
        if record.get("revoked"):
            continue
        stored = record.get("token_digest")
        if isinstance(stored, str) and hmac.compare_digest(stored.encode(), wanted):
            return record
        legacy = record.get("token")
        if isinstance(legacy, str) and hmac.compare_digest(
            digest(legacy).encode(), wanted
        ):
            return record
    return None


def revoke(fp: str) -> bool:
    """Mark the record with fingerprint *fp* revoked. False if not found."""
    records = load()
    found = False
    for record in records:
        if record.get("fingerprint") == fp:
            record["revoked"] = True
            found = True
    if found:
        _write_all(tokens_path(), records)
    return found


def legacy_plaintext_count() -> int:
    """How many records still store the secret itself. For `nova doctor`."""
    return sum(1 for record in load() if isinstance(record.get("token"), str))
