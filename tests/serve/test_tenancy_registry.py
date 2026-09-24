"""ADR-0229 D1 — the tenancy declaration is checked against real schema.

The point of these tests is that they can **disagree with the table**. ADR-0229's
own Consequences name declaration drift as the risk ("the declaration must be
asserted in tests against actual schema, not merely written down"), so a test
that read ``STORE_TENANCY`` back and asserted it equalled itself would be
vacuous — and this repository has shipped exactly that shape before.

So each store's module is read, and the claim is checked against its contents:
an ``aware`` store must really carry its discriminator; an ``unsafe`` store must
really carry none.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path

import pytest

from novafabric.serve.tenancy import (
    ENV_TENANCY_MODE,
    STORE_TENANCY,
    MultiTenancyUnavailableError,
    TenancyClass,
    TenancyMode,
    assert_multi_tenant_ready,
    blocking_stores,
    select_tenant,
    tenancy_mode,
    tenancy_posture,
)

#: Tokens that indicate a store filters rows/objects by tenant. Deliberately
#: excludes the ADR-0243 per-tenant KEK vocabulary: that is tenancy of key
#: material, and a store can hold per-tenant keys while being entirely unable to
#: answer "show me only tenant X's rows". Conflating the two would have
#: misclassified object_capsule_store for the right word and the wrong reason.
_SCOPING_TOKENS = ("tenant_id", "tenant ", "tenant\t", "<tenant>", "{tenant}", "TENANT_SCOPED")
_KEK_TOKENS = ("TenantKeyRegistry", "TENANT_KEK", "tenant_keys", "per-tenant KEK")


def _module_source(dotted: str) -> str:
    module = importlib.import_module(dotted)
    return Path(inspect.getfile(module)).read_text(encoding="utf-8")


def _scoping_hits(source: str) -> list[str]:
    """Lines that evidence row/object scoping, with KEK-only lines removed."""
    hits = []
    for line in source.splitlines():
        if not any(tok in line for tok in _SCOPING_TOKENS):
            continue
        if any(tok in line for tok in _KEK_TOKENS):
            continue
        hits.append(line.strip())
    return hits


@pytest.mark.parametrize("name", sorted(STORE_TENANCY))
def test_every_declaration_names_an_importable_module(name: str) -> None:
    decl = STORE_TENANCY[name]
    assert _module_source(decl.module), f"{name}: {decl.module} has no source"


@pytest.mark.parametrize(
    "name", sorted(n for n, d in STORE_TENANCY.items() if d.tenancy is TenancyClass.aware)
)
def test_an_aware_store_really_carries_its_discriminator(name: str) -> None:
    """`aware` is a claim about the schema, and the schema is what is checked."""
    decl = STORE_TENANCY[name]
    assert decl.discriminator is not None, f"{name}: declared aware with no discriminator"
    source = _module_source(decl.module)
    # The discriminator may be written as a template (capsules/{tenant}); check
    # its literal stem so the assertion tracks the real key format.
    stem = decl.discriminator.split("{")[0].rstrip("/") or decl.discriminator
    assert stem in source, (
        f"{name} is declared tenant-aware with discriminator {decl.discriminator!r}, "
        f"but {decl.module} does not contain {stem!r} — the declaration has drifted "
        "from the store."
    )


@pytest.mark.parametrize(
    "name", sorted(n for n, d in STORE_TENANCY.items() if d.tenancy is TenancyClass.unsafe)
)
def test_an_unsafe_store_really_has_no_scoping_discriminator(name: str) -> None:
    """The converse claim, and the one that actually protects anybody.

    If a store declared ``unsafe`` grows a tenant column, this fails and the
    declaration gets promoted — rather than the store quietly becoming scopeable
    while the dashboard keeps refusing to use it.
    """
    decl = STORE_TENANCY[name]
    assert decl.discriminator is None, f"{name}: unsafe stores have no discriminator"
    hits = _scoping_hits(_module_source(decl.module))
    assert not hits, (
        f"{name} is declared tenant-unsafe, but {decl.module} appears to carry a tenant "
        f"discriminator: {hits[:3]}. Promote it to `aware` (and scope the reads) or "
        "correct the evidence."
    )


@pytest.mark.parametrize("name", sorted(STORE_TENANCY))
def test_unsafe_stores_carry_an_operator_readable_reason(name: str) -> None:
    """D2's whole value is the reason; an unavailable panel with no why is noise."""
    decl = STORE_TENANCY[name]
    if decl.tenancy is TenancyClass.unsafe:
        assert decl.reason, f"{name}: unsafe with no reason"
        assert len(decl.reason) > 40, f"{name}: reason is too thin to act on"
    else:
        assert decl.reason is None, f"{name}: only unsafe stores carry a refusal reason"


