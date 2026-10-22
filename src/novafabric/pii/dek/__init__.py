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

Status — experimental: key lifecycle only. The AES-256-GCM DEK lifecycle is
implemented; capture does not yet encrypt PII with per-subject keys, so DEK
destruction does not yet erase any captured PII.
"""

from .store import (
    DataSubjectDEK,
    DEKStore,
    DEKSubjectRecord,
    ErasureDeferredReceipt,
    ErasureReceipt,
    open_dek_store,
)

__all__ = [
    "DEKStore",
    "DEKSubjectRecord",
    "DataSubjectDEK",
    "ErasureDeferredReceipt",
    "ErasureReceipt",
    "open_dek_store",
]
