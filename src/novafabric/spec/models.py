from __future__ import annotations

import re
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, field_validator


class AssetType(str, Enum):
    model = "model"
    agent = "agent"
    prompt = "prompt"
    tool = "tool"
    dataset = "dataset"
    evaluation = "evaluation"
    deployment = "deployment"


class AssetStatus(str, Enum):
    development = "development"
    validated = "validated"
    pending_approval = "pending_approval"
    staging = "staging"
    production = "production"
    archived = "archived"


_SEMVER_RE = re.compile(
    r"^v?\d+\.\d+(\.\d+)?(-[a-zA-Z0-9.]+)?(\+[a-zA-Z0-9.]+)?$"
)


class BaseAssetSpec(BaseModel):
    novafabric_spec_version: str
    asset_type: AssetType
    name: str = Field(min_length=1, max_length=255)
    version: str
    status: AssetStatus = AssetStatus.development
    description: str | None = None
    tags: dict[str, str] = {}
    external_refs: dict[str, str] = {}
    dependencies: list[str] = Field(
        default_factory=list,
        description=(
            "Declared asset dependencies as 'name@version' refs. "
            "Recorded as DEPENDS_ON lineage edges when the asset is registered."
        ),
    )

    @field_validator("version")
    @classmethod
    def validate_semver(cls, v: str) -> str:
        if not _SEMVER_RE.match(str(v)):
            raise ValueError(f"'{v}' is not a valid SemVer string")
        return str(v)


class ModelSpecFields(BaseModel):
    framework: str
    artifact_path: str


class ModelSpec(BaseAssetSpec):
    asset_type: Literal[AssetType.model] = AssetType.model
    spec: ModelSpecFields


class AgentModelRef(BaseModel):
    provider: str
    name: str
    temperature: float | None = None


class AgentSpecFields(BaseModel):
    model: AgentModelRef
    tools: list[str]
    prompts: dict[str, Any]
    policies: list[Any]
    evals: list[str] = Field(min_length=1)
    memory: dict[str, Any] | None = None


class AgentSpec(BaseAssetSpec):
    asset_type: Literal[AssetType.agent] = AssetType.agent
    spec: AgentSpecFields


class PromptSpecFields(BaseModel):
    template: str | None = None


class PromptSpec(BaseAssetSpec):
    asset_type: Literal[AssetType.prompt] = AssetType.prompt
    spec: PromptSpecFields | None = None


class ToolSpecFields(BaseModel):
    entrypoint: str | None = None


class ToolSpec(BaseAssetSpec):
    asset_type: Literal[AssetType.tool] = AssetType.tool
    spec: ToolSpecFields | None = None


class DatasetSpecFields(BaseModel):
    path: str | None = None


class DatasetSpec(BaseAssetSpec):
    asset_type: Literal[AssetType.dataset] = AssetType.dataset
    spec: DatasetSpecFields | None = None


class EvaluationSpecFields(BaseModel):
    suite: str | None = None


class EvaluationSpec(BaseAssetSpec):
    asset_type: Literal[AssetType.evaluation] = AssetType.evaluation
    spec: EvaluationSpecFields | None = None


class DeploymentSpecFields(BaseModel):
    endpoint: str | None = None


class DeploymentSpec(BaseAssetSpec):
    asset_type: Literal[AssetType.deployment] = AssetType.deployment
    spec: DeploymentSpecFields | None = None


AssetSpec = Annotated[
    Union[
        ModelSpec,
        AgentSpec,
        PromptSpec,
        ToolSpec,
        DatasetSpec,
        EvaluationSpec,
        DeploymentSpec,
    ],
    Field(discriminator="asset_type"),
]
