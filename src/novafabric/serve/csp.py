"""The Content-Security-Policy ``nova serve`` sends with the dashboard (issue #278).

The built dashboard carries a few small inline scripts (Astro's island loader, the
theme bootstrap, the ``/`` redirect). A policy with ``'unsafe-inline'`` would let any
injected ``<script>`` run too, so the policy lists the SHA-256 hash of each inline
script instead. The hashes are computed from the HTML files actually being served,
when the app is built, so a rebuilt bundle needs no hand-edited hash list: restart
``nova serve`` and the new hashes are in the header.

What the policy allows and why:

* ``script-src 'self'`` plus one ``'sha256-…'`` per inline script: bundled
  ``/_astro/*`` and ``/topology/assets/*`` chunks load; injected inline scripts,
  inline event handlers (``onclick=…``), ``javascript:`` URLs and scripts from any
  other origin do not.
* ``'unsafe-eval'``: the run inspector validates a capsule in the browser with Ajv,
  which compiles each JSON Schema with ``Function(…)``. Without it that check fails.
  Dropping it needs validators precompiled at build time; until then it stays.
* ``worker-src 'self' blob:``: the topology view starts a module worker from
  ``/topology/assets/`` and its text renderer starts workers from ``blob:`` URLs.

``style-src``, ``img-src``, ``connect-src`` and ``font-src`` are deliberately not set:
the dashboard can be pointed at a server base URL other than its own origin, and
restricting those is a separate decision. There is no ``default-src``, so none of
them falls back to a restrictive value.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
from collections.abc import Iterable
from pathlib import Path

#: Directives on every response, the Swagger UI page (``/api/docs``) included. That
#: page loads its script from a CDN and runs an inline bootstrap, so it keeps only
#: these and no ``script-src``.
BASE_DIRECTIVES: tuple[str, ...] = (
    "object-src 'none'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
    "form-action 'self'",
)

#: The base policy as one header value.
BASE_POLICY: str = "; ".join(BASE_DIRECTIVES)

# A ``<script …>…</script>`` element. Script content is raw text in HTML, so the
# first ``</script`` ends it; the attribute part is matched loosely (the built
# bundle never puts ``>`` inside an attribute value).
_SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL)
_SRC_ATTR_RE = re.compile(r"(?:^|\s)src\s*=", re.IGNORECASE)

#: Bounds on the startup scan, so a huge or hostile static directory cannot stall it.
MAX_HTML_FILES = 2000
MAX_HTML_BYTES = 4 * 1024 * 1024


def script_hash(source: str) -> str:
    """The CSP source expression body (``sha256-…``) for one inline script's text.

    Browsers hash the script's text after the HTML parser has normalised line
    endings, so ``\\r\\n`` and a lone ``\\r`` count as ``\\n`` here too.
    """
    normalised = source.replace("\r\n", "\n").replace("\r", "\n")
    digest = hashlib.sha256(normalised.encode("utf-8")).digest()
    return "sha256-" + base64.b64encode(digest).decode("ascii")


def inline_script_hashes_in_html(html: str) -> set[str]:
    """Hashes of every inline script in *html* (scripts with ``src=`` are skipped).

    An empty ``<script></script>`` contributes nothing. Non-JavaScript data blocks
    (``type="application/ld+json"``) are hashed too: the browser never executes
    them, so an extra hash for one allows nothing that could run.
    """
    hashes: set[str] = set()
    for match in _SCRIPT_RE.finditer(html):
        attrs, body = match.group(1), match.group(2)
        if _SRC_ATTR_RE.search(attrs) or not body:
            continue
        hashes.add(script_hash(body))
    return hashes


def inline_script_hashes(static_dir: Path | None) -> tuple[str, ...]:
    """Sorted hashes of the inline scripts in every ``*.html`` under *static_dir*.

    ``None`` or a missing directory gives ``()``. Symlinked directories are not
    followed, unreadable files are skipped, and the scan stops after
    :data:`MAX_HTML_FILES` files; a file larger than :data:`MAX_HTML_BYTES` is
    skipped. A skipped page's inline scripts are then blocked by the browser,
    which fails closed.
    """
    if static_dir is None or not static_dir.is_dir():
        return ()
    hashes: set[str] = set()
    seen = 0
    for root, _dirs, files in os.walk(static_dir, followlinks=False):
        for name in sorted(files):
            if not name.lower().endswith(".html"):
                continue
            seen += 1
            if seen > MAX_HTML_FILES:
                return tuple(sorted(hashes))
            path = Path(root) / name
            try:
                if not path.is_file() or path.stat().st_size > MAX_HTML_BYTES:
                    continue
                html = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            hashes |= inline_script_hashes_in_html(html)
    return tuple(sorted(hashes))


def build_content_security_policy(script_hashes: Iterable[str]) -> str:
    """The dashboard policy: :data:`BASE_DIRECTIVES` plus the script and worker rules."""
    script_sources = ["'self'", "'unsafe-eval'"]
    script_sources += [f"'{h}'" for h in sorted(set(script_hashes))]
    return "; ".join(
        (
            *BASE_DIRECTIVES,
            "script-src " + " ".join(script_sources),
            "worker-src 'self' blob:",
        )
    )


def dashboard_content_security_policy(static_dir: Path | None) -> str:
    """:func:`build_content_security_policy` over the inline scripts of *static_dir*."""
    return build_content_security_policy(inline_script_hashes(static_dir))
