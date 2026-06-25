"""Shared sitecustomize loader text used by every runner that wants
the captured workload's Python interpreter to install NovaFabric's
hooks at startup.

The string here is materialized to disk by the runner (e.g.
:class:`LocalRunner` writes it to a temporary directory; :class:`SlurmRunner`
writes it to the capsule directory which is on a shared filesystem
visible to every compute node). The runner then adds the directory
containing the file to ``PYTHONPATH``; Python's import machinery
auto-loads ``sitecustomize`` at interpreter startup so the hooks
install before the user's code runs.

Why string-not-importable: the loader cannot be a regular Python
module imported by the runner, because it needs to execute inside the
*captured* workload's Python — which is a separate process whose
``sys.path`` we control via ``PYTHONPATH``.
"""
from __future__ import annotations

import textwrap

HOOK_LOADER: str = textwrap.dedent("""\
    import os as _os, sys as _sys
    _capsule_dir = _os.environ.get("NOVAFABRIC_CAPSULE_DIR", "")
    _span_id = _os.environ.get("NOVAFABRIC_SPAN_ID", "0" * 16)
    if _capsule_dir:
        try:
            from pathlib import Path as _P
            from novafabric.capture.capsule import CapsuleWriter as _CW
            _run_id = _P(_capsule_dir).name
            _w = _CW(run_id=_run_id, base_dir=_P(_capsule_dir).parent)
            _w._dir = _P(_capsule_dir)
            # --fast-emit (ADR-0092 slice B): register import-triggered hooks so
            # an unused SDK is never imported at startup just to be patched.
            # Default (unset/"0"): eager install_all, present SDKs imported now.
            if _os.environ.get("NOVAFABRIC_FAST_EMIT") == "1":
                from novafabric.capture.hooks import install_all_deferred as _ia
            else:
                from novafabric.capture.hooks import install_all as _ia
            _ia(writer=_w, parent_span_id=_span_id)
        except Exception as _e:
            print(f"[novafabric] hook install failed: {_e}", file=_sys.stderr)
""")
