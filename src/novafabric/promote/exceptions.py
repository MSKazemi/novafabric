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


class BypassRefusedError(Exception):
    """A seal bypass was refused and nothing was stored (base of the two causes below).

    Callers that only need "the bypass did not happen" catch this: the CLI exits 1,
    ``nova serve`` records the attempt and answers 503.
    """


class BypassAuditError(BypassRefusedError):
    """A seal bypass was refused because its audit entry could not be written.

    A bypass skips the two-person rule; the hash-chained ``seal.bypass.create``
    entry is its compensating control. With no entry there is no bypass: nothing
    is stored and nothing is announced (fail closed).
    """


class BypassStoreError(BypassRefusedError):
    """A seal bypass was refused because its file could not be stored.

    Raised when staging the bundle, or renaming it into place after its
    ``seal.bypass.create`` entry was written, fails. In the second case a
    compensating ``seal.bypass.abort`` entry is appended for the same
    ``bypass_uuid`` (best effort; the message says when it could not be written),
    so the audit log never shows a bypass that does not exist without also showing
    that it was withdrawn.
    """
