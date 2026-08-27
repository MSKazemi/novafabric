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
"""``nova verify <bundle.zip>`` must recompute the Evidence Bundle's digests.

The bundle manifest records a `sha256` for every packaged artifact — that is what
makes the bundle tamper-evident. Nothing recomputed them, so the guarantee
shipped as prose in the bundle README and every check had to be done by hand.
These tests pin the three ways a bundle can diverge from its manifest: a modified
artifact, a missing one, and an unlisted extra.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _make_bundle(path: Path, files: dict[str, bytes]) -> Path:
    manifest = {
        "bundle_id": "bundle-test-001",
        "bundle_format": "novafabric-evidence-bundle",
        "artifacts": [
            {"path": name, "sha256": _digest(data)} for name, data in files.items()
        ],
    }
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
    return path


def _files() -> dict[str, bytes]:
    return {
        "run-capsule/capsule.yaml": b"run_id: r1\n",
        "run-capsule/model-calls.jsonl": (
            json.dumps({"gen_ai.usage.output_tokens": 77}) + "\n"
        ).encode(),
        "README.md": b"# bundle\n",
    }


def test_intact_bundle_passes(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path / "ev.zip", _files())

    result = runner.invoke(app, ["verify", str(bundle)])

    assert result.exit_code == 0, result.output
    assert "PASSED" in result.output
    assert "3 recomputed" in result.output


def test_a_modified_artifact_is_caught_and_named(tmp_path: Path) -> None:
    """The exact tamper an evidence system exists to catch: an edited token count."""
    files = _files()
    bundle = _make_bundle(tmp_path / "ev.zip", files)

    # Rebuild the ZIP with one recorded value changed, manifest untouched.
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(bundle) as src, zipfile.ZipFile(tampered, "w") as dst:
        for name in src.namelist():
            data = src.read(name)
            if name == "run-capsule/model-calls.jsonl":
                data = (json.dumps({"gen_ai.usage.output_tokens": 1}) + "\n").encode()
            dst.writestr(name, data)

    result = runner.invoke(app, ["verify", str(tampered)])

    assert result.exit_code == 1
    assert "FAILED" in result.output
    assert "model-calls.jsonl" in result.output


def test_a_missing_artifact_is_caught(tmp_path: Path) -> None:
    files = _files()
    bundle = _make_bundle(tmp_path / "ev.zip", files)
    stripped = tmp_path / "stripped.zip"
    with zipfile.ZipFile(bundle) as src, zipfile.ZipFile(stripped, "w") as dst:
        for name in src.namelist():
            if name != "README.md":
                dst.writestr(name, src.read(name))

    result = runner.invoke(app, ["verify", str(stripped)])

    assert result.exit_code == 1
    assert "missing" in result.output.lower()


def test_an_unlisted_extra_file_is_caught(tmp_path: Path) -> None:
    """Adding a file is a modification too, and must not pass silently."""
    bundle = _make_bundle(tmp_path / "ev.zip", _files())
    with zipfile.ZipFile(bundle, "a") as zf:
        zf.writestr("run-capsule/planted.jsonl", b"{}\n")

    result = runner.invoke(app, ["verify", str(bundle)])

    assert result.exit_code == 1
    assert "planted.jsonl" in result.output


def test_a_zip_without_a_manifest_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "notabundle.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("hello.txt", b"hi")

    result = runner.invoke(app, ["verify", str(path)])

    assert result.exit_code == 1
    assert "manifest.json" in result.output


def test_a_corrupt_zip_is_rejected_cleanly(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.zip"
    path.write_bytes(b"this is not a zip file at all")

    result = runner.invoke(app, ["verify", str(path)])

    assert result.exit_code == 1
    assert "ZIP" in result.output
