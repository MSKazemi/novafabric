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

"""The dashboard's connect defaults must match what ``nova serve`` actually does.

Found 2026-09-04 by opening the dashboard: the connect form pre-filled
``http://127.0.0.1:4444`` while ``nova serve`` binds **4321**, so the very first screen a
user meets pointed at nothing and reported *"Token rejected"* — a misleading error for a
port mistake. The same screen told them to run ``cat ~/.novafabric/.serve-token``, which is
the wrong path whenever ``NOVAFABRIC_HOME`` is set (it was, on the machine where this was
found, so the file genuinely did not exist).

Both are the same defect class this repo keeps meeting: **two surfaces describing one
contract, drifting apart because nothing compared them.** A constant in a `.tsx` file and a
default in a Typer signature have no reason to stay in step on their own.

These are string checks over source rather than a rendered-UI test on purpose: the point is
to fail in the fast tier the moment someone edits one side, not to prove React renders.
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVE_CLI = REPO_ROOT / "src" / "novafabric" / "cli" / "serve.py"
CONNECT_PANEL = REPO_ROOT / "web" / "src" / "components" / "dashboard" / "ConnectPanel.tsx"
DASHBOARD_APP = REPO_ROOT / "web" / "src" / "components" / "dashboard" / "DashboardApp.tsx"

#: The token file lives at ``$NOVAFABRIC_HOME/.serve-token`` (``_paths.nova_home()``), which
#: only equals ``~/.novafabric`` when the variable is unset. Any UI text naming the path has
#: to say so, or it sends people to a file that is not there.
NOVA_HOME_ENV = "NOVAFABRIC_HOME"


def _cli_default_port() -> int:
    """The port `nova serve` binds when none is given, read from its signature."""
    src = SERVE_CLI.read_text(encoding="utf-8")
    match = re.search(r"\]\s*=\s*(\d{2,5}),", src)
    assert match, (
        f"could not find the default port in {SERVE_CLI.relative_to(REPO_ROOT)} — "
        "the signature shape changed, so this guard needs updating rather than deleting"
    )
    return int(match.group(1))


def test_the_cli_default_port_is_discoverable() -> None:
    """Without this, a parse failure would make the comparison below vacuous."""
    port = _cli_default_port()
    assert 1024 <= port <= 65535, f"implausible default port parsed: {port}"


@pytest.mark.skipif(not CONNECT_PANEL.exists(), reason="dashboard source not present")
def test_the_connect_form_defaults_to_the_port_nova_serve_binds() -> None:
    """A pre-filled wrong port surfaces as 'Token rejected', which sends the user hunting
    for a credential problem that does not exist."""
    port = _cli_default_port()
    text = CONNECT_PANEL.read_text(encoding="utf-8")

    wrong = sorted(
        {
            m
            for m in re.findall(r"127\.0\.0\.1:(\d{2,5})", text)
            if int(m) != port
        }
    )
    assert not wrong, (
        f"ConnectPanel.tsx offers 127.0.0.1:{', '.join(wrong)} but `nova serve` binds "
        f"{port} by default. A user who runs `nova serve` and opens the dashboard gets a "
        "form pointing at nothing, and the failure reads as a rejected token."
    )
    assert f"127.0.0.1:{port}" in text, (
        f"ConnectPanel.tsx never mentions the actual default port {port}"
    )


@pytest.mark.skipif(
    not (CONNECT_PANEL.exists() and DASHBOARD_APP.exists()),
    reason="dashboard source not present",
)
@pytest.mark.parametrize("path", [CONNECT_PANEL, DASHBOARD_APP], ids=lambda p: p.name)
def test_ui_text_naming_the_token_file_honours_nova_home(path: pathlib.Path) -> None:
    """`~/.novafabric/.serve-token` is only correct when NOVAFABRIC_HOME is unset.

    The UI cannot read the environment, so it must not state the resolved path as if it
    were fixed. Naming the variable is the honest form — and it is the difference between
    an instruction that works and one that returns 'No such file or directory'.
    """
    text = path.read_text(encoding="utf-8")
    if ".serve-token" not in text:
        pytest.skip(f"{path.name} does not name the token file")

    mentions_bare_home = re.search(r"~/\.novafabric/\.serve-token", text) is not None
    mentions_env = NOVA_HOME_ENV in text

    assert mentions_env, (
        f"{path.name} names .serve-token but never mentions {NOVA_HOME_ENV}. The file is "
        f"at ${NOVA_HOME_ENV}/.serve-token (_paths.nova_home()), so this text is wrong for "
        "anyone who sets that variable — they follow the instruction and get nothing.\n"
        "Say ${NOVAFABRIC_HOME:-$HOME/.novafabric}/.serve-token, or name the variable "
        "alongside the default."
    )
    if mentions_bare_home:
        # Keeping the default as a *clarification* is fine; presenting it as the only
        # answer is what broke. Require the variable to appear first.
        assert text.index(NOVA_HOME_ENV) < text.index("~/.novafabric/.serve-token"), (
            f"{path.name} leads with ~/.novafabric/.serve-token and mentions "
            f"{NOVA_HOME_ENV} only afterwards; a reader takes the first path they see."
        )
