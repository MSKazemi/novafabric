"""Every description of the Evidence Bundle's contents matches the ZIP the code builds.

Before 2026-10-09 the bundle was described three ways: "the capsule, a lineage
subgraph, in-toto DSSE attestations, ed25519 signatures and vendored JSON schemas"
(README, concepts, getting started, user guide), "the capsule, its seal, a manifest
of file hashes and an in-toto DSSE statement" (the auditor tutorial — there are three
or four statements, not one), and "capsule.yaml, event_log.jsonl, and lineage data"
(`nova export-evidence --help` — no bundle has ever held an ``event_log.jsonl``).

The authoritative list is the "What is in the bundle" table in
``docs/architecture/sealing-and-verification.md``; every other page summarises it in
the same terms and links to it. This file builds a real bundle and holds the table,
the summaries and ``--help`` to it.
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

REPO = Path(__file__).resolve().parents[2]
CANONICAL = REPO / "docs/architecture/sealing-and-verification.md"
ANCHOR = "sealing-and-verification.md#what-is-in-the-bundle"

#: Pages that describe what the bundle contains. Each must link to the canonical list.
SUMMARIES: tuple[str, ...] = (
    "README.md",
    "docs/getting-started.md",
    "docs/concepts.md",
    "docs/user-guide.md",
    "docs/cli-reference.md",
    "docs/tutorials/why-novafabric.md",
    "docs/tutorials/prove-a-run-to-an-auditor.md",
)

#: Optional entries: present only under a flag or a capsule stream the test does not use.
OPTIONAL_ENTRIES = {"manifest.dsse.tsr"}  # --timestamp
OPTIONAL_ATTESTATIONS = {"energy"}  # energy-receipts.jsonl


def _canonical_table() -> list[tuple[str, str]]:
    text = CANONICAL.read_text(encoding="utf-8")
    section = text.split("### What is in the bundle", 1)[1].split("\n## ", 1)[0]
    rows = re.findall(r"^\| `([^`]+)` \| (.+) \|$", section, re.M)
    assert rows, "the canonical 'What is in the bundle' table is gone"
    return rows


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> zipfile.ZipFile:
    from novafabric.capture.orchestrator import CaptureOrchestrator
    from novafabric.evidence import bundle as bundle_mod
    from novafabric.evidence.signing import LocalSigner, generate_keypair

    tmp = tmp_path_factory.mktemp("bundle")
    mp = pytest.MonkeyPatch()
    mp.setenv("NOVAFABRIC_HOME", str(tmp / "home"))
    # The policy gate appends to the audit log; keep it in this module-scoped tmp dir.
    mp.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(tmp / "audit.jsonl"))
    try:
        cap = CaptureOrchestrator(base_dir=tmp / "runs").run(
            command=[sys.executable, "-c", "pass"]
        ).capsule_dir
        priv, _ = generate_keypair(tmp / "keys")
        out = tmp / "bundle.zip"
        bundle_mod.EvidenceBundleBuilder(cap, LocalSigner(priv), out).build()
    finally:
        mp.undo()
    return zipfile.ZipFile(out)


def test_canonical_table_lists_exactly_the_bundles_top_level_entries(
    bundle: zipfile.ZipFile,
) -> None:
    built = {name.split("/")[0] for name in bundle.namelist()}
    documented = {entry.split("/")[0] for entry, _ in _canonical_table()}
    assert built == documented - OPTIONAL_ENTRIES


def test_canonical_table_names_every_attestation(bundle: zipfile.ZipFile) -> None:
    built = {
        Path(name).name.removesuffix(".intoto.json")
        for name in bundle.namelist()
        if name.startswith("attestations/")
    }
    row = dict(_canonical_table())["attestations/"]
    documented = set(re.findall(r"`(\w+)`", row)) - {"energy-receipts"}
    assert built == documented - OPTIONAL_ATTESTATIONS


def test_canonical_table_says_the_capsule_is_copied_whole(bundle: zipfile.ZipFile) -> None:
    copied = {
        name.split("/")[1] for name in bundle.namelist() if name.startswith("run-capsule/")
    }
    row = dict(_canonical_table())["run-capsule/"]
    assert "whole capsule directory" in row
    for name in ("capsule.yaml", "env.lock", "redaction-proof.json"):
        assert name in copied and f"`{name}`" in row


@pytest.mark.parametrize("rel", SUMMARIES)
def test_every_summary_links_to_the_canonical_list(rel: str) -> None:
    text = (REPO / rel).read_text(encoding="utf-8")
    assert ANCHOR in text, f"{rel} describes the bundle but does not link to {ANCHOR}"


@pytest.mark.parametrize("rel", SUMMARIES)
def test_no_summary_uses_a_retired_description(rel: str) -> None:
    text = " ".join((REPO / rel).read_text(encoding="utf-8").split())
    # (``event_log.jsonl`` is checked on --help only: the CLI reference legitimately
    # names it as the legacy capsule file `nova migrate` renames.)
    for retired in (
        "a manifest of file hashes and an in-toto DSSE statement",
        "in-toto DSSE attestations, ed25519 signatures, and vendored JSON schemas",
    ):
        assert retired not in text, f"{rel} still says {retired!r}"


def test_export_evidence_help_names_only_what_the_bundle_holds(
    bundle: zipfile.ZipFile,
) -> None:
    from novafabric.cli.main import app

    result = CliRunner().invoke(app, ["export-evidence", "--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "event_log" not in result.output
    assert "run-capsule/" in result.output
    assert "run-capsule" in {name.split("/")[0] for name in bundle.namelist()}
