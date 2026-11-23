"""Shared scenario logic for EvidenceLab D1: pure functions, no process or network IO.

The agent, the strict mock server and the harness all import this module, so the
three of them agree on one definition of the scenario manifest, the two tools'
simulator functions, the model request and the policy oracle. Nothing here is a
NovaFabric format: the manifest and fixtures are application-local test material.

Every failure is an :class:`EvidenceLabError` with a stable ``E_*`` code, so a
test or a person can tell *which* guard fired instead of reading a traceback.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

#: The only API key the example will ever send. A real key in the environment is
#: refused, never forwarded, so a misconfigured run cannot spend money or leak.
DUMMY_API_KEY = "sk-evidencelab-local-dummy"

SCENARIOS_ROOT = Path(__file__).resolve().parent / "scenarios"
FIXTURE_FILES = ("metrics.json", "logs.jsonl", "prompt.txt", "model_response.json")
#: D2: the base keys describe the ``unsafe`` variant; ``corrected`` is declared under
#: ``variants`` and changes only the prompt and the fixture reply.
VARIANTS = ("unsafe", "corrected")
BASE_VARIANT = "unsafe"
KNOWN_TOOLS = ("read_metrics", "search_logs")
MAX_FIXTURE_BYTES = 1024 * 1024
MIN_PORT = 1024  # an ephemeral port, never a well-known one

_SCENARIO_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ACTION = re.compile(r"^[a-z0-9_]{1,64}$")
_PROXY_VARS = {"http_proxy", "https_proxy", "all_proxy"}
_EXTRA_CREDENTIAL_VARS = {
    "openai_org_id",
    "openai_organization",
    "openai_project",
    "openai_project_id",
    "aws_secret_access_key",
    "aws_session_token",
    "azure_client_secret",
}
_SAFE_CAPTURE_LEVELS = {"", "minimal", "standard"}


class EvidenceLabError(Exception):
    """A named, expected failure of the example (never a bare traceback)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ── canonical encodings ──────────────────────────────────────────────────────


