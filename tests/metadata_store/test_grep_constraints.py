"""FR-13: RLS primitives confined to src/novafabric/metadata_store/ and tests/metadata_store/."""
from __future__ import annotations

import subprocess


def test_no_session_set_in_metadata_store():
    """FR-07: no session-scoped 'SET app.' (without LOCAL) appears in metadata_store/ source."""
    result = subprocess.run(
        ["grep", "-rnE", r"\bSET\s+(?!LOCAL\s+)app\.", "src/novafabric/metadata_store/"],
        capture_output=True, text=True,
    )
    # grep exit code 1 = no matches = PASS
    assert result.returncode != 0, (
        "Session-scoped 'SET app.' found in metadata_store/ — must always use SET LOCAL:\n"
        + result.stdout
    )


def test_rls_strings_confined_to_metadata_store():
    """FR-13: current_setting(app.) and FORCE ROW LEVEL must not appear outside metadata_store/."""
    result = subprocess.run(
        ["grep", "-rnE", r"current_setting\(.*app\.|FORCE\s+ROW\s+LEVEL",
         "--include=*.py", "src/", "tests/"],
        capture_output=True, text=True,
    )
    lines = result.stdout.splitlines()
    allowed_prefixes = [
        "src/novafabric/metadata_store/",
        "tests/metadata_store/",
        "tests/fixtures/broken_session_set.py",
    ]
    violations = [
        line for line in lines
        if line and not any(line.startswith(p) for p in allowed_prefixes)
    ]
    assert not violations, (
        "RLS primitives found outside allowed paths:\n" + "\n".join(violations)
    )
