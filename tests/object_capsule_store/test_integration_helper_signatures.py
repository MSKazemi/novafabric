# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Guard: infra-gated OCC helpers must stay callable against real signatures.

``tests/object_capsule_store/integration/test_occ_backends.py`` only executes
when NOVA_INTEGRATION=1 plus a live backend is present, so a constructor rename
can rot its helpers indefinitely without any suite going red.  That is exactly
what happened: the MinIO and Ceph helpers passed ``endpoint=``/``access_key=``/
``secret_key=`` long after the adapters had settled on ``endpoint_url=`` and
boto3's credential chain.  The AC-2 conditional-PUT acceptance test could not
run at all, and nothing said so (found 2026-08-27 against a live MinIO).

These checks are signature-only — no network, no gate — so the rot surfaces in
the ordinary unit run.
"""

from __future__ import annotations

import inspect

import pytest

from novafabric.object_capsule_store.worm.ceph import CephWormAdapter
from novafabric.object_capsule_store.worm.minio import MinioWormAdapter
from novafabric.object_capsule_store.worm.s3 import S3WormAdapter

ADAPTERS = [
    pytest.param(MinioWormAdapter, id="minio"),
    pytest.param(CephWormAdapter, id="ceph"),
]


@pytest.mark.parametrize("adapter_cls", ADAPTERS)
def test_s3_compatible_adapters_take_endpoint_url(adapter_cls: type) -> None:
    """The integration helpers construct these with ``endpoint_url=``."""
    params = inspect.signature(adapter_cls.__init__).parameters
    assert "endpoint_url" in params, (
        f"{adapter_cls.__name__} lost 'endpoint_url'; "
        "tests/object_capsule_store/integration/test_occ_backends.py passes it"
    )
    assert "bucket" in params
    assert "client" in params


@pytest.mark.parametrize("adapter_cls", ADAPTERS)
def test_s3_compatible_adapters_reject_credential_kwargs(adapter_cls: type) -> None:
    """Credentials come from boto3's chain, not constructor kwargs.

    If that ever changes, the integration helpers must change with it.
    """
    params = inspect.signature(adapter_cls.__init__).parameters
    assert "access_key" not in params
    assert "secret_key" not in params


def test_occ_helpers_bind_against_real_adapter_signatures() -> None:
    """Bind each helper's call site without touching the network.

    ``Signature.bind`` raises TypeError on exactly the mismatch that made the
    live run fail, but never constructs a client.
    """
    sentinel = object()
    for adapter_cls in (MinioWormAdapter, CephWormAdapter):
        inspect.signature(adapter_cls).bind(
            bucket="nova-occ-test",
            endpoint_url="http://localhost:9000",
            client=sentinel,
        )
    inspect.signature(S3WormAdapter).bind(bucket="nova-occ-test")
