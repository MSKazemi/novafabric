"""S3 ``object_exists`` is False only for "not found" (review storage-backup-17).

Every error — AccessDenied, throttling, a timeout — returned False, so wrong
credentials on restore read as "this is not the bucket the backup describes" (the
probe's own network/auth branch could never trigger), and ``put_capsule`` re-PUT
an object after a transient HEAD failure.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from novafabric.object_capsule_store.worm.s3 import S3WormAdapter


class _ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(f"An error occurred ({code}) when calling the HeadObject operation")
        self.response = {"Error": {"Code": code}}


def _adapter(exc: Exception | None) -> S3WormAdapter:
    client = MagicMock()
    if exc is not None:
        client.head_object.side_effect = exc
    return S3WormAdapter("bucket", client=client)


@pytest.mark.parametrize("code", ["404", "NoSuchKey", "NotFound"])
def test_not_found_is_false(code: str) -> None:
    assert _adapter(_ClientError(code)).object_exists("k") is False


def test_present_is_true() -> None:
    assert _adapter(None).object_exists("k") is True


@pytest.mark.parametrize("exc", [
    _ClientError("403"), _ClientError("AccessDenied"), _ClientError("SlowDown"),
    TimeoutError("read timed out"),
], ids=["403", "access-denied", "throttled", "timeout"])
def test_any_other_error_is_raised(exc: Exception) -> None:
    with pytest.raises(type(exc)):
        _adapter(exc).object_exists("k")
