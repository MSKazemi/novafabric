from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import threading
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from novafabric.capture.capsule import CapsuleWriter

_log = logging.getLogger(__name__)

_installed: list[object] = []

# Tracks whether install_all() set the EventRecorder singleton itself (vs an
# orchestrator that manages its own recorder lifecycle). Only when we set it do
# we clear it in uninstall_all() — so we never clobber an externally-owned one.
#: The recorder ``install_all()`` installed, or None. Holds the object rather
#: than a bool so ``uninstall_all()`` can clear by identity: the flag alone
#: only records that *we* set one, not that ours is still the one installed.
_recorder_set_by_install: object | None = None

# ── Single-owner guard for the process-global hooks (ADR-0224) ─────────────
#
# `_installed` and the EventRecorder singleton are process-global with no
# per-task scoping, so two concurrent in-process captures collided three ways
# (all three reproduced, see ADR-0224 §Evidence):
#
#   1. the second install_all() left the FIRST capture's recorder in place, so
#      capture B's network/file events were filed into capture A's capsule —
#      an evidence-integrity fault, not merely lost data;
#   2. the second install_all() stacked a second patch layer (6 hooks -> 12),
#      so an event could be recorded twice while both were live;
#   3. whichever capture finished first ran uninstall_all() and tore down
#      *both*, leaving the still-running capture with no hooks and no recorder.
#
# Exactly one capture owns the patch layer at a time. A concurrent capture
# installs no second, conflicting layer; since ADR-0224 phase 2 it binds its own
# task-scoped recorder and writer instead, so its events reach its own capsule
# through the owner's layer — and every outcome is stamped into the capsule
# (wire_capture_state) rather than left to be inferred from a short stream.
_HOOK_OWNER_LOCK = threading.Lock()
_hook_owner: str | None = None

# ── Token contract (ADR-0224 D3, phase 2 — 2026-08-29) ─────────────────────
#
# Phase 1's token had two states: a non-empty string (you own the hooks) and
# ``""`` (you lost the race, nothing was installed and nothing is yours to
# release). ADR-0224 named the third state as the remaining slice: for the
# loser to *participate* — bind its own recorder and writer so its events land
# in its own capsule through the winner's single patch layer — and later
# release that binding, the token has to say which of the two it is.
#
# The prefix is internal. Callers still treat the token as opaque and still
# hand it straight back to uninstall_all(); wire_capture_state() decodes it.
_OWNER_PREFIX = "own:"
_PARTICIPANT_PREFIX = "par:"

#: Capture-scope binding handles by token — the owner's **and** every
#: participant's (ADR-0224 D3 ▸ Amendment 3) — so uninstall_all() can release a
#: binding from whatever context it is called in. Every entry is removed by the
#: uninstall that pairs with its install; the legacy ``uninstall_all()`` clears
#: them all. Capped so a caller that never hands its token back cannot grow it
#: without bound: past the cap a capture simply does not bind, and its marker
#: says so (``skipped-concurrent``) rather than claiming a scoped stream.
_scope_bindings: dict[str, str] = {}
_MAX_LIVE_SCOPES = 256

#: Participant tokens whose owner tore its patch layer down while they were
#: still running. Their binding is kept — revoking it would send the rest of
#: their events to whichever capture installs hooks next — but their wire stream
#: has a gap, and ``wire_capture_state`` must say so. Bounded like the above.
_truncated_participants: set[str] = set()

#: Owner tokens that were live while another capture tried to claim the hooks.
#:
#: This is the residual risk the single-owner guard does NOT remove. The hooks
#: are process-global monkeypatches holding the *owner's* writer, so while two
#: captures overlap, the non-owner's HTTP/file traffic is still intercepted and
#: recorded into the OWNER's capsule. The guard stops teardown and double-
#: recording; it cannot stop cross-attribution short of task-scoped hooks
#: (ADR-0224 phase 2).
#:
#: An evidence system must not leave that inferable only from a suspiciously
#: busy stream, so the owner can ask whether it was contended and mark its own
#: capsule accordingly.
_contended_owners: set[str] = set()


