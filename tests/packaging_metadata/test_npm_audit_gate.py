"""The npm-audit severity + waiver gate (scripts/npm_audit_gate.py).

Why this exists: the Python dependency closure has had a severity + waiver
gate since ADR-0186, while the three publicly tracked npm lockfiles had no
audit at all — eight HIGH advisories sat in ``web/`` unseen (2026-09-04).
These tests pin the gate's contract the same way the CI-shape tests pin
``pip_audit_gate.py``'s invocation: every exit code, the fail-closed rule,
the dedup rule, and the waiver lifecycle.

The gate is exercised the way CI runs it — as a subprocess — so the tests
also prove the script is runnable, not merely importable.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GATE = REPO / "scripts" / "npm_audit_gate.py"

GHSA_A = "GHSA-aaaa-bbbb-cccc"
GHSA_B = "GHSA-dddd-eeee-ffff"


def run_gate(tmp_path: Path, report: dict, waivers: str, label: str = "") -> tuple[int, str, str]:
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    waiver_path = tmp_path / "waivers.toml"
    waiver_path.write_text(waivers, encoding="utf-8")
    argv = [sys.executable, str(GATE), "--report", str(report_path), "--waivers", str(waiver_path)]
    if label:
        argv += ["--label", label]
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def advisory(ghsa: str, severity: str, title: str = "a title") -> dict:
    return {
        "source": 1234,
        "name": "dep",
        "severity": severity,
        "title": title,
        "url": f"https://github.com/advisories/{ghsa}",
        "range": "<1.0.0",
    }


def report_with(packages: dict[str, list[dict | str]]) -> dict:
    return {
        "auditReportVersion": 2,
        "vulnerabilities": {
            name: {"name": name, "severity": "high", "via": via, "isDirect": False}
            for name, via in packages.items()
        },
        "metadata": {"vulnerabilities": {}},
    }


def waiver_toml(ghsa: str, expires: str, justification: str = "dev-only; tracked upstream") -> str:
    return f'[[waiver]]\nid = "{ghsa}"\njustification = "{justification}"\nexpires = "{expires}"\n'


# The gate compares expiry against TODAY IN UTC. A test computing "yesterday"
# from the local clock straddles midnight: at 01:00 CEST on the 4th, UTC is
# still the 3rd, "local yesterday" equals "UTC today", and expires < today is
# False -- the expired-waiver test then passes vacuously. Derive both dates
# from the same clock the gate uses, with margin.
_UTC_TODAY = dt.datetime.now(dt.timezone.utc).date()
FUTURE = (_UTC_TODAY + dt.timedelta(days=30)).isoformat()
PAST = (_UTC_TODAY - dt.timedelta(days=2)).isoformat()


# ---------------------------------------------------------------------------
# Exit code 0 — clean and non-blocking paths
# ---------------------------------------------------------------------------


def test_empty_report_passes(tmp_path: Path) -> None:
    code, out, err = run_gate(tmp_path, report_with({}), "")
    assert code == 0, err
    assert "0 advisory(ies)" in out


def test_moderate_and_low_report_but_do_not_block(tmp_path: Path) -> None:
    rep = report_with(
        {
            "pkg-a": [advisory(GHSA_A, "moderate")],
            "pkg-b": [advisory(GHSA_B, "low")],
        }
    )
    code, out, _ = run_gate(tmp_path, rep, "")
    assert code == 0
    assert "non-blocking (MODERATE)" in out
    assert "non-blocking (LOW)" in out


def test_waived_high_passes_until_expiry(tmp_path: Path) -> None:
    rep = report_with({"pkg-a": [advisory(GHSA_A, "high")]})
    code, out, _ = run_gate(tmp_path, rep, waiver_toml(GHSA_A, FUTURE))
    assert code == 0
    assert "waived" in out and GHSA_A in out


def test_unused_waiver_notes_but_does_not_fail(tmp_path: Path) -> None:
    code, out, _ = run_gate(tmp_path, report_with({}), waiver_toml(GHSA_B, FUTURE))
    assert code == 0
    assert "matched no advisory" in out


# ---------------------------------------------------------------------------
# Exit code 1 — blocking paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("severity", ["high", "critical"])
def test_high_and_critical_block(tmp_path: Path, severity: str) -> None:
    rep = report_with({"pkg-a": [advisory(GHSA_A, severity)]})
    code, _, err = run_gate(tmp_path, rep, "")
    assert code == 1
    assert f"BLOCKING ({severity.upper()})" in err


def test_missing_severity_fails_closed(tmp_path: Path) -> None:
    via = advisory(GHSA_A, "high")
    del via["severity"]
    rep = report_with({"pkg-a": [via]})
    code, _, err = run_gate(tmp_path, rep, "")
    assert code == 1
    assert "BLOCKING (UNKNOWN)" in err and "failing closed" in err


def test_unrecognised_severity_fails_closed(tmp_path: Path) -> None:
    rep = report_with({"pkg-a": [advisory(GHSA_A, "catastrophic")]})
    code, _, err = run_gate(tmp_path, rep, "")
    assert code == 1
    assert "BLOCKING (UNKNOWN)" in err


def test_expired_waiver_fails_even_when_nothing_matches_it(tmp_path: Path) -> None:
    """Waivers cannot rot into permanence — an expired one is itself a failure."""
    code, _, err = run_gate(tmp_path, report_with({}), waiver_toml(GHSA_A, PAST))
    assert code == 1
    assert "EXPIRED WAIVER" in err


def test_waiver_for_one_advisory_does_not_cover_another(tmp_path: Path) -> None:
    rep = report_with({"pkg-a": [advisory(GHSA_A, "high"), advisory(GHSA_B, "high")]})
    code, _, err = run_gate(tmp_path, rep, waiver_toml(GHSA_A, FUTURE))
    assert code == 1
    assert GHSA_B in err and GHSA_A not in err


# ---------------------------------------------------------------------------
# Exit code 2 — malformed input
# ---------------------------------------------------------------------------


def test_malformed_report_is_exit_2(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text("not json", encoding="utf-8")
    waiver_path = tmp_path / "waivers.toml"
    waiver_path.write_text("", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(GATE), "--report", str(report_path), "--waivers", str(waiver_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "not valid JSON" in proc.stderr


def test_report_without_vulnerabilities_object_is_exit_2(tmp_path: Path) -> None:
    code, _, err = run_gate(tmp_path, {"something": "else"}, "")
    assert code == 2
    assert "no 'vulnerabilities' object" in err


def test_waiver_with_unknown_field_is_exit_2(tmp_path: Path) -> None:
    bad = waiver_toml(GHSA_A, FUTURE).replace("[[waiver]]", '[[waiver]]\nseverity = "high"')
    rep = report_with({"pkg-a": [advisory(GHSA_A, "high")]})
    code, _, err = run_gate(tmp_path, rep, bad)
    assert code == 2
    assert "unknown field" in err


# ---------------------------------------------------------------------------
# Dedup — the property the real web/ finding exercised
# ---------------------------------------------------------------------------


def test_one_advisory_reached_via_six_packages_is_one_decision(tmp_path: Path) -> None:
    """npm attributes one advisory to every package in its chain; the real
    web/ case surfaced GHSA-jmr9-qjv8-65gv as six 'high' package entries.
    One advisory must be one line, one waiver, one decision."""
    shared = advisory(GHSA_A, "high")
    rep = report_with(
        {
            "a": [shared],
            "b": [shared, "a"],
            "c": ["b"],
            "d": ["c"],
            "e": ["d"],
            "f": ["e"],
        }
    )
    code, _, err = run_gate(tmp_path, rep, "")
    assert code == 1
    assert err.count("BLOCKING") == 1, err
    assert "1 blocking advisory(ies)" in err

    code, out, _ = run_gate(tmp_path, rep, waiver_toml(GHSA_A, FUTURE))
    assert code == 0, "one waiver must clear the whole chain"
    assert "none blocking" in out


def test_string_via_entries_are_pointers_not_advisories(tmp_path: Path) -> None:
    rep = report_with({"pkg-a": ["pkg-b"]})  # vulnerable only via another package
    code, out, _ = run_gate(tmp_path, rep, "")
    assert code == 0
    assert "0 advisory(ies)" in out


def test_advisory_without_ghsa_url_uses_source_id(tmp_path: Path) -> None:
    via = advisory(GHSA_A, "high")
    via["url"] = "https://example.com/no-ghsa-here"
    rep = report_with({"pkg-a": [via]})
    code, _, err = run_gate(tmp_path, rep, "")
    assert code == 1
    assert "npm-advisory-1234" in err
