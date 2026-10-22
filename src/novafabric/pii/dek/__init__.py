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
"""Data Encryption Key (DEK) store for GDPR Art.17 erasure (ADR-0069).

Status — experimental (ADR-0314). DEK destruction erases the PII that capture
encrypted under the DEK, which happens only for ``nova capture --pii-subject <id>``.
"""

from .store import (
    DataSubjectDEK,
    DEKStore,
    DEKSubjectRecord,
    ErasureDeferredReceipt,
    ErasureHeldReceipt,
    ErasureHold,
    ErasureReceipt,
    HeldCapsule,
    active_erasure_holds,
    open_dek_store,
)

__all__ = [
    "DEKStore",
    "DEKSubjectRecord",
    "DataSubjectDEK",
    "ErasureDeferredReceipt",
    "ErasureHeldReceipt",
    "ErasureHold",
    "ErasureReceipt",
    "HeldCapsule",
    "active_erasure_holds",
    "open_dek_store",
]