def canonical_json(value: Any) -> str:
    """The one JSON encoding every digest in this example is taken over."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: str | bytes) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


# ── manifest ─────────────────────────────────────────────────────────────────


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Tools(_Strict):
    allowlist: list[str]
    expected_order: list[str]
    logs_limit: int = Field(ge=1, le=100)
    max_logs_limit: int = Field(ge=1, le=100)

    @field_validator("allowlist", "expected_order")
    @classmethod
    def _known_tools_only(cls, names: list[str]) -> list[str]:
        unknown = [n for n in names if n not in KNOWN_TOOLS]
        if unknown:
            raise ValueError(f"unknown tool names {unknown}; D1 implements {list(KNOWN_TOOLS)}")
        return names


class Bounds(_Strict):
    min: int
    max: int


class Policy(_Strict):
    forbidden_actions: list[str]
    allowed_actions: list[str]
    # D2: an allowlisted action listed here is accepted only with an integer value in range.
    value_bounds: dict[str, Bounds] = Field(default_factory=dict)


class Expected(_Strict):
    unsafe_action: str
    policy_result: Literal["reject"]


class VariantExpected(_Strict):
    action: str
    policy_result: Literal["accept", "reject"]


class Variant(_Strict):
    """A controlled input variant: another prompt and fixture reply, same observations."""

    prompt_revision: str
    prompt: str
    model_response: str
    golden: str
    fixture_sha256: dict[str, str]
    expected: VariantExpected

    @field_validator("prompt", "model_response", "golden")
    @classmethod
    def _plain_file_name(cls, value: str) -> str:
        parts = Path(value).parts
        if not parts or Path(value).is_absolute() or ".." in parts:
            raise ValueError(f"{value!r} must be a relative path inside the scenario directory")
        return value


class Limits(_Strict):
    max_steps: int = Field(ge=1, le=32)
    max_request_bytes: int = Field(ge=1024, le=MAX_FIXTURE_BYTES)
    timeout_seconds: int = Field(ge=1, le=300)


class Manifest(_Strict):
    schema_version: Literal[1]
    scenario_id: str
    seed: int
    model_id: str
    prompt_revision: str
    fixture_sha256: dict[str, str]
    tools: Tools
    policy: Policy
    expected: Expected
    limits: Limits
    variants: dict[str, Variant] = Field(default_factory=dict)

    @field_validator("variants")
    @classmethod
    def _known_variants(cls, value: dict[str, Variant]) -> dict[str, Variant]:
        unknown = sorted(set(value) - set(VARIANTS[1:]))
        if unknown:
            raise ValueError(f"unknown variants {unknown}; D2 declares {list(VARIANTS[1:])}")
        return value

    @field_validator("model_id")
    @classmethod
    def _visibly_synthetic(cls, value: str) -> str:
        if not value.startswith("novafabric-mock/"):
            raise ValueError("model_id must be visibly synthetic (novafabric-mock/…)")
        return value

    @field_validator("fixture_sha256")
    @classmethod
    def _fixture_digests(cls, value: dict[str, str]) -> dict[str, str]:
        if sorted(value) != sorted(FIXTURE_FILES):
            raise ValueError(f"fixture_sha256 must name exactly {sorted(FIXTURE_FILES)}")
        bad = [name for name, digest in value.items() if not _SHA256.match(digest)]
        if bad:
            raise ValueError(f"not a lowercase sha256 hex digest: {bad}")
        return value


@dataclass(frozen=True)
class Scenario:
    """A loaded scenario: the manifest plus fixtures read once and digest-checked."""

    manifest: Manifest
    directory: Path
    metrics: dict[str, Any]
    logs: list[dict[str, Any]]
    prompt: str
    model_response: dict[str, Any]
    variant: str = BASE_VARIANT

    @property
    def fixture_manifest_sha256(self) -> str:
        return sha256_hex(canonical_json(self.manifest.fixture_sha256))

    @property
    def _variant(self) -> Variant | None:
        return None if self.variant == BASE_VARIANT else self.manifest.variants[self.variant]

    @property
    def prompt_revision(self) -> str:
        v = self._variant
        return self.manifest.prompt_revision if v is None else v.prompt_revision

    @property
    def expected_action(self) -> str:
        v = self._variant
        return self.manifest.expected.unsafe_action if v is None else v.expected.action

    @property
    def expected_policy_result(self) -> str:
        v = self._variant
        return self.manifest.expected.policy_result if v is None else v.expected.policy_result

    @property
    def golden_path(self) -> Path:
        v = self._variant
        return GOLDEN_REQUEST if v is None else Path(v.golden)

    @property
    def variant_fixture_sha256(self) -> dict[str, str]:
        """Digests of the files this variant adds (empty for the base variant)."""
        v = self._variant
        return {} if v is None else dict(v.fixture_sha256)


def resolve_scenario_dir(scenario_id: str, root: Path | None = None) -> Path:
    if not _SCENARIO_ID.match(scenario_id):
        raise EvidenceLabError("E_SCENARIO_NOT_FOUND", f"{scenario_id!r} is not a scenario id")
    directory = (root or SCENARIOS_ROOT) / scenario_id.replace("-", "_")
    if not (directory / "scenario.yaml").is_file():
        raise EvidenceLabError(
            "E_SCENARIO_NOT_FOUND", f"no scenario {scenario_id!r} in {directory.parent}"
        )
    return directory


def _read_fixture(directory: Path, name: str, expected_sha: str) -> bytes:
    path = directory / name
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise EvidenceLabError(
            "E_FIXTURE_MISSING", f"fixture {name} is unreadable: {exc.strerror}"
        ) from exc
    if size > MAX_FIXTURE_BYTES:
        raise EvidenceLabError("E_FIXTURE_TOO_LARGE", f"fixture {name} is {size} bytes")
    data = path.read_bytes()
    if sha256_hex(data) != expected_sha:
        raise EvidenceLabError(
            "E_FIXTURE_DIGEST_MISMATCH", f"fixture {name} does not match its manifest digest"
        )
    return data


def load_scenario(directory: Path, variant: str = BASE_VARIANT) -> Scenario:
    """Load and validate a scenario variant. Unknown or missing manifest keys are errors."""
    if variant not in VARIANTS:
        raise EvidenceLabError("E_VARIANT_UNKNOWN", f"{variant!r} is not one of {list(VARIANTS)}")
    manifest_path = directory / "scenario.yaml"
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeDecodeError) as exc:
        raise EvidenceLabError(
            "E_MANIFEST_INVALID", f"cannot read {manifest_path.name}: {exc}"
        ) from exc
    try:
        manifest = Manifest.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or "<root>"
        raise EvidenceLabError("E_MANIFEST_INVALID", f"{where}: {first['msg']}") from exc
    if manifest.scenario_id != directory.name.replace("_", "-"):
        raise EvidenceLabError(
            "E_MANIFEST_INVALID",
            f"scenario_id {manifest.scenario_id!r} does not match {directory.name!r}",
        )
    tools = manifest.tools
    if sorted(tools.allowlist) != sorted(tools.expected_order):
        raise EvidenceLabError(
            "E_MANIFEST_INVALID", "tools.expected_order must be a permutation of allowlist"
        )
    if tools.logs_limit > tools.max_logs_limit:
        raise EvidenceLabError(
            "E_MANIFEST_INVALID", "tools.logs_limit exceeds tools.max_logs_limit"
        )

    policy = manifest.policy
    for action, bounds in policy.value_bounds.items():
        if action not in policy.allowed_actions or bounds.min > bounds.max:
            raise EvidenceLabError(
                "E_MANIFEST_INVALID",
                f"policy.value_bounds.{action}: must name an allowed action with min <= max",
            )
    for name, spec in manifest.variants.items():
        if sorted(spec.fixture_sha256) != sorted([spec.prompt, spec.model_response]) or any(
            not _SHA256.match(d) for d in spec.fixture_sha256.values()
        ):
            raise EvidenceLabError(
                "E_MANIFEST_INVALID",
                f"variants.{name}.fixture_sha256 must pin exactly its prompt and model_response",
            )
    if variant != BASE_VARIANT and variant not in manifest.variants:
        raise EvidenceLabError("E_VARIANT_UNKNOWN", f"the scenario declares no {variant!r} variant")

    data = {n: _read_fixture(directory, n, manifest.fixture_sha256[n]) for n in FIXTURE_FILES}
    if variant != BASE_VARIANT:
        spec = manifest.variants[variant]
        data["prompt.txt"] = _read_fixture(directory, spec.prompt, spec.fixture_sha256[spec.prompt])
        data["model_response.json"] = _read_fixture(
            directory, spec.model_response, spec.fixture_sha256[spec.model_response]
        )
    try:
        metrics = json.loads(data["metrics.json"])
        logs = [
            json.loads(line) for line in data["logs.jsonl"].decode().splitlines() if line.strip()
        ]
        model_response = json.loads(data["model_response.json"])
        prompt = data["prompt.txt"].decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise EvidenceLabError(
            "E_FIXTURE_INVALID", f"a fixture is not valid JSON/UTF-8: {exc}"
        ) from exc
    return Scenario(manifest, directory, metrics, logs, prompt, model_response, variant)


# ── the two tools' simulator functions (pure; the agent wraps them) ───────────


def _known_services(scn: Scenario) -> set[str]:
    return set(scn.metrics.get("services", {}))


def query_metrics(scn: Scenario, service: str) -> dict[str, Any]:
    if not isinstance(service, str) or service not in _known_services(scn):
        raise EvidenceLabError("E_UNKNOWN_SERVICE", f"no metrics for service {service!r}")
    series = scn.metrics["services"][service]["series"]
    return {"service": service, "points": len(series), "series": series}


def query_logs(scn: Scenario, service: str, limit: int) -> dict[str, Any]:
    if not isinstance(service, str) or service not in _known_services(scn):
        raise EvidenceLabError("E_UNKNOWN_SERVICE", f"no logs for service {service!r}")
    ceiling = scn.manifest.tools.max_logs_limit
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= ceiling:
        raise EvidenceLabError("E_LIMIT_EXCEEDED", f"limit must be an integer in 1..{ceiling}")
    matching = [row for row in scn.logs if row.get("service") == service]
    return {
        "service": service,
        "limit": limit,
        "total_matching": len(matching),
        "returned": min(limit, len(matching)),
        "entries": matching[-limit:],
    }


# ── the model request ────────────────────────────────────────────────────────

INVESTIGATED_SERVICE = "payments"


def build_request_body(scn: Scenario, observations: Mapping[str, Any]) -> dict[str, Any]:
    """The exact Chat Completions body for this scenario (golden: ``golden/``)."""
    user = canonical_json(
        {
            "scenario_id": scn.manifest.scenario_id,
            "prompt_revision": scn.prompt_revision,
            "observations": dict(observations),
        }
    )
    return {
        "model": scn.manifest.model_id,
        "seed": scn.manifest.seed,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": scn.prompt},
            {"role": "user", "content": user},
        ],
    }


GOLDEN_REQUEST = Path("golden") / "model_request.json"


def read_scenario_file(directory: Path, name: str | Path) -> bytes:
    """A file of the scenario directory: regular files only, and bounded.

    A directory, device or FIFO is refused by name (a FIFO would block the read forever),
    as is anything past ``MAX_FIXTURE_BYTES`` whatever ``st_size`` claims.
    """
    path = directory / name
    if not path.is_file():  # follows symlinks
        if path.exists():
            raise EvidenceLabError("E_FIXTURE_INVALID", f"{name} is not a regular file")
        raise EvidenceLabError("E_FIXTURE_MISSING", f"{name} is missing")
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_FIXTURE_BYTES + 1)
    except OSError as exc:
        raise EvidenceLabError(
            "E_FIXTURE_MISSING", f"{name} is unreadable: {exc.strerror}"
        ) from exc
    if len(data) > MAX_FIXTURE_BYTES:
        raise EvidenceLabError("E_FIXTURE_TOO_LARGE", f"{name} is too large")
    return data


def load_golden_request(scn: Scenario) -> dict[str, Any]:
    """The committed golden request transcript, read from the scenario directory."""
    name = scn.golden_path
    data = read_scenario_file(scn.directory, name)
    try:
        golden = json.loads(data)
    except (ValueError, RecursionError) as exc:  # deep nesting raises RecursionError
        raise EvidenceLabError("E_FIXTURE_INVALID", f"{name} is not valid JSON/UTF-8") from exc
    if not isinstance(golden, dict):
        raise EvidenceLabError("E_FIXTURE_INVALID", f"{name} is not a JSON object")
    return golden


def expected_observations(scn: Scenario) -> dict[str, Any]:
    """What the two tools must have returned, computed from the fixtures alone."""
    return {
        "read_metrics": query_metrics(scn, INVESTIGATED_SERVICE),
        "search_logs": query_logs(scn, INVESTIGATED_SERVICE, scn.manifest.tools.logs_limit),
    }


# ── decision + policy oracle ─────────────────────────────────────────────────


class Decision(_Strict):
    action: str = Field(min_length=1, max_length=64, pattern=_ACTION.pattern)
    value: bool | int | float
    reason: str = Field(min_length=1, max_length=500)


@dataclass(frozen=True)
class PolicyVerdict:
    result: Literal["accept", "reject"]
    rule: str


def parse_decision(content: str) -> Decision:
    if len(content) > 4096:
        raise EvidenceLabError("E_DECISION_INVALID", "decision is larger than 4096 characters")
    try:
        parsed = json.loads(content)
    except ValueError as exc:
        raise EvidenceLabError(
            "E_DECISION_INVALID_JSON", "model content is not valid JSON"
        ) from exc
    try:
        return Decision.model_validate(parsed)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or "<root>"
        raise EvidenceLabError("E_DECISION_INVALID", f"{where}: {first['msg']}") from exc


def evaluate_policy(scn: Scenario, decision: Decision) -> PolicyVerdict:
    """Deny by default: only an allowlisted action is accepted, and only within its bounds."""
    policy = scn.manifest.policy
    if decision.action in policy.forbidden_actions:
        return PolicyVerdict("reject", "forbidden_action")
    if decision.action not in policy.allowed_actions:
        return PolicyVerdict("reject", "action_not_allowlisted")
    bounds = policy.value_bounds.get(decision.action)
    if bounds is not None:
        value = decision.value
        if isinstance(value, bool) or not isinstance(value, int):
            return PolicyVerdict("reject", "value_not_an_integer")
        if not bounds.min <= value <= bounds.max:
            return PolicyVerdict("reject", "value_out_of_bounds")
    return PolicyVerdict("accept", "allowlisted_action")


# ── loopback + credential guards ─────────────────────────────────────────────


def validate_base_url(url: str) -> str:
    """Accept only ``http://<IPv4 loopback>:<ephemeral port>/v1``."""
    bad = EvidenceLabError(
        "E_BASE_URL_INVALID", "OPENAI_BASE_URL must be http://127.0.0.1:<port>/v1 (IPv4 loopback)"
    )
    try:
        parts = urlsplit(url)
        port = parts.port
        host = parts.hostname or ""
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise bad from exc
    if (
        parts.scheme != "http"
        or parts.username is not None
        or parts.password is not None
        or address.version != 4
        or not address.is_loopback
        or port is None
        or port < MIN_PORT
        or parts.path != "/v1"
        or parts.query
        or parts.fragment
    ):
        raise bad
    return url


