"""ADR-0231 — the dashboard audit record says who, what changed, and what was refused.

Three things were missing and none of them was about transport: there was no
actor (one shared token, so every record carried the same fingerprint), no state
transition (the request args, never before-and-after), and no denials.

The constraint that shapes all of it is ``DASHBOARD_FIELD_ALLOWLIST``:
deny-by-default, so a field added to the writer and not to the allowlist is
**silently omitted from every export** while the local file looks enriched and
nothing errors. That divergence is what these tests exist to make impossible.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from novafabric.audit.siem_export import (
    DASHBOARD_FIELD_ALLOWLIST,
    to_ocsf,
)
from novafabric.serve import audit

REPO = Path(__file__).resolve().parents[2]
WRITER = REPO / "src" / "novafabric" / "serve" / "audit.py"


@pytest.fixture
def audit_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "dashboard-audit.jsonl"
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(path))
    yield path


def _records(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# D5 — the allowlist and the writer must not diverge
# ---------------------------------------------------------------------------


def _keys_the_writer_assigns() -> set[str]:
    """Every ``record[...] = ...`` key in ``serve/audit.py``, read from the AST.

    Derived from the source rather than listed by hand on purpose. ADR-0196 D4
    added a guard of this kind against a hand-written literal, whose own comment
    claimed it was discovered by import; it went stale and the defect it guarded
    recurred. **A hand-maintained mirror of code is the thing that drifts.**
    """
    tree = ast.parse(WRITER.read_text(encoding="utf-8"))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "record"
                    and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)
                ):
                    keys.add(target.slice.value)
        # The literal the record is constructed from.
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    continue
    # Plus the constructor literal, which assigns rather than subscripts.
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "record" and isinstance(node.value, ast.Dict):
                for key in node.value.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        keys.add(key.value)
    return keys


def test_the_writers_declared_field_set_matches_what_it_actually_assigns() -> None:
    """``EMITTABLE_FIELDS`` is a claim about the writer; check it against the writer."""
    assigned = _keys_the_writer_assigns()
    assert assigned, "AST walk found no record keys — the guard is blind, not passing"
    missing = assigned - audit.EMITTABLE_FIELDS
    assert not missing, (
        f"serve/audit.py writes {sorted(missing)} but EMITTABLE_FIELDS does not declare them"
    )


def test_every_emittable_field_survives_the_export_allowlist() -> None:
    """D5. Deny-by-default means an undeclared field is dropped, silently."""
    dropped = audit.EMITTABLE_FIELDS - DASHBOARD_FIELD_ALLOWLIST
    assert not dropped, (
        f"the dashboard writer can emit {sorted(dropped)}, which DASHBOARD_FIELD_ALLOWLIST "
        "omits — those fields would be silently missing from every SIEM export while the "
        "local log looked enriched. Add them to the allowlist in the same change."
    )


def test_the_allowlist_does_not_promise_fields_the_writer_never_emits() -> None:
    """The converse: an allowlist entry for a field nobody writes is dead weight."""
    orphans = DASHBOARD_FIELD_ALLOWLIST - audit.EMITTABLE_FIELDS
    assert not orphans, f"allowlisted but never written: {sorted(orphans)}"


# ---------------------------------------------------------------------------
# D3 — the record states how well it knows the actor
# ---------------------------------------------------------------------------


def test_a_shared_token_record_carries_no_actor_id(audit_file: Path) -> None:
    """With one shared token every record has the same fingerprint. Say so."""
    audit.append(action="x", args={}, cli_equivalent="", actor_token_fp="ab12")
    record = _records(audit_file)[0]
    assert record["actor"] == {"identity_source": "shared-token"}
    assert "id" not in record["actor"], (  # type: ignore[operator]
        "an id key — even null — invites a SIEM to render a user column as if it meant something"
    )


def test_a_credential_record_carries_its_id(audit_file: Path) -> None:
    audit.append(
        action="x",
        args={},
        cli_equivalent="",
        actor_token_fp="ab12",
        identity_source="credential",
        actor_id="fp:deadbeef",
    )
    assert _records(audit_file)[0]["actor"] == {
        "identity_source": "credential",
        "id": "fp:deadbeef",
    }


def test_an_actor_id_is_refused_for_a_shared_token(audit_file: Path) -> None:
    """A caller cannot smuggle a strong-looking id under a weak identity source."""
    audit.append(
        action="x",
        args={},
        cli_equivalent="",
        actor_token_fp="ab12",
        identity_source="shared-token",
        actor_id="alice@example.org",
    )
    assert "id" not in _records(audit_file)[0]["actor"]  # type: ignore[operator]


def test_an_unknown_identity_source_degrades_to_the_weakest(audit_file: Path) -> None:
    """Failing open here would mean overstating evidence, so it fails closed."""
    audit.append(
        action="x", args={}, cli_equivalent="", actor_token_fp="ab12", identity_source="wishful"
    )
    assert _records(audit_file)[0]["actor"] == {"identity_source": "shared-token"}


def test_ocsf_never_presents_a_token_fingerprint_as_a_user_name() -> None:
    """The defect this ADR names, which was live in shipped code.

    A SIEM renders ``actor.user.name`` in a column headed *User* and an analyst
    reads it as a person. A shared-token fingerprint is not a person.
    """
    record = {
        "ts": "2026-09-06T00:00:00+00:00",
        "action": "delete_run",
        "actor_token_fp": "ab12cd34",
        "actor": {"identity_source": "shared-token"},
    }
    event = to_ocsf(record, source="dashboard")
    user = event["actor"]["user"]
    assert user.get("name") is None, "a fingerprint must never land in user.name"
    assert user["uid"] == "ab12cd34", "it is an opaque id, and belongs in uid"
    assert user["type_id"] == 0, "Unknown — the honest OCSF value for a session credential"
    assert event["actor"]["nova_identity_source"] == "shared-token"


def test_ocsf_names_the_user_only_for_an_identity_beyond_the_credential() -> None:
    """A federated subject is a person; a credential fingerprint is not."""
    record = {
        "ts": "2026-09-06T00:00:00+00:00",
        "action": "delete_run",
        "actor_token_fp": "ab12cd34",
        "actor": {"identity_source": "federated", "id": "alice@example.org"},
    }
    user = to_ocsf(record, source="dashboard")["actor"]["user"]
    assert user["name"] == "alice@example.org"
    assert user["type_id"] == 1


def test_ocsf_does_not_restate_a_token_fingerprint_as_a_user_name() -> None:
    """An issued token is stronger evidence than a shared one — still not a person.

    `authz` passes the token's own fingerprint as the actor id, so without this
    rule `user.name` would carry the fingerprint again under a human-sounding
    key: a quieter version of the very overstatement this mapping removes.
    """
    record = {
        "ts": "2026-09-06T00:00:00+00:00",
        "action": "delete_run",
        "actor_token_fp": "ab12cd34",
        "actor": {"identity_source": "credential", "id": "ab12cd34"},
    }
    user = to_ocsf(record, source="dashboard")["actor"]["user"]
    assert user.get("name") is None
    assert user["uid"] == "ab12cd34"
    assert user["type_id"] == 1, "an issued credential is a known actor type, unlike a shared token"


def test_the_chained_audit_source_mapping_is_untouched() -> None:
    """Its `actor` is a real subject already; changing it would be a regression."""
    record = {"timestamp": "2026-09-06T00:00:00+00:00", "event_type": "x", "actor": "alice"}
    assert to_ocsf(record, source="audit")["actor"] == {"user": {"name": "alice"}}


# ---------------------------------------------------------------------------
# D2 — before and after, redacted, with proof
# ---------------------------------------------------------------------------


def test_state_transition_is_recorded(audit_file: Path) -> None:
    audit.append(
        action="assign_role",
        args={"subject": "alice"},
        cli_equivalent="",
        actor_token_fp="ab12",
        resource="role_assignment:alice",
        prior={"roles": ["reader"]},
        current={"roles": ["reader", "admin"]},
    )
    record = _records(audit_file)[0]
    assert record["prior"] == {"roles": ["reader"]}
    assert record["current"] == {"roles": ["reader", "admin"]}
    assert record["resource"] == "role_assignment:alice"


def test_captured_state_is_redacted_and_says_so(audit_file: Path) -> None:
    """Append-only + unrotated + captured state = a permanent secret, unless this runs."""
    audit.append(
        action="update_config",
        args={},
        cli_equivalent="",
        actor_token_fp="ab12",
        prior={"password": "hunter2", "host": "db"},
        current={"password": "hunter3", "host": "db"},
    )
    record = _records(audit_file)[0]
    assert "hunter2" not in json.dumps(record)
    assert "hunter3" not in json.dumps(record)
    assert record["redaction"]["applied"] is True  # type: ignore[index]
    assert record["redaction"]["findings"] == ["redacted"]  # type: ignore[index]
    assert record["redaction"]["ruleset"].startswith("adr0187-")  # type: ignore[index]


def test_the_recorded_ruleset_version_comes_from_the_ruleset() -> None:
    """A version string copied into a second file attests to a version that may not have run."""
    from novafabric.support_bundle._redact import REDACTION_RULESET_VERSION

    assert audit._redaction_ruleset() == f"adr0187-{REDACTION_RULESET_VERSION}"


def test_no_state_means_no_redaction_block(audit_file: Path) -> None:
    """A `redaction: applied` block over nothing is a claim about nothing."""
    audit.append(action="x", args={}, cli_equivalent="", actor_token_fp="ab12")
    assert "redaction" not in _records(audit_file)[0]


#: Actions that supply before/after state today. ADR-0231 D2 asks for every
#: mutating handler; this slice wired the role surface, which is the ADR's own
#: worked example and what ADR-0228 made security-critical. Stated explicitly so
#: **partial coverage cannot read as complete** — the rest is deferred, not done.
ACTIONS_WITH_STATE_CAPTURE = frozenset({"assign_role", "revoke_role"})


def test_the_state_capture_coverage_is_stated_not_implied() -> None:
    """If a handler gains or loses state capture, this list must move with it."""
    source = (REPO / "src" / "novafabric" / "serve" / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    with_state: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "append":
            continue
        kwargs = {kw.arg for kw in node.keywords}
        if not {"prior", "current"} & kwargs:
            continue
        for kw in node.keywords:
            if kw.arg == "action" and isinstance(kw.value, ast.Constant):
                with_state.add(str(kw.value.value))
    assert with_state == ACTIONS_WITH_STATE_CAPTURE, (
        f"state-capturing actions in serve/app.py are {sorted(with_state)}, but this test "
        f"declares {sorted(ACTIONS_WITH_STATE_CAPTURE)}. Update the list — and the ADR's "
        "implementation status — so partial coverage never reads as complete."
    )