#: Opt-in strict mode (ADR-0224 OQ-2). Truthy values enable it.
STRICT_ENV_VAR = "NOVAFABRIC_CAPTURE_STRICT"


class ConcurrentCaptureRefused(RuntimeError):
    """Strict mode: a capture was refused because another is live in-process.

    Raised by :func:`install_all` **before** anything is installed or bound, so
    a refused capture leaves no state behind. It is the one deliberate exception
    to capture's fail-open rule: an operator who sets strict mode has asked that
    a knowingly partial or contended capsule be an error, not a silent
    degradation. Never raised unless strict mode was requested.
    """


def strict_capture_enabled(strict: bool | None = None) -> bool:
    """Explicit *strict* wins; otherwise ``NOVAFABRIC_CAPTURE_STRICT`` (default off)."""
    if strict is not None:
        return strict
    return os.environ.get(STRICT_ENV_VAR, "").strip().lower() in {"1", "true", "yes", "on"}


def _claim_hook_ownership(strict: bool = False) -> str:
    """Become owner of the process-global hooks.

    With *strict*, raises :class:`ConcurrentCaptureRefused` instead of returning
    a participant token (or an owner token while a participant is still live).

    Returns an **owner** token (``own:``-prefixed) when this call took
    ownership, or a **participant** token (``par:``-prefixed) when another
    capture already owns them. Both are non-empty and both must be handed back
    to :func:`uninstall_all`; only the owner tears anything down.
    """
    global _hook_owner
    with _HOOK_OWNER_LOCK:
        if strict and (
            _hook_owner is not None
            or any(t.startswith(_PARTICIPANT_PREFIX) for t in _scope_bindings)
        ):
            raise ConcurrentCaptureRefused(
                "another capture is live in this process and strict capture "
                f"({STRICT_ENV_VAR}) refuses to produce a concurrent, "
                "possibly partial capsule; run captures one at a time or "
                "unset strict mode (ADR-0224 OQ-2)"
            )
        if _hook_owner is None:
            _hook_owner = _OWNER_PREFIX + uuid.uuid4().hex
            # A participant that outlived the previous owner is still running
            # and still bound. Its bare-thread events will fall back to *this*
            # owner's writer, so this owner is contended from the start.
            if any(t.startswith(_PARTICIPANT_PREFIX) for t in _scope_bindings):
                _contended_owners.add(_hook_owner)
            return _hook_owner
        # Someone already owns them: record that the owner's capsule may now
        # contain this capture's wire events (see wire_capture_state).
        _contended_owners.add(_hook_owner)
        return _PARTICIPANT_PREFIX + uuid.uuid4().hex


def _release_hook_ownership(token: str) -> bool:
    """Give up ownership if *token* holds it. True if the caller should tear down.

    Only an ``own:`` token that is still the live owner matches; an empty token,
    a participant token, or an owner token already released is a no-op here.
    """
    global _hook_owner
    with _HOOK_OWNER_LOCK:
        if (
            token
            and token.startswith(_OWNER_PREFIX)
            and _hook_owner is not None
            and _hook_owner == token
        ):
            _hook_owner = None
            return True
        return False


def _forget_contention(token: str) -> None:
    with _HOOK_OWNER_LOCK:
        _contended_owners.discard(token)


def current_hook_owner() -> str | None:
    """The live owner token, or None. Exposed for tests and diagnostics."""
    with _HOOK_OWNER_LOCK:
        return _hook_owner


