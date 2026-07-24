"""NovaFabric Python client SDK — typed sync client for the ``/v0`` REST API.

ADR-0202 P1, **experimental**. Server-mode tooling: local-first core features
(capture, validate, replay, diff, lineage) have no dependency on this package.

Entry point::

    from novafabric.client import NovaFabricClient

    with NovaFabricClient("https://nova.example.com/v0", api_key="nvfk_...") as nc:
        for capsule in nc.iter_capsules():
            print(capsule.run_id)
"""

from novafabric.client._client import (
    NovaFabricClient,
    TokenProvider,
    reset_deprecation_warnings,
)
from novafabric.client._errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    NotFoundError,
    NovaFabricAPIError,
    NovaFabricClientError,
    NovaFabricConfigError,
    NovaFabricTimeout,
    NovaFabricTransportError,
    PreconditionFailedError,
    RateLimitedError,
    ServerError,
    ValidationFailedError,
)
from novafabric.client._models import (
    ApiResult,
    AssetDetail,
    AssetSummary,
    CapsuleDetail,
    CapsuleSummary,
    Page,
    ResponseMeta,
    ScoreSubmission,
    ScoreSubmissionResult,
    ServerHealth,
)
from novafabric.client._retry import RetryConfig

__all__ = [
    "ApiResult",
    "AssetDetail",
    "AssetSummary",
    "AuthenticationError",
    "AuthorizationError",
    "CapsuleDetail",
    "CapsuleSummary",
    "ConflictError",
    "NotFoundError",
    "NovaFabricAPIError",
    "NovaFabricClient",
    "NovaFabricClientError",
    "NovaFabricConfigError",
    "NovaFabricTimeout",
    "NovaFabricTransportError",
    "Page",
    "PreconditionFailedError",
    "RateLimitedError",
    "ResponseMeta",
    "RetryConfig",
    "ScoreSubmission",
    "ScoreSubmissionResult",
    "ServerError",
    "ServerHealth",
    "TokenProvider",
    "ValidationFailedError",
    "reset_deprecation_warnings",
]
