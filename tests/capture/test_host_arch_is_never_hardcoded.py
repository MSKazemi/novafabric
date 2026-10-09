"""``host.arch`` is measured, never written as a literal.

Found 2026-10-09: every framework adapter and the ``@novafabric.agent`` SDK
decorator wrote ``"arch": "x86_64"`` into ``capsule.yaml:host`` — a constant, so
an adapter capsule recorded on an arm64 laptop claimed an x86_64 host. That is
false evidence in the one block whose job is to say where a run happened.

One normalisation (``capture.env.host_arch``) now serves every writer: the main
``nova capture`` path, ``env.lock``, the OTLP importer, the adapters and the SDK
decorator. The static guard below fails on any ``"arch": "<literal>"`` in
``src/novafabric``; the dynamic tests prove an adapter capsule and an SDK
capsule record the same value the main capture path does.
"""

from __future__ import annotations

import ast
import platform
from pathlib import Path

import pytest
import yaml

from novafabric.capture import env as capture_env
from novafabric.capture.orchestrator import _build_host_info

_SRC = Path(__file__).resolve().parents[2] / "src" / "novafabric"


def _hardcoded_arch_literals() -> list[str]:
    hits: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "arch"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    hits.append(f"{path.relative_to(_SRC)}:{value.lineno}: {value.value!r}")
    return hits


def test_no_capsule_writer_hardcodes_an_arch_literal() -> None:
    hits = _hardcoded_arch_literals()
    assert not hits, (
        "host.arch must come from novafabric.capture.env.host_arch(), not a "
        "literal (false evidence on any other architecture):\n  " + "\n  ".join(hits)
    )


def test_the_guard_can_see_a_literal(tmp_path: Path) -> None:
    """Guard the guard: the scan must flag the exact shape the bug had."""
    bad = ast.parse('host = {"os": "linux", "arch": "x86_64"}')
    found = [
        v.value
        for n in ast.walk(bad)
        if isinstance(n, ast.Dict)
        for k, v in zip(n.keys, n.values)
        if isinstance(k, ast.Constant) and k.value == "arch" and isinstance(v, ast.Constant)
    ]
    assert found == ["x86_64"]
    assert len(list(_SRC.rglob("*.py"))) > 100, "the scan walked an empty tree"


@pytest.mark.parametrize(
    ("machine", "expected"),
    [("aarch64", "arm64"), ("arm64", "arm64"), ("AMD64", "x86_64"), ("x86_64", "x86_64")],
)
def test_host_arch_normalises(
    monkeypatch: pytest.MonkeyPatch, machine: str, expected: str
) -> None:
    monkeypatch.setattr(platform, "machine", lambda: machine)
    assert capture_env.host_arch() == expected


def test_an_unmapped_machine_is_recorded_as_reported_not_as_x86_64(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "machine", lambda: "armv7l")
    assert capture_env.host_arch() == "armv7l"
    monkeypatch.setattr(platform, "machine", lambda: "")
    assert capture_env.host_arch() == "unknown"


def test_the_main_capture_path_uses_the_shared_normalisation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "machine", lambda: "aarch64")
    assert _build_host_info()["arch"] == "arm64"


def test_an_adapter_capsule_records_the_main_capture_paths_arch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.adapters._capsule import begin_capture

    # Pretend to be an arm64 host: the pre-fix constant reads x86_64 here.
    monkeypatch.setattr(platform, "machine", lambda: "aarch64")
    cap = begin_capture(framework="langgraph", run_name="demo", data_dir=tmp_path)
    cap.finish()

    manifest = yaml.safe_load((cap.cap_dir / "capsule.yaml").read_text())
    assert manifest["host"]["arch"] == _build_host_info()["arch"] == "arm64"


def test_an_sdk_agent_capsule_records_the_main_capture_paths_arch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.sdk import agent

    monkeypatch.setattr(platform, "machine", lambda: "aarch64")

    @agent(name="demo", version="1", capsule_dir=tmp_path)
    def work() -> str:
        return "ok"

    assert work() == "ok"
    manifests = list(tmp_path.rglob("capsule.yaml"))
    assert len(manifests) == 1, manifests
    manifest = yaml.safe_load(manifests[0].read_text())
    assert manifest["host"]["arch"] == _build_host_info()["arch"] == "arm64"