def wire_capture_state(token: str) -> str:
    """The honest ``metadata.wire_capture`` value for a capture holding *token*.

    Five states, because "the stream is short" has five different causes and a
    reader must not have to guess which:

    - ``"installed"`` — this capture owned the hooks for its whole life and
      nothing else overlapped. The wire stream is this run's, and complete.
    - ``"installed-contended"`` — this capture owned the hooks, but another
      capture overlapped it. The patches are process-global, so events raised in
      a context that bound nothing — most importantly a **bare thread**, which
      inherits no context — still fall back to this capture's writer. The stream
      may therefore contain the other run's events. Complete, not exclusive.
    - ``"scoped-concurrent"`` — another capture owned the hooks, so this one
      installed none; but it bound its own recorder and writer, so events raised
      **in its own task** were filed into its own capsule through the owner's
      single patch layer. This is what ADR-0224 phase 2 added: the stream is this
      run's, and complete for everything the run did in its own context.
    - ``"scoped-truncated"`` — as ``scoped-concurrent``, but the owner finished
      first and removed the patch layer while this capture was still running.
      Everything up to that moment is this run's and complete; events after it
      were not captured unless another capture installed hooks again. The stream
      is this run's but **has a gap** (ADR-0224 D3 ▸ Amendment 3).
    - ``"skipped-concurrent"`` — no wire-level hooks and no binding. The
      adapter-level record is still complete; the wire stream is absent, not
      empty. Phase 1's outcome for the race loser, kept for callers that pass an
      empty token, and for a participant that could not bind.

    ``"installed-contended"`` deliberately still warns after phase 2. The
    narrowing is real but partial: a task-bound concurrent capture no longer
    cross-files, yet a thread it spawns inherits no binding and still resolves to
    the owner. An evidence system reports the residual rather than rounding it to
    zero.

    Read this **before** :func:`uninstall_all`, which forgets the contention
    record so it cannot accumulate across a long-lived process.
    """
    if not token:
        return "skipped-concurrent"
    if token.startswith(_PARTICIPANT_PREFIX):
        with _HOOK_OWNER_LOCK:
            bound = token in _scope_bindings
            truncated = token in _truncated_participants
        if not bound:
            # Installed nothing and bound nothing: say absent, not scoped.
            return "skipped-concurrent"
        return "scoped-truncated" if truncated else "scoped-concurrent"
    return "installed-contended" if owner_was_contended(token) else "installed"


def owner_was_contended(token: str) -> bool:
    """True if another capture overlapped *token*'s ownership.

    When true, the owner's wire-event stream may contain events produced by
    the concurrent capture, because the hooks patch process-global call sites.
    Callers should record this in their capsule rather than let a reader
    assume every event in the stream belongs to that run.
    """
    with _HOOK_OWNER_LOCK:
        return bool(token) and token in _contended_owners


# Map of (target SDK module, hook module path, hook class name).
# Order is significant for the layering guard:
#   - Per-SDK hooks first (richer records than wire-level)
#   - Wire-level higher-layer before lower (requests > urllib3) so the
#     layering guard in _layering.py prevents double-recording
#   - Plugin discovery happens separately, after all built-ins
_BUILT_IN_HOOKS: tuple[tuple[str, str, str], ...] = (
    ("openai",     "novafabric.capture.hooks._openai",     "OpenAIHook"),
    ("anthropic",  "novafabric.capture.hooks._anthropic",  "AnthropicHook"),
    ("mcp",        "novafabric.capture.hooks._mcp",        "MCPHook"),
    # ADR-0306: the `record.tool` capture sink. Its "SDK" is NovaFabric's own
    # façade module, so it is always available and always installed.
    ("novafabric.capture.record", "novafabric.capture.hooks._python_tool", "PythonToolHook"),
    ("requests",   "novafabric.capture.hooks._requests",   "RequestsHook"),
    ("httpx",      "novafabric.capture.hooks._httpx",      "HttpxHook"),
    ("aiohttp",    "novafabric.capture.hooks._aiohttp",    "AiohttpHook"),
    ("urllib3",    "novafabric.capture.hooks._urllib3",    "Urllib3Hook"),
)


