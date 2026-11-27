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

"""Structure and safety contract of the OpenTofu templates under ``deploy/iac/`` (#238).

The templates are validated by ``.github/workflows/iac-validate.yml`` (``tofu fmt``,
``init -backend=false``, ``validate`` and plan-only ``tofu test``), which needs the
``tofu`` binary. This guard needs nothing but Python and git, so it runs in every
unit job and fails on the properties that make the templates safe to publish:

* the neutral module declares no provider and no resource (it renders inputs only,
  so it can be validated and tested with no account);
* every provider is version-pinned, and every provider example says it is
  experimental;
* nothing defaults open: SSH and HTTPS ingress default to no CIDRs, and no ``/0``
  range appears in a ``.tf`` file except the outbound package-download default;
* no input accepts a secret value: credentials are passed by secret *name*;
* no state, real variable file or remote backend is committed;
* the CI job stays offline and credential-free and never applies;
* there is no Azure module (the project does not provide or exercise one).

HCL is checked with regular expressions over the files the public git tracks —
deliberately simple, because the authoritative parser runs in the workflow.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
IAC = "deploy/iac"
MODULE = f"{IAC}/modules/novafabric-single-host"
EXAMPLES = (f"{IAC}/examples/aws-ec2", f"{IAC}/examples/gcp-compute")
ROOTS = (MODULE, *EXAMPLES)
WORKFLOW = REPO / ".github" / "workflows" / "iac-validate.yml"

#: Input names that would carry a secret VALUE. A ``*_secret_ref`` input carries a name.
SECRET_VALUE_NAME = re.compile(r"(password|passwd|dsn|token|secret|api_key|private_key)", re.I)


def _tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", IAC],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [p for p in out.splitlines() if p]


TRACKED = _tracked()


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _tf_files(root: str) -> list[str]:
    return [p for p in TRACKED if p.startswith(root + "/") and p.endswith(".tf")]


def _all_tf() -> list[str]:
    return [p for p in TRACKED if p.endswith(".tf")]


def _variable_block(text: str, name: str) -> str | None:
    """The body of ``variable "<name>" { ... }`` (top-level blocks close at column 0)."""
    match = re.search(rf'^variable "{re.escape(name)}" \{{\n(.*?)^\}}', text, re.M | re.S)
    return match.group(1) if match else None


def test_the_tree_is_present_and_the_sweep_is_not_vacuous() -> None:
    assert len(TRACKED) >= 15, f"expected the deploy/iac tree in git, found {TRACKED}"
    for root in ROOTS:
        for name in ("versions.tf", "main.tf", "variables.tf", "outputs.tf"):
            assert f"{root}/{name}" in TRACKED, f"{root}/{name} is missing"
    assert any(p.startswith(f"{MODULE}/tests/") and p.endswith(".tftest.hcl") for p in TRACKED)
    assert f"{IAC}/README.md" in TRACKED
    for example in EXAMPLES:
        assert f"{example}/README.md" in TRACKED


def test_the_neutral_module_declares_no_provider_and_no_resource() -> None:
    for rel in _tf_files(MODULE):
        text = _read(rel)
        for pattern in (r"^\s*required_providers\b", r'^provider "', r'^resource "', r'^data "'):
            assert not re.search(pattern, text, re.M), (
                f"{rel} matches {pattern!r}: the neutral module must render inputs only, "
                "so `tofu test` and `validate` run with no account and no credentials"
            )


@pytest.mark.parametrize("example", EXAMPLES)
def test_every_provider_is_version_pinned(example: str) -> None:
    text = _read(f"{example}/versions.tf")
    sources = re.findall(r'source\s*=\s*"([^"]+)"\s*\n\s*version\s*=\s*"([^"]+)"', text)
    assert sources, f"{example}/versions.tf declares no pinned provider"
    for source, version in sources:
        assert re.fullmatch(r"~> \d+\.\d+", version), (
            f"{example}: provider {source} must be pinned as '~> MAJOR.MINOR', got {version!r}"
        )
    declared = re.findall(r"^\s*source\s*=", text, re.M)
    assert len(declared) == len(sources), f"{example}: a provider has no version constraint"


@pytest.mark.parametrize("example", EXAMPLES)
def test_every_provider_example_is_labelled_experimental(example: str) -> None:
    assert "EXPERIMENTAL" in _read(f"{example}/versions.tf")
    assert "EXPERIMENTAL" in _read(f"{example}/main.tf")
    readme = _read(f"{example}/README.md")
    assert "experimental" in readme[:600].lower(), f"{example}/README.md must open with its status"
    assert "cost" in readme.lower(), f"{example}/README.md must carry a cost warning"


def test_there_is_no_azure_module() -> None:
    offenders = [p for p in TRACKED if "azure" in p.lower()]
    for rel in TRACKED:
        if rel.endswith((".tf", ".hcl", ".tftpl", ".example")) and re.search(
            r"azurerm|azure", _read(rel), re.I
        ):
            offenders.append(rel)
    assert "azurerm" not in WORKFLOW.read_text(encoding="utf-8").lower()
    assert not offenders, f"no Azure module or provider is provided here: {offenders}"


@pytest.mark.parametrize("root", ROOTS)
@pytest.mark.parametrize("name", ["admin_cidrs", "client_cidrs"])
def test_ingress_defaults_to_closed(root: str, name: str) -> None:
    block = _variable_block(_read(f"{root}/variables.tf"), name)
    assert block is not None, f"{root} has no variable {name!r}"
    assert re.search(r"^\s*default\s*=\s*\[\]\s*$", block, re.M), (
        f"{root}: {name} must default to [] - nothing is reachable until an operator names CIDRs"
    )


def test_the_module_refuses_a_world_open_ssh_range() -> None:
    block = _variable_block(_read(f"{MODULE}/variables.tf"), "admin_cidrs")
    assert block is not None and "/0$" in block, "admin_cidrs must refuse a /0 range"
    outputs = _read(f"{MODULE}/outputs.tf")
    assert "allow_public_https" in outputs and "/0$" in outputs, (
        "a /0 client range must need the explicit allow_public_https opt-in"
    )


def test_no_world_open_range_outside_the_outbound_default() -> None:
    offenders = []
    for rel in _all_tf():
        text = _read(rel)
        egress = _variable_block(text, "egress_cidrs") or ""
        stripped = text.replace(egress, "")
        for match in re.finditer(r"0\.0\.0\.0/0|::/0", stripped):
            line = stripped.count("\n", 0, match.start()) + 1
            offenders.append(f"{rel}:{line}")
    assert not offenders, (
        "a match-everything range appears outside the egress_cidrs default; ingress must "
        f"come only from operator-named CIDRs: {offenders}"
    )


def test_the_api_binds_loopback_only() -> None:
    main = _read(f"{MODULE}/main.tf")
    assert re.search(r'^\s*listen_host\s*=\s*"127\.0\.0\.1"', main, re.M)
    unit = _read(f"{MODULE}/templates/novafabric.service.tftpl")
    assert "--host ${listen_host}" in unit
    assert "--insecure-no-auth" not in unit and "--i-know-this-is-public" not in unit


def test_no_input_accepts_a_secret_value() -> None:
    offenders = []
    for rel in _all_tf():
        for name in re.findall(r'^variable "([^"]+)"', _read(rel), re.M):
            if SECRET_VALUE_NAME.search(name) and not name.endswith("_secret_ref"):
                offenders.append(f"{rel}: {name}")
    assert not offenders, (
        "these inputs would put a secret value into user-data and the state file; "
        f"take the secret's NAME as a *_secret_ref input instead: {offenders}"
    )
    block = _variable_block(_read(f"{MODULE}/variables.tf"), "postgres_dsn_secret_ref")
    assert block is not None and "postgres(ql)?://" in block, (
        "postgres_dsn_secret_ref must refuse a pasted DSN"
    )


def test_no_state_real_tfvars_or_remote_backend_is_committed() -> None:
    forbidden = re.compile(
        r"(\.tfstate(\..*)?|\.tfplan|\.tfvars|\.tfvars\.json|\.terraform\.lock\.hcl)$"
    )
    committed = [p for p in TRACKED if forbidden.search(p) or "/.terraform/" in p]
    assert not committed, f"never commit state, plans or real variable files: {committed}"
    backends = [rel for rel in _all_tf() if re.search(r'^\s*backend\s+"', _read(rel), re.M)]
    assert not backends, f"no backend block: operators choose and secure their own: {backends}"


def test_the_installer_is_pinned_and_makes_no_update_check() -> None:
    script = _read(f"{MODULE}/templates/install-novafabric.sh.tftpl")
    assert '"novafabric[server]==${version}"' in script
    assert "--disable-pip-version-check" in script
    assert not script.startswith("#!"), "the shebang is added in main.tf (see the template)"
    block = _variable_block(_read(f"{MODULE}/variables.tf"), "novafabric_version")
    assert block is not None and "default" not in block, "the release must be chosen explicitly"


def test_the_contract_tests_are_plan_only_and_cover_the_refusals() -> None:
    tests = [p for p in TRACKED if p.startswith(f"{MODULE}/tests/") and p.endswith(".tftest.hcl")]
    text = "\n".join(_read(p) for p in tests)
    runs = re.findall(r'^run "', text, re.M)
    plans = re.findall(r"^\s*command\s*=\s*plan\s*$", text, re.M)
    assert runs and len(runs) == len(plans), "every tofu test run must be `command = plan`"
    assert not re.search(r"^\s*command\s*=\s*apply", text, re.M)
    for target in (
        "var.admin_cidrs",
        "var.postgres_dsn_secret_ref",
        "var.novafabric_version",
        "output.ingress_rules",
        "output.user_data",
    ):
        assert f"expect_failures = [{target}]" in text, f"no negative contract test for {target}"


def _workflow() -> dict:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    return doc


def test_the_ci_job_is_offline_credential_free_and_never_applies() -> None:
    raw = WORKFLOW.read_text(encoding="utf-8")
    doc = _workflow()
    assert doc.get("permissions") == {"contents": "read"}
    code = "\n".join(line.split("#", 1)[0] for line in raw.splitlines())
    assert "secrets." not in code, "the IaC job must not read any secret"
    assert "id-token" not in code, "the IaC job must not mint cloud OIDC tokens"
    assert not re.search(r"\b(tofu|terraform)\b[^\n]*\b(apply|destroy|import)\b", code)
    assert not re.search(r"\b(aws|gcloud|gsutil|az)\s", code), "no cloud CLI in the IaC job"

    (job,) = doc["jobs"].values()
    assert job["runs-on"] == "ubuntu-24.04"
    assert "permissions" not in job or job["permissions"] == {"contents": "read"}
    runs = "\n".join(str(step.get("run", "")) for step in job["steps"])
    for needed in ("tofu fmt -check", "init -backend=false", "validate", " test"):
        assert needed in runs, f"the IaC job no longer runs {needed!r}"
    for root in ROOTS:
        assert root in runs, f"the IaC job no longer validates {root}"
    uses = [str(step.get("uses", "")) for step in job["steps"] if step.get("uses")]
    assert all(re.search(r"@[0-9a-f]{40}\b", u) for u in uses), f"pin actions by SHA: {uses}"
    versions = [
        step["with"]["tofu_version"]
        for step in job["steps"]
        if "tofu_version" in (step.get("with") or {})
    ]
    assert versions and all(re.fullmatch(r"\d+\.\d+\.\d+", v) for v in versions), versions


def test_the_ops_page_states_the_maturity_honestly() -> None:
    page = (REPO / "docs" / "ops" / "infrastructure-as-code.md").read_text(encoding="utf-8")
    assert "experimental" in page.lower()
    assert "never been applied" in page.lower() or "not been applied" in page.lower()
    assert "deploy/iac/README.md" in page
