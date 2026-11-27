"""Support-bundle privacy review: who and where a bundle was made on stays out.

The redaction ruleset strips credentials. Before this slice nothing stripped the
*identity* of the machine and person: ``doctor.json`` carried the absolute
registry path (``/home/<user>/.novafabric/registry.db``), ``logs/README.txt`` the
absolute log directory, log lines the host name, and every tar entry the local
account's user and group names (``tarfile.add`` looks them up). None of those is
a secret, so no secret rule fired. These tests pin the scrub, the anonymous tar
headers and the human-readable ``MANIFEST.txt``.

Every identity value below is a fixture made up for the test — the real HOME,
user and host of the machine running the suite are replaced, never read.
"""

from __future__ import annotations

import hashlib
import json
import socket
import tarfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.support_bundle import (
    ALLOWED_MEMBERS,
    LOCAL_SCRUB_VERSION,
    MANIFEST_TEXT_MEMBER,
    MEMBER_PURPOSES,
    build_support_bundle,
)
from novafabric.support_bundle._scrub import (
    HOME_PLACEHOLDER,
    HOST_PLACEHOLDER,
    USER_PLACEHOLDER,
    LocalIdentity,
    scrub_text,
    scrub_value,
)

runner = CliRunner()

USER = "quintessa"
HOST_FQDN = "box-7f3a9.lab.example"
HOST_SHORT = "box-7f3a9"


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A HOME under tmp_path named for a made-up user, with NOVAFABRIC_HOME inside it."""
    home = tmp_path / "home" / USER
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USER", USER)
    monkeypatch.setenv("LOGNAME", USER)
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home / ".novafabric"))
    monkeypatch.delenv("NOVAFABRIC_LOG_DIR", raising=False)
    monkeypatch.delenv("NOVAFABRIC_BACKEND", raising=False)
    monkeypatch.setattr(socket, "gethostname", lambda: HOST_FQDN)
    monkeypatch.setattr("platform.node", lambda: HOST_FQDN)
    return home


def _identity(home: str = f"/home/{USER}") -> LocalIdentity:
    return LocalIdentity(homes=(home,), user=USER, hostnames=(HOST_FQDN, HOST_SHORT))


def _read(bundle: Path) -> tuple[dict[str, bytes], list[tarfile.TarInfo]]:
    contents: dict[str, bytes] = {}
    with tarfile.open(bundle, "r:gz") as tar:
        infos = tar.getmembers()
        for info in infos:
            handle = tar.extractfile(info)
            assert handle is not None
            contents[info.name] = handle.read()
    return contents, infos


# ---------- unit: scrub_text / scrub_value ----------


def test_home_directory_becomes_tilde() -> None:
    text = f"registry at /home/{USER}/.novafabric/registry.db"
    assert scrub_text(text, _identity()) == "registry at ~/.novafabric/registry.db"


def test_a_longer_name_sharing_the_home_prefix_is_not_mistaken_for_home() -> None:
    # /home/quintessa must not turn /home/quintessa2 into ~2.
    out = scrub_text(f"/home/{USER}2/data", _identity())
    assert "~2" not in out
    assert out == f"/home/{USER_PLACEHOLDER}/data"


def test_other_users_home_prefixes_lose_the_name() -> None:
    identity = LocalIdentity()
    assert scrub_text("/home/bob/x.log", identity) == f"/home/{USER_PLACEHOLDER}/x.log"
    assert scrub_text("/Users/bob/x.log", identity) == f"/Users/{USER_PLACEHOLDER}/x.log"
    assert scrub_text("C:\\Users\\bob\\x.log", identity) == f"C:\\Users\\{USER_PLACEHOLDER}\\x.log"


def test_user_name_as_a_path_segment_is_scrubbed_outside_home() -> None:
    out = scrub_text(f"capsules in /scratch/{USER}/runs", _identity())
    assert out == f"capsules in /scratch/{USER_PLACEHOLDER}/runs"


def test_this_hosts_name_is_scrubbed_full_and_short() -> None:
    line = f"bound on {HOST_FQDN}:4321; peer {HOST_SHORT} ok"
    out = scrub_text(line, _identity())
    assert HOST_SHORT not in out
    assert out == f"bound on {HOST_PLACEHOLDER}:4321; peer {HOST_PLACEHOLDER} ok"


def test_host_name_inside_a_longer_word_is_left_alone() -> None:
    identity = LocalIdentity(hostnames=("node",))
    assert scrub_text("nodes=4 subnode-1 node", identity) == f"nodes=4 subnode-1 {HOST_PLACEHOLDER}"


def test_remote_hosts_and_ips_are_kept_because_they_are_the_diagnosis() -> None:
    line = 'connection to server at "db.internal" (10.0.0.5), port 5432 failed'
    assert scrub_text(line, _identity()) == line


def test_scrub_value_walks_nested_structures_and_keeps_keys() -> None:
    payload = {
        "storage": {"db_path": f"/home/{USER}/.novafabric/registry.db", "rows": 3},
        "hosts": [HOST_FQDN, "db.internal"],
    }
    assert scrub_value(payload, _identity()) == {
        "storage": {"db_path": "~/.novafabric/registry.db", "rows": 3},
        "hosts": [HOST_PLACEHOLDER, "db.internal"],
    }


def test_an_empty_identity_changes_only_generic_home_prefixes() -> None:
    text = "plain line with no paths 01HXAY7M5JZ8R7K4P9DPBYK2WX sha256:" + "ab" * 32
    assert scrub_text(text, LocalIdentity()) == text


# ---------- unit: LocalIdentity.current, including failure cases ----------


def test_current_identity_reads_home_user_and_host(fake_home: Path) -> None:
    identity = LocalIdentity.current()
    assert str(fake_home) in identity.homes
    assert identity.user == USER
    assert set(identity.hostnames) == {HOST_FQDN, HOST_SHORT}


def test_failing_lookups_yield_an_empty_identity_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> str:
        raise OSError("no passwd entry")

    monkeypatch.setattr("getpass.getuser", boom)
    monkeypatch.setattr(socket, "gethostname", boom)
    monkeypatch.setattr("platform.node", boom)
    identity = LocalIdentity.current()
    assert identity.user is None
    assert identity.hostnames == ()


def test_generic_and_too_short_names_are_never_scrub_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "gethostname", lambda: "localhost")
    monkeypatch.setattr("platform.node", lambda: "ab")
    monkeypatch.setattr("getpass.getuser", lambda: "x")
    identity = LocalIdentity.current()
    assert identity.hostnames == ()
    assert identity.user is None


def test_a_root_home_is_never_a_scrub_target(monkeypatch: pytest.MonkeyPatch) -> None:
    # HOME=/ would otherwise rewrite every absolute path in the bundle.
    monkeypatch.setenv("HOME", "/")
    assert LocalIdentity.current().homes == ()


# ---------- integration: the built bundle ----------


def test_bundle_carries_no_home_path_user_or_host(
    fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_dir = fake_home / ".novafabric" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / f"serve-{HOST_SHORT}.log").write_text(
        f"INFO started on {HOST_FQDN} with capsule dir {fake_home}/.novafabric/capsules\n"
        f"INFO wrote /scratch/{USER}/run-1\n"
    )
    config = fake_home / "server.yaml"
    config.write_text(f"capsule_dir: {fake_home}/capsules\nhost: 127.0.0.1\n")

    out = tmp_path / "bundle.tar.gz"
    result = build_support_bundle(out, config_path=config)
    contents, infos = _read(out)

    log_member = f"logs/serve-{HOST_PLACEHOLDER}.log"
    assert log_member in contents
    assert log_member in result.members
    for name, data in contents.items():
        text = data.decode()
        for needle in (str(fake_home), USER, HOST_SHORT):
            assert needle not in name, f"{needle!r} in member name {name!r}"
            assert needle not in text, f"{needle!r} leaked into {name}"
    assert f"{HOME_PLACEHOLDER}/.novafabric/capsules" in contents[log_member].decode()
    assert "127.0.0.1" in contents["config.redacted.yaml"].decode()  # non-identity kept

    # Archive headers name no local account.
    for info in infos:
        assert info.uname == ""
        assert info.gname == ""
        assert info.uid == 0
        assert info.gid == 0


def test_no_logs_readme_does_not_reveal_the_absolute_log_dir(
    fake_home: Path, tmp_path: Path
) -> None:
    out = tmp_path / "bundle.tar.gz"
    build_support_bundle(out)
    contents, _ = _read(out)
    readme = contents["logs/README.txt"].decode()
    assert "no log files found" in readme
    assert str(fake_home) not in readme
    assert f"{HOME_PLACEHOLDER}/.novafabric/logs" in readme


def test_colliding_scrubbed_log_names_are_both_kept(fake_home: Path, tmp_path: Path) -> None:
    log_dir = fake_home / ".novafabric" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / f"{HOST_SHORT}.log").write_text("short\n")
    (log_dir / f"{HOST_FQDN}.log").write_text("full\n")
    out = tmp_path / "bundle.tar.gz"
    build_support_bundle(out)
    contents, _ = _read(out)
    logs = sorted(name for name in contents if name.endswith(".log"))
    assert len(logs) == 2, logs
    assert {contents[name].decode() for name in logs} == {"short\n", "full\n"}


# ---------- MANIFEST.txt ----------


def test_manifest_text_lists_every_member_with_a_reason(fake_home: Path, tmp_path: Path) -> None:
    out = tmp_path / "bundle.tar.gz"
    result = build_support_bundle(out)
    contents, _ = _read(out)
    text = contents[MANIFEST_TEXT_MEMBER].decode()
    for name in result.members:
        assert name in text, f"{name} missing from MANIFEST.txt"
    assert "Nothing was uploaded" in text
    assert "Check before sharing" in text
    # It is hashed in manifest.json like every other member.
    manifest = json.loads(contents["manifest.json"])
    entry = manifest["members"][MANIFEST_TEXT_MEMBER]
    assert entry["sha256"] == hashlib.sha256(contents[MANIFEST_TEXT_MEMBER]).hexdigest()
    assert manifest["local_scrub_version"] == LOCAL_SCRUB_VERSION


def test_manifest_text_says_why_a_member_is_absent(fake_home: Path, tmp_path: Path) -> None:
    out = tmp_path / "bundle.tar.gz"
    build_support_bundle(out, config_path=tmp_path / "missing.yaml")
    contents, _ = _read(out)
    assert "config.redacted.yaml" not in contents
    text = contents[MANIFEST_TEXT_MEMBER].decode()
    assert "Not in this bundle" in text
    assert "no server config file was found" in text


def test_manifest_text_warns_about_log_content_only_when_logs_are_present(
    fake_home: Path, tmp_path: Path
) -> None:
    out = tmp_path / "without.tar.gz"
    build_support_bundle(out)
    assert "prompts, responses" not in _read(out)[0][MANIFEST_TEXT_MEMBER].decode()

    log_dir = fake_home / ".novafabric" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "app.log").write_text("INFO hello\n")
    out = tmp_path / "with.tar.gz"
    build_support_bundle(out)
    assert "prompts, responses" in _read(out)[0][MANIFEST_TEXT_MEMBER].decode()


def test_every_allowlisted_member_has_a_stated_purpose() -> None:
    missing = [name for name in ALLOWED_MEMBERS if name not in MEMBER_PURPOSES]
    assert not missing, f"allowlisted member(s) with no MANIFEST.txt purpose: {missing}"


# ---------- CLI ----------


def test_cli_tells_the_user_to_review_before_sharing(fake_home: Path, tmp_path: Path) -> None:
    out = tmp_path / "cli.tar.gz"
    result = runner.invoke(app, ["support-bundle", "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert "MANIFEST.txt" in result.output
    assert "Review before sharing" in result.output