def test_the_three_classes_are_all_represented() -> None:
    """A registry that had collapsed to one class would pass every test above."""
    classes = {decl.tenancy for decl in STORE_TENANCY.values()}
    assert classes == set(TenancyClass), (
        "the registry no longer distinguishes all three classes — agnostic and unsafe "
        "both mean 'does not filter by tenant' and have opposite safety consequences"
    )


# ---------------------------------------------------------------------------
# D2 — refuse the deployment, do not serve it unscoped
# ---------------------------------------------------------------------------


def test_single_tenant_is_the_default_and_never_refuses() -> None:
    assert tenancy_mode({}) is TenancyMode.single
    assert_multi_tenant_ready({})  # must not raise


@pytest.mark.parametrize("value", ["", "single", "SINGLE", "typo", "1", "true"])
def test_only_the_exact_multi_value_opts_in(value: str) -> None:
    """A typo must resolve to the restrictive mode, which is also today's behaviour."""
    assert tenancy_mode({ENV_TENANCY_MODE: value}) is TenancyMode.single
    assert_multi_tenant_ready({ENV_TENANCY_MODE: value})


def test_multi_tenant_is_refused_while_a_store_is_unsafe() -> None:
    with pytest.raises(MultiTenancyUnavailableError) as excinfo:
        assert_multi_tenant_ready({ENV_TENANCY_MODE: "multi"})
    message = str(excinfo.value)
    for name in blocking_stores():
        assert name in message, f"{name} blocks multi-tenant mode but is not named"
    assert "would return one tenant's evidence to another" in message


def test_the_refusal_names_runs_cache_not_only_the_obvious_two() -> None:
    """The reason this slice refuses at startup rather than per endpoint.

    Per-endpoint 503 would refuse the KG and lineage panels and go on serving
    `/api/runs` — backed by the equally unsafe runs index — which would read as
    "the rest is scoped". If runs_cache ever leaves this list, revisit D2.
    """
    assert "runs_cache" in blocking_stores()
    assert {"knowledge_graph", "lineage_store"} <= set(blocking_stores())


# ---------------------------------------------------------------------------
# D4 — a selector narrows, never widens
# ---------------------------------------------------------------------------


def test_an_unbound_credential_passes_the_request_through() -> None:
    """Single-tenant default: no set, no narrowing, no change in behaviour."""
    assert select_tenant(None, None) is None
    assert select_tenant("anything", None) == "anything"


def test_a_selector_inside_the_authorized_set_is_honoured() -> None:
    assert select_tenant("blue", frozenset({"blue", "green"})) == "blue"


def test_a_selector_outside_the_authorized_set_is_refused_not_ignored() -> None:
    """Silently ignoring it would leave the caller believing they had narrowed."""
    with pytest.raises(PermissionError, match="never widen"):
        select_tenant("red", frozenset({"blue", "green"}))


def test_a_single_tenant_credential_resolves_without_a_selector() -> None:
    assert select_tenant(None, frozenset({"blue"})) == "blue"


def test_a_multi_tenant_credential_without_a_selector_stays_unnarrowed() -> None:
    assert select_tenant(None, frozenset({"blue", "green"})) is None


