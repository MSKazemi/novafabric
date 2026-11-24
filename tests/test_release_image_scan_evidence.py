# Copyright 2024 NovaFabric Contributors
# Apache-2.0 License
"""A promoted digest needs per-platform scan EVIDENCE, not just a green gate step (#218).

``scripts/release_image.py`` refuses to copy a candidate unless a Trivy JSON report exists
for every shipped platform that

* names the candidate DIGEST (``ArtifactName`` ends in ``@<digest>``) - a scan by tag, or a
  scan of another image, is not evidence for this one;
* names the right ARCHITECTURE (``Metadata.ImageConfig``) - Trivy silently scans the
  runner's platform (amd64) when it cannot honour ``--platform``, and a clean amd64 report
  filed under "arm64" is a false pass; and
* carries no fixable CRITICAL finding (ADR-0186 policy).

Absence is never a pass: a scan that timed out, crashed or was cancelled leaves no report.

The fixtures under ``tests/fixtures/oci-promotion/`` are real ``trivy 0.76.2`` output captured
from a synthetic two-platform image whose arm64 leg carries a fixable CRITICAL (Django
1.11.0) and whose amd64 leg is clean, plus a capture of ``trivy image --input <OCI layout>``
(``--platform`` is ignored for a multi-arch layout, so the "arm64" scan analysed amd64).
They say nothing about how a registry behaves; that is
``tests/deploy/test_release_image_scenarios.py``.

No Docker, no network.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "oci-promotion"

_spec = importlib.util.spec_from_file_location(
    "release_image", REPO / "scripts" / "release_image.py"
)
assert _spec and _spec.loader
ri = importlib.util.module_from_spec(_spec)
sys.modules["release_image"] = ri
_spec.loader.exec_module(ri)

# The digest the captured reports were produced against.
DIGEST = "sha256:85e105776d70d2d7ebab631cdfa9bc812049d69d522a4e77e2a3b4a28abb67bf"
PLATFORMS = ["linux/amd64", "linux/arm64"]
AMD64_REPORT = "trivy-report-linux-amd64.json"
ARM64_REPORT = "trivy-report-linux-arm64.json"


@pytest.fixture
def reports(tmp_path: Path) -> Path:
    """Canonical-named copies of the real reports: amd64 clean, arm64 vulnerable."""
    d = tmp_path / "reports"
    d.mkdir()
    shutil.copy(FIXTURES / "trivy-linux-amd64.clean.json", d / AMD64_REPORT)
    shutil.copy(FIXTURES / "trivy-linux-arm64.vulnerable.json", d / ARM64_REPORT)
    return d


def _clean_arm64(reports: Path) -> None:
    """A genuine clean arm64 report: re-label the clean amd64 capture."""
    data = json.loads((FIXTURES / "trivy-linux-amd64.clean.json").read_text())
    data["Metadata"]["ImageConfig"]["architecture"] = "arm64"
    (reports / ARM64_REPORT).write_text(json.dumps(data))


def _edit(path: Path, fn: Any) -> None:
    data = json.loads(path.read_text())
    fn(data)
    path.write_text(json.dumps(data))


# ----------------------------------------------------------------- the verdict


def test_clean_report_for_the_right_digest_and_platform_passes(reports: Path) -> None:
    ri.check_report(reports / AMD64_REPORT, DIGEST, "linux/amd64")


def test_a_fixable_critical_blocks_only_its_own_platform(reports: Path) -> None:
    ri.check_report(reports / AMD64_REPORT, DIGEST, "linux/amd64")
    with pytest.raises(ri.GateError, match="CVE-2019-14234"):
        ri.check_report(reports / ARM64_REPORT, DIGEST, "linux/arm64")


def test_one_failing_architecture_prohibits_promotion(reports: Path) -> None:
    with pytest.raises(ri.GateError, match="linux/arm64"):
        ri.check_reports(reports, DIGEST, PLATFORMS)


def test_both_platform_reports_clean_makes_promotion_eligible(reports: Path) -> None:
    _clean_arm64(reports)
    ri.check_reports(reports, DIGEST, PLATFORMS)


def test_the_first_failing_platform_does_not_hide_the_second(reports: Path) -> None:
    """Both platforms are judged; the message names every failing one."""
    _edit(reports / AMD64_REPORT, lambda d: d["Results"].append(_critical(d)))
    with pytest.raises(ri.GateError) as err:
        ri.check_reports(reports, DIGEST, PLATFORMS)
    assert "linux/amd64" in str(err.value) and "linux/arm64" in str(err.value)


def _critical(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "Target": "x",
        "Vulnerabilities": [
            {
                "VulnerabilityID": "CVE-0000-0001",
                "PkgName": "libx",
                "InstalledVersion": "1",
                "FixedVersion": "2",
                "Severity": "CRITICAL",
            }
        ],
    }


def test_an_unfixed_critical_and_a_fixable_high_do_not_block(reports: Path) -> None:
    """ADR-0186: the gate is fixable CRITICAL only. Everything else is reported, not blocking."""
    _clean_arm64(reports)

    def add(data: dict[str, Any]) -> None:
        crit = _critical(data)
        unfixed = {**crit["Vulnerabilities"][0], "FixedVersion": ""}
        high = {**crit["Vulnerabilities"][0], "Severity": "HIGH"}
        data["Results"] = [{"Target": "x", "Vulnerabilities": [unfixed, high]}]

    for name in (AMD64_REPORT, ARM64_REPORT):
        _edit(reports / name, add)
    ri.check_reports(reports, DIGEST, PLATFORMS)


def test_a_missing_report_is_a_failure_not_a_pass(reports: Path) -> None:
    """A scan that timed out or crashed leaves no report; absence must never read as clean."""
    (reports / ARM64_REPORT).unlink()
    with pytest.raises(ri.GateError, match=r"no scan report.*linux/arm64"):
        ri.check_reports(reports, DIGEST, PLATFORMS)


@pytest.mark.parametrize(
    "content",
    ["", "not json", "[]", "null", '{"SchemaVersion": 2}', '{"ArtifactName": "x"}'],
    ids=["empty", "garbage", "list", "null", "no-artifact", "no-metadata"],
)
def test_a_report_that_does_not_parse_as_a_verdict_is_a_failure(
    reports: Path, content: str
) -> None:
    (reports / AMD64_REPORT).write_text(content)
    with pytest.raises(ri.GateError):
        ri.check_report(reports / AMD64_REPORT, DIGEST, "linux/amd64")


def test_a_report_for_a_different_digest_is_a_failure(reports: Path) -> None:
    other = "sha256:" + "0" * 64
    with pytest.raises(ri.GateError, match="different image"):
        ri.check_report(reports / AMD64_REPORT, other, "linux/amd64")


def test_a_report_scanned_by_tag_not_digest_is_a_failure(reports: Path) -> None:
    _edit(
        reports / AMD64_REPORT,
        lambda d: d.update(ArtifactName="localhost:5001/candidate:candidate"),
    )
    with pytest.raises(ri.GateError, match="different image"):
        ri.check_report(reports / AMD64_REPORT, DIGEST, "linux/amd64")


def test_a_digest_that_only_contains_the_candidate_digest_is_not_a_match(reports: Path) -> None:
    """``endswith`` on the digest, not a substring: ``…@sha256:<d>x`` is another image."""
    _edit(reports / AMD64_REPORT, lambda d: d.update(ArtifactName=d["ArtifactName"] + "0"))
    with pytest.raises(ri.GateError, match="different image"):
        ri.check_report(reports / AMD64_REPORT, DIGEST, "linux/amd64")


def test_the_real_trivy_layout_false_pass_is_caught_by_the_architecture_check() -> None:
    """Measured on trivy 0.76.2: ``--input <oci layout>`` ignores ``--platform`` for a
    multi-arch index, so the arm64 scan silently analysed amd64 and reported 0 findings.
    That report is clean and well-formed; only its architecture gives it away."""
    data = json.loads((FIXTURES / "trivy-layout-ignores-platform.json").read_text())
    data["ArtifactName"] = f"localhost:5001/candidate@{DIGEST}"  # as if scanned by digest
    assert data["Metadata"]["ImageConfig"]["architecture"] == "amd64"
    with pytest.raises(ri.GateError, match="architecture"):
        ri.check_report_data(data, DIGEST, "linux/arm64", source="layout")


def test_a_report_for_the_wrong_architecture_is_a_failure(reports: Path) -> None:
    """The same clean amd64 report filed as the arm64 one (TRIVY_PLATFORM never reached trivy)."""
    shutil.copy(reports / AMD64_REPORT, reports / ARM64_REPORT)
    with pytest.raises(ri.GateError, match="architecture"):
        ri.check_report(reports / ARM64_REPORT, DIGEST, "linux/arm64")


def test_a_report_for_the_wrong_os_is_a_failure(reports: Path) -> None:
    _edit(reports / AMD64_REPORT, lambda d: d["Metadata"]["ImageConfig"].update(os="windows"))
    with pytest.raises(ri.GateError, match="architecture"):
        ri.check_report(reports / AMD64_REPORT, DIGEST, "linux/amd64")


def test_a_shipped_platform_without_a_report_is_a_failure_even_if_others_are_clean(
    reports: Path,
) -> None:
    _clean_arm64(reports)
    with pytest.raises(ri.GateError, match=r"no scan report.*linux/s390x"):
        ri.check_reports(reports, DIGEST, [*PLATFORMS, "linux/s390x"])


# ----------------------------------------------------------------- the CLI surface


def _cli(*args: str) -> int:
    return int(ri.main(list(args)))


def test_check_reports_cli_exit_codes(reports: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = ["check-reports", "--digest", DIGEST, "--report-dir", str(reports)]
    plats = [x for p in PLATFORMS for x in ("--platform", p)]
    assert _cli(*base, *plats) == 1  # arm64 is vulnerable
    err = capsys.readouterr().err
    assert "::error" in err and "CVE-2019-14234" in err
    _clean_arm64(reports)
    assert _cli(*base, *plats) == 0


def test_check_reports_cli_refuses_a_bad_digest_before_reading_anything(reports: Path) -> None:
    assert (
        _cli(
            "check-reports",
            "--digest",
            "",
            "--platform",
            "linux/amd64",
            "--report-dir",
            str(reports),
        )
        == 1
    )
    assert (
        _cli(
            "check-reports",
            "--digest",
            "latest",
            "--platform",
            "linux/amd64",
            "--report-dir",
            str(reports),
        )
        == 1
    )


def test_check_reports_cli_requires_a_report_dir_and_a_platform() -> None:
    with pytest.raises(SystemExit) as no_dir:
        _cli("check-reports", "--digest", DIGEST, "--platform", "linux/amd64")
    assert no_dir.value.code == 2
    with pytest.raises(SystemExit) as no_plat:
        _cli("check-reports", "--digest", DIGEST, "--report-dir", ".")
    assert no_plat.value.code == 2


def test_promote_cannot_be_run_without_scan_evidence() -> None:
    """``--report-dir`` is mandatory on promote: the evidence check cannot be skipped."""
    with pytest.raises(SystemExit) as exc:
        _cli(
            "promote",
            "--src-repo",
            "localhost:5000/x",
            "--dest-repo",
            "ghcr.io/o/x",
            "--digest",
            DIGEST,
            "--platform",
            "linux/amd64",
            "--platform",
            "linux/arm64",
        )
    assert exc.value.code == 2


def test_report_file_names_are_one_per_platform() -> None:
    assert ri.report_name("linux/amd64") == AMD64_REPORT
    assert ri.report_name("linux/arm64") == ARM64_REPORT
