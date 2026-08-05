"""Structural checks that the `age` docker-compose profile is genuinely opt-in.

Companion to `tests/deploy/test_image_pins.py` (same repo, same drift-prevention
intent, different axis): that test enforces *which tag* the `age` service is
pinned to; this one enforces *how* the service is wired into the compose file's
profile structure — dedicated profile, absent by default, host-loopback-only,
healthchecked. Parses the YAML directly rather than shelling out to
`docker compose config`, so it runs without Docker installed.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "deploy" / "docker" / "docker-compose.yml"


def _load_compose() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))


def _active_services(compose: dict, profile: str | None) -> set[str]:
    """Mirror docker compose's own profile-activation rule.

    A service with no `profiles` key is always active. A service with a
    `profiles` list is active only when `profile` is in that list.
    """
    active = set()
    for name, definition in compose["services"].items():
        service_profiles = definition.get("profiles")
        if not service_profiles:
            active.add(name)
        elif profile is not None and profile in service_profiles:
            active.add(name)
    return active


def test_age_service_exists_and_is_a_dedicated_profile() -> None:
    compose = _load_compose()
    age = compose["services"]["age"]
    assert age.get("profiles") == ["age"], (
        "the `age` service must carry its own dedicated `profiles: [age]` — "
        f"got {age.get('profiles')!r}"
    )


def test_age_service_absent_from_default_profile() -> None:
    compose = _load_compose()
    assert "age" not in _active_services(compose, profile=None), (
        "the `age` service must not start on a plain `docker compose up` "
        "(no profile flag) — it must be strictly opt-in"
    )


def test_age_service_absent_from_prod_profile() -> None:
    compose = _load_compose()
    assert "age" not in _active_services(compose, profile="prod"), (
        "the `age` service must not be folded into `prod` — it is a dedicated "
        "alternative lineage engine (AGELineageStore), not part of the "
        "KuzuDB/JanusGraph prod stack"
    )


def test_age_service_present_under_its_own_profile() -> None:
    compose = _load_compose()
    assert "age" in _active_services(compose, profile="age")


def test_age_service_binds_loopback_only() -> None:
    compose = _load_compose()
    age = compose["services"]["age"]
    ports = age.get("ports", [])
    assert ports, "the `age` service must publish a host port"
    for port in ports:
        assert str(port).startswith("127.0.0.1:"), (
            f"the `age` service must bind 127.0.0.1 only, got {port!r}"
        )


def test_age_service_binds_the_documented_port() -> None:
    compose = _load_compose()
    age = compose["services"]["age"]
    assert "127.0.0.1:5433:5432" in age.get("ports", []), (
        "the `age` service must publish host port 5433 (5432 is the "
        "MetadataStore postgres service; 5433 is documented in "
        "docs/developer-guide.md and docs/ops/cluster-scale-migration.md)"
    )


def test_age_service_has_a_healthcheck() -> None:
    compose = _load_compose()
    age = compose["services"]["age"]
    assert age.get("healthcheck", {}).get("test"), (
        "the `age` service must define a healthcheck, matching the style of "
        "every other service in this compose file"
    )


def test_age_service_is_not_pinned_to_latest() -> None:
    compose = _load_compose()
    age = compose["services"]["age"]
    image = age.get("image", "")
    assert image.startswith("apache/age:"), f"unexpected image for age service: {image!r}"
    assert not image.endswith(":latest"), f"the `age` service must not use :latest: {image!r}"


def test_age_service_uses_a_named_volume() -> None:
    compose = _load_compose()
    age = compose["services"]["age"]
    volumes = age.get("volumes", [])
    assert any(v.startswith("age-data:") for v in volumes), (
        "the `age` service must persist to a dedicated named volume"
    )
    assert "age-data" in compose.get("volumes", {}), (
        "age-data must be declared in the top-level volumes block"
    )


def test_age_service_container_name_matches_convention() -> None:
    compose = _load_compose()
    age = compose["services"]["age"]
    assert age.get("container_name") == "novafabric-age"