def _is_sdk_available(sdk_module: str) -> bool:
    """Return True if ``sdk_module`` is importable WITHOUT importing it.

    Uses :func:`importlib.util.find_spec` to probe the import system
    for the module's spec without executing it.

    Originally introduced in v0.6.9 as a hot-path optimization with
    the hypothesis that find_spec would be substantially cheaper than
    full hook-module imports for absent SDKs. **The hypothesis was
    measured and disproven**: find_spec walks the same import-system
    machinery as a real import, so for the small hook modules in this
    package the saving is ~1.5 ms — within benchmark noise. See
    ``benchmarks/README.md`` for the methodology.

    The check is kept anyway because it makes intent explicit (check
    then import, vs try-then-recover) and avoids loading hook modules
    into ``sys.modules`` when they would no-op. Real subprocess-
    startup wins now require either a leaner novafabric package init
    chain or a sys.audit / sys.meta_path-based deferred install — both
    queued for v0.7.x architectural work.
    """
    try:
        return importlib.util.find_spec(sdk_module) is not None
    except (ImportError, ValueError):
        # ImportError: parent package is broken; ValueError: __spec__ is None.
        # Either way, the SDK isn't usable — treat as absent.
        return False


def _ensure_recorder(writer: "CapsuleWriter") -> None:
    """Set the module-level EventRecorder singleton from *writer* if unset.

    Wire-level hooks record NetworkEvent/FileEvent via
    ``get_current_recorder()`` rather than holding the writer directly, so the
    singleton must exist before any patched method can fire an event. This is
    essential wherever the hooks run in a different process or scope than the
    orchestrator that owns the recorder — the captured-workload subprocess
    (sitecustomize loader), the in-process SDK ``capture()`` wrapper, and the
    framework adapters. Without it the recorder is ``None`` in those contexts
    and all network/file events are silently dropped.

    Idempotent and fail-open: only sets the recorder when none exists (so an
    externally-owned recorder is never clobbered) and never lets a setup
    failure block hook installation.
    """
    global _recorder_set_by_install
    try:
        from novafabric.capture.event_recorder import (
            EventRecorder,
            get_process_recorder,
            set_current_recorder,
        )
        # The singleton, not the task-resolved recorder: a binding in the
        # calling task says nothing about what a bare thread will resolve, and
        # a stale one used to suppress the singleton entirely.
        if get_process_recorder() is None:
            _cap_dir = writer.capsule_dir
            _rec = EventRecorder(
                capsule_dir=_cap_dir,
                run_id=_cap_dir.name,
                capsule_id=_cap_dir.name,
            )
            set_current_recorder(_rec)
            _recorder_set_by_install = _rec
    except Exception:
        _log.debug("capture: could not set the recorder singleton", exc_info=True)


def _bind_scope(token: str, writer: "CapsuleWriter", *, own_recorder: bool) -> bool:
    """Bind *token*'s capture scope — recorder and writer — to the calling task.

    Both kinds of capture bind (ADR-0224 D3 ▸ Amendment 3). A **participant**
    must, or its events would reach the owner's capsule. The **owner** must too:
    a capture that wins the race inside a task where another capture is still
    bound — one nested in a participant that outlived the previous owner — would
    otherwise resolve that outer capture and file its events there.

    A participant gets a fresh recorder on its own capsule; the owner binds the
    process recorder ``_ensure_recorder`` just settled, so that the owner's own
    task and a bare thread resolve the same object.

    Returns True if a binding was made. Fail-open, like every other installation
    path: a capture that cannot bind degrades rather than failing the run it is
    observing, and ``wire_capture_state`` reports the degraded state honestly.
    """
    try:
        from novafabric.capture.event_recorder import (
            EventRecorder,
            bind_capture,
            get_process_recorder,
        )

        with _HOOK_OWNER_LOCK:
            if len(_scope_bindings) >= _MAX_LIVE_SCOPES:
                _log.warning(
                    "capture: %d capture scopes are live; not binding another — "
                    "a caller is not handing its install_all() token back",
                    len(_scope_bindings),
                )
                return False
        if own_recorder:
            cap_dir = writer.capsule_dir
            recorder: EventRecorder | None = EventRecorder(
                capsule_dir=cap_dir, run_id=cap_dir.name, capsule_id=cap_dir.name
            )
        else:
            recorder = get_process_recorder()
        handle = bind_capture(recorder=recorder, writer=writer)
        with _HOOK_OWNER_LOCK:
            _scope_bindings[token] = handle
        return True
    except Exception:
        _log.debug("capture: could not bind a capture scope", exc_info=True)
        return False