def test_an_empty_authorized_set_selects_nothing() -> None:
    """An empty set is not "unrestricted" — that is what None means."""
    with pytest.raises(PermissionError, match="empty tenant set"):
        select_tenant("blue", frozenset())
    with pytest.raises(PermissionError, match="empty tenant set"):
        select_tenant(None, frozenset())


# ---------------------------------------------------------------------------
# Posture reporting
# ---------------------------------------------------------------------------


def test_posture_reports_every_store_in_both_modes() -> None:
    posture = tenancy_posture({})
    assert posture["mode"] == "single"
    assert posture["multi_tenant_ready"] is False
    assert set(posture["stores"]) == set(STORE_TENANCY)
    assert posture["blocking"] == list(blocking_stores())


def test_posture_is_honest_about_readiness_rather_than_about_the_mode() -> None:
    """Single-tenant mode is safe; it is not evidence that multi-tenant would be."""
    assert tenancy_posture({})["multi_tenant_ready"] is False


# ---------------------------------------------------------------------------
# The refusal and the posture, as behaviour rather than as a function call
# ---------------------------------------------------------------------------


def test_nova_serve_refuses_to_start_in_multi_tenant_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The refusal must reach the operator through the CLI, not just the helper.

    Asserted end-to-end because the failure it prevents is silent: a server that
    has already bound a socket is one an operator will assume is correct.
    """
    pytest.importorskip("fastapi")
    from typer.testing import CliRunner

    from novafabric.cli.main import app as cli_app

    monkeypatch.setenv(ENV_TENANCY_MODE, "multi")
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))

    # Stub the server so this test cannot start one. Without it, deleting the
    # refusal makes the CLI fall through to uvicorn.run and the test *hangs*
    # rather than failing — which is how it behaved the first time the guard was
    # red-green checked. A guard that hangs when its subject is removed is worse
    # than one that fails: the 300 s pytest-timeout eventually names it, four
    # minutes late, in the tier that is supposed to be fast.
    started: list[bool] = []

    def _refuse_to_serve(*_args: object, **_kwargs: object) -> None:
        started.append(True)

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", _refuse_to_serve)

    result = CliRunner().invoke(
        cli_app, ["serve", "--experimental", "--capsule-dir", str(tmp_path)]
    )
    assert not started, "the CLI reached uvicorn.run despite multi-tenant being unavailable"
    assert result.exit_code == 2, result.output
    assert "multi-tenant" in result.output.lower()
    # It must name what to fix, not merely assert a posture.
    assert "runs_cache" in result.output or "runs" in result.output


def test_nova_serve_is_unaffected_when_the_variable_is_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """D5: single-tenant is the default and reaches the normal start path."""
    pytest.importorskip("fastapi")
    monkeypatch.delenv(ENV_TENANCY_MODE, raising=False)
    assert_multi_tenant_ready()  # must not raise


def test_doctor_reports_the_tenancy_posture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """D1's stated benefit — the gaps become enumerated rather than implicit."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from novafabric.serve.app import create_app

    monkeypatch.delenv(ENV_TENANCY_MODE, raising=False)
    capsules = tmp_path / "runs"
    capsules.mkdir()
    token = "doctor-token-0123456789abcdef"
    app = create_app(token=token, capsule_dir=capsules, static_mounted_by_caller=True)
    with TestClient(app) as client:
        body = client.get(
            f"/api/doctor?token={token}", headers={"host": "127.0.0.1:4321"}
        ).json()

    checks = {check["name"]: check for check in body["checks"]}
    assert "tenancy_posture" in checks, "the posture must be visible in doctor output"
    check = checks["tenancy_posture"]
    assert check["ok"] is True, "single-tenant is a correct posture, not a failure"
    assert check["tenancy"]["multi_tenant_ready"] is False
    for name in blocking_stores():
        assert name in check["detail"], f"{name} is not named in the doctor detail"
