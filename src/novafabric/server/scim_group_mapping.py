"""SCIM Group → RBAC role mapping (ADR-0139 D3).

Config-driven: the operator declares which IdP group ``displayName`` maps to which
of the six ADR-0018 / ADR-0058 RBAC roles. No new role vocabulary is invented.

Semantics (ADR-0139 D3):

* A user provisioned into a **mapped** group receives that role.
* Removal from the group revokes it.
* **Unmapped** groups are accepted but grant no role (SCIM must not reject valid
  resources; they simply contribute nothing — never a default role).
* Maker-checker Separation of Duties (ADR-0058) is preserved: resolution may
  legitimately grant both ``promoter`` and ``approver`` to one subject; the SoD
  constraint is enforced per-*proposal* elsewhere, not by refusing the grant.

This module is a pure, side-effect-free resolver plus one audit-emitting helper.
It does not itself gate any request — role *enforcement* remains in
``novafabric.server.rbac``. Wiring the SCIM Group HTTP routes to call this is a
separate, reviewed slice.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

#: The six RBAC roles a SCIM group may map to. The four ADR-0018 roles
#: (reader/writer/admin/auditor) plus the ADR-0058 maker-checker SoD roles
#: (promoter/approver). No new roles are introduced here (ADR-0139 D3).
VALID_SCIM_ROLES: frozenset[str] = frozenset(
    {"reader", "writer", "admin", "auditor", "promoter", "approver"}
)


class UnknownRoleError(ValueError):
    """Raised when a group→role config entry names a role outside the six."""


def _normalize_group(display_name: str) -> str:
    """Canonical form for matching an IdP group ``displayName``.

    Only surrounding whitespace is stripped — matching is otherwise exact and
    case-sensitive, so a mapping never fires on an unintended group.
    """
    return display_name.strip()


@dataclass(frozen=True)
class GroupRoleMapping:
    """Immutable IdP-group-``displayName`` → RBAC-role configuration."""

    by_group: Mapping[str, str]

    @classmethod
    def from_config(cls, raw: Mapping[str, str]) -> GroupRoleMapping:
        """Build a validated mapping from ``{group_display_name: role}`` config.

        Raises :class:`UnknownRoleError` if any target role is not one of the six.
        """
        normalized: dict[str, str] = {}
        for group, role in raw.items():
            if role not in VALID_SCIM_ROLES:
                raise UnknownRoleError(
                    f"group {group!r} maps to unknown role {role!r}; "
                    f"valid roles: {sorted(VALID_SCIM_ROLES)}"
                )
            normalized[_normalize_group(group)] = role
        return cls(by_group=normalized)

    def role_for_group(self, display_name: str) -> str | None:
        """Return the RBAC role for a group ``displayName``, or None if unmapped."""
        return self.by_group.get(_normalize_group(display_name))


def resolve_roles(
    group_display_names: Iterable[str], mapping: GroupRoleMapping
) -> set[str]:
    """Effective RBAC roles for a user given their group memberships.

    Deterministic and side-effect-free. Unmapped groups contribute nothing; a
    user with no memberships resolves to no roles (RBAC is unaffected for them).
    """
    roles: set[str] = set()
    for group in group_display_names:
        role = mapping.role_for_group(group)
        if role is not None:
            roles.add(role)
    return roles


@dataclass(frozen=True)
class RoleChange:
    """The effect of a group-membership change on a subject's roles."""

    before: set[str]
    after: set[str]
    granted: set[str]
    revoked: set[str]


def apply_group_membership(
    *,
    actor: str,
    subject: str,
    groups_before: Iterable[str],
    groups_after: Iterable[str],
    mapping: GroupRoleMapping,
    scim_request_id: str | None = None,
    db_path: Path | None = None,
) -> RoleChange:
    """Resolve a membership change to a role change and audit it when non-empty.

    Computes the subject's roles before and after the membership change. When the
    effective role set changes, appends exactly one append-only provisioning audit
    event (``operation="group-role-remap"``) via the existing SCIM audit store; no
    event is written when the roles are unchanged. Returns the :class:`RoleChange`.
    """
    before = resolve_roles(groups_before, mapping)
    after = resolve_roles(groups_after, mapping)

    if before != after:
        # Imported here so the pure resolver above carries no store dependency.
        from novafabric.server.scim_store import append_audit_event

        append_audit_event(
            actor=actor,
            operation="group-role-remap",
            resource_type="Group",
            subject=subject,
            roles_before=sorted(before),
            roles_after=sorted(after),
            scim_request_id=scim_request_id,
            db_path=db_path,
        )

    return RoleChange(
        before=before,
        after=after,
        granted=after - before,
        revoked=before - after,
    )