def _release_scope(token: str) -> bool:
    """Release *token*'s capture scope from any context. True if one was live."""
    with _HOOK_OWNER_LOCK:
        handle = _scope_bindings.pop(token, None)
        _truncated_participants.discard(token)
    if handle is None:
        return False
    try:
        from novafabric.capture.event_recorder import unbind_capture

        unbind_capture(handle)
    except Exception:
        _log.debug("capture: could not release a capture scope", exc_info=True)
    return True


def _mark_live_participants_truncated() -> int:
    """The patch layer is going while participants still run. Returns how many.

    Their bindings are deliberately **kept**. Revoking them — what phase 2 first
    did — would not stop their events, only redirect them: the next capture to
    install hooks would receive them through its own writer. Kept, a participant
    keeps filing into its own capsule whenever a patch layer exists, and its
    marker records the gap (``scoped-truncated``).
    """
    with _HOOK_OWNER_LOCK:
        live = [t for t in _scope_bindings if t.startswith(_PARTICIPANT_PREFIX)]
        _truncated_participants.update(live)
    return len(live)


def _release_every_scope() -> int:
    """Process reset: revoke every capture binding, in every context."""
    with _HOOK_OWNER_LOCK:
        _scope_bindings.clear()
        _truncated_participants.clear()
    try:
        from novafabric.capture.event_recorder import unbind_all_captures

        return unbind_all_captures()
    except Exception:
        _log.debug("capture: could not release capture scopes", exc_info=True)
        return 0


def _install_plugins(writer: "CapsuleWriter", parent_span_id: str) -> None:
    """Discover and install third-party hook plugins (entry-point group).

    Plugins discovered via the 'novafabric.hooks' entry-point group (RFC-0001
    §Detailed design — Option C, sub-track C-2). Each plugin is isolated: a
    buggy plugin must not break built-in capture. Plugin discovery is NOT
    skipped based on SDK availability — plugin authors opt in via the entry
    point and may have their own dependency-presence checks.
    """
    from novafabric.capture.hooks._plugin import install_discovered_plugins
    _installed.extend(
        install_discovered_plugins(writer=writer, parent_span_id=parent_span_id)
    )


def install_all(
    writer: "CapsuleWriter", parent_span_id: str, *, strict: bool | None = None
) -> str:
    """Install every built-in hook whose target SDK is importable.

    Returns an opaque, always non-empty **token**: an owner token when this call
    installed the hooks, or a participant token when another capture already
    owns them (nothing is installed, but this capture's own recorder and writer
    are bound to the calling task — ADR-0224 D3). Pass it to
    :func:`wire_capture_state` for the honest ``wire_capture`` marker, then
    straight back to :func:`uninstall_all`.

    That hand-back is safe in every case *by construction*: only the live owner
    token can tear the hooks down, so a capture that lost the race cannot tear
    down the one that won. Returning ``None`` for the loser would have been the
    obvious API and is a trap — ``uninstall_all(None)`` is the legacy
    unconditional teardown, so handing back the return value would have caused
    exactly the bug this guard exists to prevent.

    Hooks for absent SDKs are skipped entirely — the hook module is
    not even imported. The performance impact is small (see
    :func:`_is_sdk_available`); the main benefit is making intent
    explicit at the install-orchestration layer.

    ``strict`` (default: the ``NOVAFABRIC_CAPTURE_STRICT`` env var, else off)
    makes an overlapping capture raise :class:`ConcurrentCaptureRefused` before
    anything is installed. Off, behaviour is unchanged.

    Sets the EventRecorder singleton (see :func:`_ensure_recorder`) so the
    wire-level hooks can write their event streams.

    This is the **eager** path: present SDKs are imported now in order to be
    patched. For the import-deferred path that avoids importing unused SDKs at
    startup, see :func:`install_all_deferred` (``--fast-emit``).
    """
    # Strict mode (opt-in, default off — ADR-0224 OQ-2): refuse a capture that
    # would overlap another, instead of degrading to a scoped/contended capsule.
    token = _claim_hook_ownership(strict_capture_enabled(strict))
    if token.startswith(_PARTICIPANT_PREFIX):
        # Another capture owns the hooks. Installing anyway would stack a second
        # patch layer and file this capture's events into the owner's capsule
        # (ADR-0224 failure modes 1 and 2), so we install nothing.
        #
        # Phase 2: instead of recording nothing, bind this capture's own
        # recorder and writer to the calling task. The owner's single patch
        # layer resolves both when an event *fires*, so events raised in this
        # capture's context are filed into this capture's capsule.
        _bind_scope(token, writer, own_recorder=True)
        return token
    _ensure_recorder(writer)
    _bind_scope(token, writer, own_recorder=False)

    for sdk_module, hook_module_path, hook_class_name in _BUILT_IN_HOOKS:
        if not _is_sdk_available(sdk_module):
            continue
        try:
            hook_module = importlib.import_module(hook_module_path)
            hook_cls = getattr(hook_module, hook_class_name)
        except (ImportError, AttributeError):
            # Defensive: if the hook module itself is broken (shouldn't
            # happen with built-ins, but possible in dev), skip it
            # rather than poison the rest of capture.
            continue
        hook = hook_cls(writer=writer, parent_span_id=parent_span_id)
        hook.install()
        _installed.append(hook)

    _install_thread_propagation()
    _install_plugins(writer, parent_span_id)
    return token


