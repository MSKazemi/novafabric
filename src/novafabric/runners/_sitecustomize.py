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

Because it is the first ``sitecustomize`` on ``sys.path``, it shadows any other;
after installing the hooks it runs the next one, so a user's own site setup still
applies under capture.
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
            # M14: this process's recorder is never finalized here; at exit,
            # hand its drop counts to the orchestrator via a capsule file,
            # which merges them into capture-health.json. Fail-open.
            from novafabric.capture.workload_health import register_workload_flush as _rwf
            _rwf(_P(_capsule_dir))
        except Exception as _e:
            print(f"[novafabric] hook install failed: {_e}", file=_sys.stderr)

    # Python imports exactly one ``sitecustomize``: this one, because the runner put
    # its directory first. Run the one it shadows — the user's, the venv's or the
    # distro's — so the workload under capture is the program the user ran (review
    # capture-runners-09). Same contract as ``site.py``: an error is reported and
    # the workload still starts.
    def _chain_shadowed_sitecustomize():
        import importlib.machinery as _im
        import importlib.util as _iu
        # Every loader directory seen so far is skipped, so two loaders on the
        # path (a capture nested in a capture) never chain back into each other.
        _seen = _sys.__dict__.setdefault("_novafabric_loader_dirs", set())
        _seen.add(_os.path.dirname(_os.path.abspath(__file__)))
        _rest = [
            _p for _p in _sys.path
            if _os.path.abspath(_p or _os.curdir) not in _seen
        ]
        _spec = _im.PathFinder.find_spec("sitecustomize", _rest)
        if _spec is None or _spec.loader is None:
            return
        _mod = _iu.module_from_spec(_spec)
        _sys.modules["sitecustomize"] = _mod
        try:
            _spec.loader.exec_module(_mod)
        except Exception as _e:
            print(
                "Error in sitecustomize; set PYTHONVERBOSE for traceback:\\n"
                f"{type(_e).__name__}: {_e}",
                file=_sys.stderr,
            )

    try:
        _chain_shadowed_sitecustomize()
    except Exception as _e:
        print(f"[novafabric] could not run the shadowed sitecustomize: {_e}", file=_sys.stderr)
""")
