"""Concurrent in-process captures must not corrupt each other (ADR-0224).

`capture.hooks` keeps one module-level `_installed` list and one `EventRecorder`
singleton, with no per-task scoping. Two concurrent in-process captures — which
seven of the eight framework adapters and the SDK wrapper could all produce —
collided three ways, all reproduced before the fix:

1. the second `install_all()` left the FIRST capture's recorder in place, so
   capture B's events were filed into capture A's capsule;
2. the second `install_all()` stacked a second patch layer (6 hooks -> 12), so
   an event could be recorded twice;
3. whichever capture finished first ran `uninstall_all()` and tore down *both*,
   leaving the still-running capture with no hooks and no recorder.

Only `a2a.py` had a guard. These tests pin the shared one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from novafabric.capture import hooks
from novafabric.capture.event_recorder import get_current_recorder, set_current_recorder


class _FakeWriter:
    def __init__(self, capsule_dir: Path) -> None:
        self.capsule_dir = capsule_dir


@pytest.fixture(autouse=True)
def _clean_hook_state(tmp_path: Path):
    """These tests manipulate process-global state; leave it as found."""
    hooks.uninstall_all()
    set_current_recorder(None)
    hooks._contended_owners.clear()
    yield
    hooks.uninstall_all()
    set_current_recorder(None)
    hooks._contended_owners.clear()


def _writer(tmp_path: Path, name: str) -> _FakeWriter:
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    return _FakeWriter(d)


def _recorder_capsule() -> str | None:
    rec = get_current_recorder()
    return rec._capsule_dir.name if rec else None


def test_first_capture_wins_and_gets_a_token(tmp_path: Path) -> None:
    token = hooks.install_all(writer=_writer(tmp_path, "run-A"), parent_span_id="A")
    assert token, "the first capture must own the hooks"
    assert hooks.current_hook_owner() == token
    assert _recorder_capsule() == "run-A"


def test_second_concurrent_capture_does_not_stack_a_second_patch_layer(
    tmp_path: Path,
) -> None:
    """Failure mode 2: 6 hooks became 12, so events could be recorded twice."""
    hooks.install_all(writer=_writer(tmp_path, "run-A"), parent_span_id="A")
    layers_after_a = len(hooks._installed)
    assert layers_after_a > 0

    token_b = hooks.install_all(writer=_writer(tmp_path, "run-B"), parent_span_id="B")
    # ADR-0224 phase 2 (2026-08-29): the loser's token is no longer `""`. It is
    # a *participant* token — it owns no hooks (asserted next) but does own a
    # capture-scope binding it must later release, which an empty token could
    # never carry. What must not change is that no second layer is stacked.
    assert not hooks.current_hook_owner() == token_b, "B must not own the hooks"
    assert hooks.wire_capture_state(token_b) == "scoped-concurrent"
    assert len(hooks._installed) == layers_after_a, "a second patch layer was stacked"


def test_second_capture_does_not_redirect_the_recorder(tmp_path: Path) -> None:
    """Failure mode 1 — the evidence-integrity one.

    Phase 1 fixed this by making B record nothing: the process-wide singleton
    stayed A's. Phase 2 fixes it properly — B binds its **own** recorder to its
    own task, so B's events go to B's capsule instead of being dropped, while
    A's singleton is still untouched. Both halves are asserted, because "B no
    longer corrupts A" and "B is now recorded" are different claims.
    """
    # Read the singleton through the module: `from ... import _current_recorder`
    # binds the value at import time and would not see a later reassignment.
    from novafabric.capture import event_recorder as _er  # noqa: PLC0415

    hooks.install_all(writer=_writer(tmp_path, "run-A"), parent_span_id="A")
    token_b = hooks.install_all(writer=_writer(tmp_path, "run-B"), parent_span_id="B")

    # A's process-wide singleton is intact — B never redirected it.
    assert _er._current_recorder is not None
    assert _er._current_recorder._capsule_dir.name == "run-A"
    # ...and B, in its own task, now resolves its own recorder rather than A's.
    assert _recorder_capsule() == "run-B"

    hooks.uninstall_all(token_b)
    # Once B releases, this task resolves A's singleton again.
    assert _recorder_capsule() == "run-A"


def test_the_loser_cannot_tear_down_the_winner(tmp_path: Path) -> None:
    """Failure mode 3, and the reason the loser's token is `""` and not None:
    handing back whatever `install_all` returned must be safe either way."""
    token_a = hooks.install_all(writer=_writer(tmp_path, "run-A"), parent_span_id="A")
    token_b = hooks.install_all(writer=_writer(tmp_path, "run-B"), parent_span_id="B")

    assert hooks.uninstall_all(token_b) is False
    assert len(hooks._installed) > 0, "B tore down A's hooks"
    # B's binding is released by that call, so this task sees A's singleton.
    assert _recorder_capsule() == "run-A", "B cleared A's recorder"
    assert hooks.current_hook_owner() == token_a

    assert hooks.uninstall_all(token_a) is True
    assert hooks._installed == []
    assert hooks.current_hook_owner() is None


def test_none_is_the_legacy_unconditional_teardown(tmp_path: Path) -> None:
    """`uninstall_all()` with no token keeps its historical meaning, for the
    single-capture-per-process paths (subprocess sitecustomize, orchestrator).
    This is exactly why the loser's token must not be None."""
    hooks.install_all(writer=_writer(tmp_path, "run-A"), parent_span_id="A")
    assert hooks.uninstall_all() is True
    assert hooks._installed == []
    assert hooks.current_hook_owner() is None, "legacy teardown must release ownership too"


def test_ownership_is_reusable_after_release(tmp_path: Path) -> None:
    """Sequential captures in one process must each get the hooks — a guard
    that leaked ownership would silently disable wire capture forever after."""
    for name in ("run-A", "run-B", "run-C"):
        token = hooks.install_all(writer=_writer(tmp_path, name), parent_span_id=name)
        assert token, f"{name} failed to claim the hooks after the previous release"
        assert hooks.uninstall_all(token) is True
        set_current_recorder(None)


def test_contention_is_recorded_for_the_owner(tmp_path: Path) -> None:
    """The residual risk the guard does NOT remove: the hooks are process-global
    patches holding the owner's writer, so while two captures overlap the
    non-owner's traffic is still recorded into the OWNER's capsule. That must be
    detectable, not inferable from a suspiciously busy stream."""
    token_a = hooks.install_all(writer=_writer(tmp_path, "run-A"), parent_span_id="A")
    assert hooks.owner_was_contended(token_a) is False

    hooks.install_all(writer=_writer(tmp_path, "run-B"), parent_span_id="B")
    assert hooks.owner_was_contended(token_a) is True, (
        "A's capsule may now contain B's wire events and nothing says so"
    )


def test_an_uncontended_owner_is_not_flagged(tmp_path: Path) -> None:
    token = hooks.install_all(writer=_writer(tmp_path, "run-A"), parent_span_id="A")
    hooks.uninstall_all(token)
    assert hooks.owner_was_contended(token) is False


def test_empty_token_never_owns_anything() -> None:
    """The invariant that makes `uninstall_all(install_all(...))` safe."""
    assert hooks._release_hook_ownership("") is False
    assert hooks.owner_was_contended("") is False