def _install_thread_propagation() -> None:
    """Carry capture scopes into threads and pool items (ADR-0224 D3 ▸ Am. 4).

    Part of the owner's patch layer, so it lives and dies with it. Fail-open: a
    failure here leaves threads resolving the fallback, as before.
    """
    try:
        from novafabric.capture.hooks._threads import ThreadContextHook

        hook = ThreadContextHook()
        hook.install()
        _installed.append(hook)
    except Exception:
        _log.debug("capture: thread scope propagation not installed", exc_info=True)


#: What ``CapsuleWriter.open()`` creates and nothing else has touched yet.
_FRESH_CAPSULE_ENTRIES = frozenset(
    {"inputs", "outputs", "model-calls.jsonl", "tool-calls.jsonl", "trace.jsonl", "assets.jsonl"}
)


def _discard_unwritten_capsule(writer: "CapsuleWriter") -> None:
    """Remove a capsule directory that was opened but never written to.

    Only the exact skeleton ``CapsuleWriter.open()`` creates, with every file
    empty, is removed — anything else is left alone (capsules are append-only;
    this undoes our own just-made, empty directory, never evidence).
    """
    import shutil

    try:
        d = writer.capsule_dir
        entries = {p.name: p for p in d.iterdir()}
        if not set(entries) <= _FRESH_CAPSULE_ENTRIES:
            return
        for p in entries.values():
            if p.is_dir():
                if any(p.iterdir()):
                    return
            elif p.stat().st_size:
                return
        shutil.rmtree(d)
    except Exception:  # noqa: BLE001 — cleanup must not mask the refusal
        _log.debug("capture: could not discard the refused capsule", exc_info=True)


def install_all_or_discard(
    writer: "CapsuleWriter", parent_span_id: str, *, strict: bool | None = None
) -> str:
    """:func:`install_all` for adapters that have already opened *writer*.

    Identical, except that a strict-mode :class:`ConcurrentCaptureRefused` also
    removes the empty capsule directory the caller just opened, so a refused
    capture leaves no trace on disk (as well as no hook state — the refusal is
    raised before anything is installed). The refusal is re-raised unchanged.
    """
    try:
        if strict is None:
            return install_all(writer=writer, parent_span_id=parent_span_id)
        return install_all(writer=writer, parent_span_id=parent_span_id, strict=strict)
    except ConcurrentCaptureRefused:
        _discard_unwritten_capsule(writer)
        raise