def validate_environment(env: Mapping[str, str]) -> None:
    """Refuse an environment that could reach a real provider or widen capture."""
    validate_base_url(env.get("OPENAI_BASE_URL", ""))
    if env.get("OPENAI_API_KEY") != DUMMY_API_KEY:
        raise EvidenceLabError(
            "E_CREDENTIAL_REJECTED",
            "OPENAI_API_KEY must be the local dummy key; a real key is never forwarded",
        )
    for name, value in env.items():
        lowered = name.lower()
        if not value:
            continue
        if name == "OPENAI_API_KEY":
            continue
        if lowered.endswith(("_api_key", "_api_token")) or lowered in _EXTRA_CREDENTIAL_VARS:
            raise EvidenceLabError(
                "E_CREDENTIAL_REJECTED", f"{name} is set; the example uses no provider credentials"
            )
        if lowered in _PROXY_VARS:
            raise EvidenceLabError(
                "E_ENV_REJECTED", f"{name} is set; a proxy could route the loopback call away"
            )
    if env.get("NOVA_CAPTURE_LEVEL", "").lower() not in _SAFE_CAPTURE_LEVELS:
        raise EvidenceLabError(
            "E_ENV_REJECTED", "NOVA_CAPTURE_LEVEL must not widen capture past standard"
        )
