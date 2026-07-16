"""Named exception classes for the agent execution-graph projection (ADR-0124)."""

from __future__ import annotations


class AgentGraphError(Exception):
    """Base class for agent execution-graph errors."""


class CapsuleNotFoundError(AgentGraphError):
    """The given path is not a readable Run Capsule directory."""