def install_all_deferred(writer: "CapsuleWriter", parent_span_id: str) -> None:
    """Install built-in hooks *lazily*, triggered by the workload's own imports.

    Unlike :func:`install_all`, this does **not** import any target SDK at
    startup. For each built-in hook it registers a one-shot ``sys.meta_path``
    post-import callback (see :mod:`novafabric.capture.hooks._deferred`) that
    fires only if/when the workload imports that SDK — at which point the SDK
    is already loaded (the workload's own cost) and patching adds ~nothing.

    This eliminates "startup cost #2" — the ~700 ms (openai) / ~340 ms (mcp)
    paid at interpreter startup by the eager path to import SDKs purely to patch
    them, even when the workload never calls them. It is the lazy-import path of
    the warm-capture-daemon design (ADR-0092 slice B).

    Fidelity is preserved: when a hook *does* install (its SDK is imported), it
    is the identical hook class, and the EventRecorder singleton is set lazily
    in the callback before the patched method can fire an event. SDKs already
    imported when this runs are patched immediately (handled by
    :func:`~novafabric.capture.hooks._deferred.register_post_import_hook`).
    Plugin discovery still runs eagerly (small, opt-in, no uniform target SDK
    to gate on).
    """
    from novafabric.capture.hooks._deferred import register_post_import_hook

    def _make_callback(hook_module_path: str, hook_class_name: str) -> object:
        def _on_import(_module: object) -> None:
            # Recorder must exist before the patched method can fire an event.
            _ensure_recorder(writer)
            try:
                hook_module = importlib.import_module(hook_module_path)
                hook_cls = getattr(hook_module, hook_class_name)
            except (ImportError, AttributeError):
                return
            hook = hook_cls(writer=writer, parent_span_id=parent_span_id)
            hook.install()
            _installed.append(hook)
        return _on_import

    for sdk_module, hook_module_path, hook_class_name in _BUILT_IN_HOOKS:
        register_post_import_hook(
            sdk_module,
            _make_callback(hook_module_path, hook_class_name),  # type: ignore[arg-type]
        )

    _install_plugins(writer, parent_span_id)


def uninstall_all(token: str | None = None) -> bool:
    """Tear down the hooks. Returns True if a teardown actually happened.

    ``token`` is what :func:`install_all` returned. When given, the teardown
    only happens if that token still owns the hooks — so a capture that lost
    the race, or one that already released, cannot tear down a capture still
    in flight.

    ``token=None`` keeps the historical unconditional behaviour, which is
    correct **only** where exactly one capture exists per process: the
    subprocess sitecustomize loader and the orchestrator. Every in-process
    caller (SDK wrapper, framework adapters) must pass its token.

    Every token's capture scope is released here, from whatever context this
    runs in — the release is visible in the binding context too
    (ADR-0224 D3 ▸ Amendment 3). The legacy ``token=None`` path releases every
    scope in the process.
    """
    global _recorder_set_by_install
    if token:
        # A participant installed nothing, so it tears nothing down — but both
        # kinds bound a capture scope, and it must be released or a finished
        # capture's writer stays bound for the rest of its task.
        _release_scope(token)
    if token is not None and token.startswith(_PARTICIPANT_PREFIX):
        return False
    if token is not None and not _release_hook_ownership(token):
        return False
    if token:
        # Bounded: the contention record exists so the owner can mark its own
        # capsule, and that read has happened by now (see wire_capture_state).
        _forget_contention(token)
        # The patch layer is going while participants may still run: record
        # their gap, keep their bindings (see the function for why).
        _mark_live_participants_truncated()
    if token is None:
        # Legacy unconditional path: drop any live ownership so the next
        # capture in this process can claim the hooks, and every binding with it.
        _release_hook_ownership(current_hook_owner() or "")
        _release_every_scope()
    for hook in _installed:
        try:
            hook.uninstall()  # type: ignore[attr-defined]
        except Exception:
            pass
    _installed.clear()
    # Clear the recorder only if install_all() set it (in-process SDK / adapter
    # paths). When an orchestrator owns the recorder it clears its own.
    if _recorder_set_by_install is not None:
        try:
            from novafabric.capture.event_recorder import clear_current_recorder
            clear_current_recorder(_recorder_set_by_install)  # type: ignore[arg-type]
        except Exception:
            pass
        finally:
            _recorder_set_by_install = None
    return True
