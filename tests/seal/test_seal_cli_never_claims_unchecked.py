"""``nova seal`` commands never report a check they did not make (review cli-b-04/05/06).

* ``seal sign`` (default ``--backend local``) printed a hint and exited 0 having signed
  nothing, so ``nova seal sign m.json && publish`` published an unsigned manifest.
* ``seal log verify --db <typo>`` created an empty log there and printed
  ``Consistency: OK`` — an auditor's check passed against a log that never existed.
* ``seal log verify --consistency N`` took the old root from the log's own stored
  heads, which a log holder rewrites together with the leaves, and called the
  result an append-only proof. ``--old-root`` now takes the head from outside.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.seal_propose import seal_app
from novafabric.trust.novaseal.merkle import MerkleLog

runner = CliRunner()


def _log(path: Path, leaves: list[dict[str, int]]) -> str:
    log = MerkleLog(path)
    try:
        for leaf in leaves:
            log.append(leaf)
        root = log.root_at_size(len(leaves))
    finally:
        log.close()
    assert root
    return root


def test_seal_sign_local_refuses_instead_of_pretending(tmp_path: Path) -> None:
    res = runner.invoke(seal_app, ["sign", '{"a": 1}'])
    assert res.exit_code == 2, res.output
    assert "nothing was signed" in res.output


def test_log_verify_of_a_missing_log_creates_nothing(tmp_path: Path) -> None:
    db = tmp_path / "typo-merkle.db"
    res = runner.invoke(seal_app, ["log", "verify", "--db", str(db)])
    assert res.exit_code == 3, res.output
    assert "Consistency: OK" not in res.output
    assert not db.exists()


def test_log_verify_of_an_existing_empty_log_is_ok(tmp_path: Path) -> None:
    db = tmp_path / "merkle.db"
    MerkleLog(db).close()
    res = runner.invoke(seal_app, ["log", "verify", "--db", str(db)])
    assert res.exit_code == 0, res.output


def test_consistency_without_old_root_says_internal_only(tmp_path: Path) -> None:
    db = tmp_path / "merkle.db"
    _log(db, [{"i": i} for i in range(5)])
    res = runner.invoke(seal_app, ["log", "verify", "--db", str(db), "--consistency", "3"])
    assert res.exit_code == 0, res.output
    assert "internal consistency only" in " ".join(res.output.split())


def test_old_root_proves_append_only_and_catches_a_rewritten_log(tmp_path: Path) -> None:
    genuine, rewritten = tmp_path / "genuine.db", tmp_path / "rewritten.db"
    published = _log(genuine, [{"i": i} for i in range(3)])
    log = MerkleLog(genuine)
    try:
        log.append({"i": 3})
        log.append({"i": 4})
    finally:
        log.close()
    # The same log rewritten from scratch: different history, consistent stored heads.
    _log(rewritten, [{"i": -i} for i in range(5)])

    args = ["log", "verify", "--consistency", "3", "--old-root", published]
    ok = runner.invoke(seal_app, [*args, "--db", str(genuine)])
    assert ok.exit_code == 0, ok.output
    assert "internal consistency only" not in " ".join(ok.output.split())

    caught = runner.invoke(seal_app, [*args, "--db", str(rewritten)])
    assert caught.exit_code == 2, caught.output
    # Without --old-root the rewritten log passes its own internal check.
    internal = runner.invoke(
        seal_app, ["log", "verify", "--consistency", "3", "--db", str(rewritten)]
    )
    assert internal.exit_code == 0, internal.output


def test_old_root_without_consistency_is_a_usage_error(tmp_path: Path) -> None:
    db = tmp_path / "merkle.db"
    _log(db, [{"i": 0}])
    res = runner.invoke(seal_app, ["log", "verify", "--db", str(db), "--old-root", "ab" * 32])
    assert res.exit_code == 2, res.output
