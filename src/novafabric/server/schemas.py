"""Response schemas for the ``/v0`` REST contract (ADR-0227).

These models exist to make ``api/openapi.yaml`` *describe* the API. They are
attached to routes through FastAPI's ``responses={...}`` parameter, which is
**documentation-only**: it names the schema in ``components.schemas`` and leaves
the returned object untouched.

That is deliberate, and it is the whole point of ADR-0227. The obvious
alternative — ``response_model=`` — makes FastAPI *filter* the response body to
the model's fields, which would silently change the wire format of a published
API. The 30 ``response_model=None`` markers across ``server/routes/`` are
load-bearing for exactly that reason, and this module does not disturb them.

The cost of a documentation-only model is that nothing checks it, so it can
quietly start lying. That cost is paid by
``tests/server/test_openapi_schema_conformance.py``, which validates a real
response from every in-scope route against the model declared for it. If a route
changes shape without its model following, the suite fails by name.

Models reuse the project's existing enums and request models wherever they
exist (``spec.models.AssetType``/``AssetStatus``). A restated enum is how the
previous published contract came to list four asset statuses when the code had
six.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from novafabric.spec.models import AssetStatus, AssetType

# ---------------------------------------------------------------------------
# Error envelope (ADR-0017)
# ---------------------------------------------------------------------------


class ErrorDetail(BaseModel):
    """The inner object of the standard error envelope."""

    code: str = Field(examples=["not_found"])
    message: str = Field(examples=["Asset 'my-agent@v1.0' not found."])
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured, machine-readable context for the error.",
    )


class ErrorEnvelope(BaseModel):
    """Every non-2xx response body: ``{"error": {"code", "message", "details"}}``."""

    error: ErrorDetail


_ERROR_DESCRIPTIONS: dict[int, str] = {
    400: "Invalid request parameters or body.",
    401: "Missing or invalid bearer token.",
    403: "Authenticated but not permitted (missing role).",
    404: "The requested resource does not exist.",
    409: "The request conflicts with the current state of the resource.",
    412: "A precondition on the request was not met.",
    413: "The request body exceeds the configured size limit.",
}


def error_responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    """Declare standard-envelope error responses for the given status codes.

    Documentation-only, like everything else in this module — the handlers in
    ``server/errors.py`` already produce these bodies at runtime.

    ``422`` is deliberately absent. Two different shapes answer on it: the
    project's own ``ValidationError`` returns the envelope, while FastAPI's
    request-validation failure returns ``HTTPValidationError``. Declaring one of
    them as *the* 422 body would document a half-truth, so the framework's
    automatic ``HTTPValidationError`` entry is left to stand alone.
    """
    return {
        code: {"model": ErrorEnvelope, "description": _ERROR_DESCRIPTIONS[code]}
        for code in codes
    }


# ---------------------------------------------------------------------------
# Request-body declaration
# ---------------------------------------------------------------------------


def _inline_defs(node: Any, defs: dict[str, Any], chain: tuple[str, ...] = ()) -> Any:
    """Resolve ``#/$defs/...`` references into the schema body.

    Pydantic emits nested models into ``$defs``, but ``$defs`` is not a location
    an OpenAPI document can reference — a spec carrying ``#/$defs/ScoreSource``
    is one the generator will refuse to bundle. Routes here declare their
    request body as a self-contained inline schema, so the nested definitions
    are folded in rather than pointed at.

    ``chain`` tracks the refs currently being resolved so a self-referential
    model raises instead of recursing forever.
    """
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref.removeprefix("#/$defs/")
            if name in chain:
                raise ValueError(
                    f"cannot inline a self-referential model: {' -> '.join((*chain, name))}"
                )
            resolved = _inline_defs(defs[name], defs, (*chain, name))
            siblings = {k: v for k, v in node.items() if k != "$ref"}
            return {**resolved, **siblings}
        return {k: _inline_defs(v, defs, chain) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline_defs(item, defs, chain) for item in node]
    return node


def request_body(
    model: type[BaseModel], *, description: str | None = None, required: bool = True
) -> dict[str, Any]:
    """Build an ``openapi_extra`` fragment describing a route's request body.

    Used where a route validates its body by hand and must keep doing so. The
    evidence-export route answers a missing ``run_id`` with a ``400`` from the
    standard envelope; binding the model as the handler's parameter would move
    that to FastAPI's ``422``, changing a published API to improve a document.
    So the model describes the body without receiving it, exactly like the
    ``responses={...}`` declarations above.
    """
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})
    body: dict[str, Any] = {
        "required": required,
        "content": {"application/json": {"schema": _inline_defs(schema, defs)}},
    }
    if description is not None:
        body["description"] = description
    return {"requestBody": body}


# ---------------------------------------------------------------------------
# Pagination (ADR-0206)
# ---------------------------------------------------------------------------


class PaginationMeta(BaseModel):
    """Cursor-pagination envelope shared by every list endpoint.

    ``total`` is optional, and that is not a hedge. Keyset pages deliberately
    omit it — an exact total is the O(N) scan keyset pagination exists to
    eliminate — so only the first page carries one (``routes/capsules.py``,
    ``list_capsules``). The previous published contract marked it required,
    which made the spec disagree with every page after the first.
    """

    next_cursor: str | None = Field(
        default=None,
        description="Opaque cursor for the next page; null when no more pages.",
    )
    total: int | None = Field(
        default=None,
        description=(
            "Total count of matching items, without pagination applied. Present "
            "on the first page only; keyset pages omit it by design."
        ),
    )


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------


class AssetSummary(BaseModel):
    """One asset as returned by the list endpoint."""

    id: str
    name: str
    version: str = Field(description="SemVer string.")
    asset_type: AssetType
    status: AssetStatus
    created_at: str
    promoted_at: str | None = None
    git_commit_sha: str | None = None


class AssetDetail(AssetSummary):
    """A single asset with its spec and promotion provenance."""

    spec_json: str | None = Field(
        default=None, description="Raw JSON-serialized asset spec."
    )
    promoted_by: str | None = None
    forced_promotion: bool = False


class AssetListResponse(PaginationMeta):
    """``GET /v0/assets``."""

    items: list[AssetSummary]


# ---------------------------------------------------------------------------
# Capsules
# ---------------------------------------------------------------------------


class CapsuleSummary(BaseModel):
    """One run capsule as returned by the list endpoint."""

    run_id: str
    status: str = Field(
        description="Run outcome; 'unknown' when the manifest does not record one."
    )
    created_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    command: list[str] | None = None
    exit_code: int | None = None


class CapsuleDetail(CapsuleSummary):
    """A single run capsule, with the manifest fields the list view omits."""

    schema_version: str | None = None
    novafabric_version: str | None = None
    model_call_count: int | None = None
    tool_call_count: int | None = None
    mutating_tool_count: int | None = None
    capture_mode: str | None = None


class CapsuleListResponse(PaginationMeta):
    """``GET /v0/capsules``."""

    items: list[CapsuleSummary]


class ScoreSubmissionResult(BaseModel):
    """``POST /v0/capsules/{run_id}/scores``.

    Returned for **both** 201 (appended) and 200 (idempotent replay). The
    previous published contract declared the 200 as bodiless; the route returns
    the same body either way and only the status code differs.
    """

    score: dict[str, Any] = Field(description="The stored score record.")
    idempotent_replay: bool
    config_bound: bool
    submission: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Who submitted, as distinct from what evaluator produced the value: "
            "principal, scope and received_at."
        ),
    )


# ---------------------------------------------------------------------------
# Evidence bundles
# ---------------------------------------------------------------------------


class EvidenceExportRequest(BaseModel):
    """``POST /v0/evidence`` request body."""

    run_id: str = Field(description="The capsule run_id to export as evidence.")
    output_path: str | None = Field(
        default=None,
        description="Optional override for the output ZIP path (server-side path).",
    )
    allow_unsafe_skips: bool = False


class BundleSummary(BaseModel):
    """Evidence-bundle metadata."""

    bundle_id: str
    run_id: str
    created_at: str
    size_bytes: int
    bundle_path: str | None = None


__all__ = [
    "AssetDetail",
    "AssetListResponse",
    "AssetSummary",
    "BundleSummary",
    "CapsuleDetail",
    "CapsuleListResponse",
    "CapsuleSummary",
    "ErrorDetail",
    "ErrorEnvelope",
    "EvidenceExportRequest",
    "PaginationMeta",
    "ScoreSubmissionResult",
    "error_responses",
    "request_body",
]
