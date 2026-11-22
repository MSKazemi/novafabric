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

"""Built-in profiles for copyrighted frameworks carry IDs and NovaFabric's own words only.

ISO/IEC standards and the AICPA Trust Services Criteria are copyrighted. A built-in
profile may cite their control identifiers, but each ``description`` must be a short
summary written by NovaFabric, never the standard's wording. Users who need the full
text load their own licensed catalog with ``--profile path.yaml``.
"""

from __future__ import annotations

from novafabric.compliance.audit.loader import list_profiles, load_profile

#: Frameworks whose criteria text is copyrighted by its publisher.
COPYRIGHTED_FRAMEWORKS: frozenset[str] = frozenset({"SOC 2 Type 2", "ISO/IEC 42001"})

#: Sentence openers of the publishers' own criteria wording.
BANNED_OPENERS: tuple[str, ...] = (
    "The entity ",
    "To meet its objectives, the entity",
    "The organisation shall",
    "The organization shall",
    "When planning for the AI management system",
)

#: Upper bound for a NovaFabric-written summary.
MAX_DESCRIPTION_CHARS = 160


def _copyrighted_controls() -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for pid in list_profiles():
        prof = load_profile(pid)
        if prof.framework in COPYRIGHTED_FRAMEWORKS:
            for c in prof.controls:
                rows.append((pid, c.id, " ".join(c.description.split())))
    return rows


def test_guard_covers_every_copyrighted_profile() -> None:
    frameworks = {load_profile(p).framework for p in list_profiles()}
    assert COPYRIGHTED_FRAMEWORKS <= frameworks  # the guard below is not vacuous


def test_copyrighted_profiles_use_no_standard_wording() -> None:
    offenders = [
        (pid, cid) for pid, cid, d in _copyrighted_controls()
        if any(opener in d for opener in BANNED_OPENERS)
    ]
    assert offenders == []


def test_copyrighted_profile_descriptions_are_short_summaries() -> None:
    too_long = [
        (pid, cid, len(d)) for pid, cid, d in _copyrighted_controls()
        if len(d) > MAX_DESCRIPTION_CHARS
    ]
    assert too_long == []


def test_iso42001_profile_cites_only_clauses_that_exist() -> None:
    # Clause 8 of ISO/IEC 42001:2023 ends at 8.4; continual improvement is 10.1
    # (10.2 is nonconformity and corrective action).
    ids = {c.id for c in load_profile("iso42001").controls}
    assert not any(i.startswith("ISO42-8.5") for i in ids)
    assert "ISO42-10.1-CONTINUAL-IMPROVEMENT" in ids
    assert not any(i.startswith("ISO42-10.2") for i in ids)


def test_soc2_cc6_titles_match_their_criteria() -> None:
    # CC6.6 covers threats from outside the system boundary; malicious software is CC6.8.
    titles = {c.id: c.title for c in load_profile("soc2-type2").controls}
    assert "malicious" not in titles["CC6.6-THREAT-MITIGATION"].lower()
    assert "outside the system boundary" in titles["CC6.6-THREAT-MITIGATION"].lower()
