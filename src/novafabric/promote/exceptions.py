"""Exceptions for the promote (maker-checker) subsystem."""

from __future__ import annotations


class PredicateValidationError(Exception):
    """Raised when a predicate fails JSON Schema validation."""


class PolicyNotFoundError(Exception):
    """Raised when a requested policy version is not found."""


class BundleNotFoundError(Exception):
    """Raised when a proposal or approval bundle file is not found."""


class SoDError(Exception):
    """Raised on Separation-of-Duties violations during verification."""
