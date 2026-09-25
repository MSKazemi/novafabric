"""ADR-0122 P4: the portable session bundle — export, verify, import.

Acceptance criteria under test:

- export carries ``session.json`` byte-for-byte plus every member capsule
  (including empty directories) and a digest index whose ``manifest_hash``
  and per-member ``capsule_hash`` reuse the Evidence Bundle primitives;
- export is deterministic (byte-identical archives) and atomic (no partial
  file on refusal);
- export refuses an empty session, a missing/tampered member, a symlink, a
  non-ULID run_id, and resource-ceiling overruns;
- verify detects every single-byte modification, missing and unlisted files,
  an edited index, a swapped session.json, and member/ref mismatches;
- import rejects path traversal (``..``, absolute, backslash, drive letter),
  symlink entries, duplicates, and bombs before anything lands; a verified
  import is resolvable by ``show``/``replay`` and never overwrites.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import zipfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from novafabric.evidence.merkle import capsule_merkle_root
from novafabric.session import (
    BundleLimits,
    SessionBundleError,
    UnsafeBundleMemberError,
    add_member,
    export_session_bundle,
    import_session_bundle,
    load_session,
    new_session,
    resolve_members,
    save_session,
    verify_session_bundle,
)
from novafabric.session import bundle as bundle_mod

RUN_1 = "01HZ8T0A00YZ2K7N9DPBYK2W01"
RUN_2 = "01HZ8T1B00YZ2K7N9DPBYK2W02"


def make_capsule(base: Path, run_id: str) -> Path:
    capsule_dir = base / run_id
    (capsule_dir / "outputs").mkdir(parents=True)
    (capsule_dir / "inputs").mkdir()
    (capsule_dir / "capsule.yaml").write_text(
        yaml.dump(
            {
                "schema_version": "1.0.0",
                "run_id": run_id,
                "created_at": "2026-07-15T09:00:00.000000Z",
                "status": "success",
            }
        )
    )
    (capsule_dir / "model-calls.jsonl").write_text('{"call": 1}\n')
    (capsule_dir / "outputs" / "answer.txt").write_text(f"answer from {run_id}\n")
    return capsule_dir


def build_session(
    tmp_path: Path, run_ids: tuple[str, ...] = (RUN_1, RUN_2)
) -> tuple[str, Path, Path]:
    root = tmp_path / "sessions"
    caps = tmp_path / "caps"
    manifest = new_session(kind="conversation")
    save_session(manifest, root=root)
    for run_id in run_ids:
        add_member(manifest, make_capsule(caps, run_id), root=root)
    save_session(manifest, root=root)
    return manifest.session_id, root, caps


def _rezip(
    src: Path, dst: Path, mutate: dict[str, bytes | None], extra: dict[str, bytes] | None = None
) -> None:
    """Copy a bundle, replacing (bytes) or dropping (None) named members."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for info in zin.infolist():
            if info.filename in mutate:
                if mutate[info.filename] is None:
                    continue
                zout.writestr(info.filename, mutate[info.filename] or b"")
            else:
                zout.writestr(info, zin.read(info))
        for name, data in (extra or {}).items():
            zout.writestr(name, data)


@pytest.fixture()
def exported(tmp_path: Path) -> tuple[str, Path, Path]:
    sid, root, caps = build_session(tmp_path)
    out = tmp_path / "out" / "session.zip"
    export_session_bundle(sid, out, root=root, capsule_base=caps)
    return sid, root, out


class TestExport:
    def test_layout_and_index(self, tmp_path: Path) -> None:
        sid, root, caps = build_session(tmp_path)
        out = tmp_path / "b.zip"
        report = export_session_bundle(sid, out, root=root, capsule_base=caps)
        assert report.members == 2
        assert report.session_id == sid
        assert report.archive_sha256 == "sha256:" + hashlib.sha256(out.read_bytes()).hexdigest()

        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
            assert names[-1] == "session-bundle.json"
            assert names[:-1] == sorted(names[:-1])
            assert f"capsules/{RUN_1}/inputs/" in names  # empty dir kept
            assert zf.read("session.json") == (root / sid / "session.json").read_bytes()
            for info in zf.infolist():
                assert info.date_time == (1980, 1, 1, 0, 0, 0)
            index = json.loads(zf.read("session-bundle.json"))

        assert index["bundle_kind"] == "session-bundle"
        assert index["session_id"] == sid
        assert [m["run_id"] for m in index["members"]] == [RUN_1, RUN_2]
        # reused Evidence Bundle primitives
        from novafabric.evidence.bundle import _canonical_manifest_hash

        assert index["manifest_hash"] == _canonical_manifest_hash(index)
        assert index["members"][0]["capsule_hash"] == capsule_merkle_root(caps / RUN_1)
        assert report.files == len(index["artifacts"])

    def test_deterministic_bytes(self, tmp_path: Path) -> None:
        sid, root, caps = build_session(tmp_path)
        a, b = tmp_path / "a.zip", tmp_path / "b.zip"
        export_session_bundle(sid, a, root=root, capsule_base=caps)
        (caps / RUN_1 / "outputs" / "answer.txt").touch()  # mtime change only
        export_session_bundle(sid, b, root=root, capsule_base=caps)
        assert a.read_bytes() == b.read_bytes()

    def test_refuses_empty_session(self, tmp_path: Path) -> None:
        manifest = new_session()
        save_session(manifest, root=tmp_path)
        with pytest.raises(SessionBundleError, match="no members"):
            export_session_bundle(manifest.session_id, tmp_path / "x.zip", root=tmp_path)
        assert not (tmp_path / "x.zip").exists()

    def test_refuses_missing_and_tampered_members(self, tmp_path: Path) -> None:
        sid, root, caps = build_session(tmp_path)
        (caps / RUN_1 / "capsule.yaml").write_text("run_id: tampered\n")
        import shutil

        shutil.rmtree(caps / RUN_2)
        with pytest.raises(SessionBundleError, match="tampered.*missing"):
            export_session_bundle(sid, tmp_path / "x.zip", root=root, capsule_base=caps)
        assert not (tmp_path / "x.zip").exists()

    def test_refuses_symlink(self, tmp_path: Path) -> None:
        sid, root, caps = build_session(tmp_path)
        (caps / RUN_1 / "outputs" / "link").symlink_to("/etc/passwd")
        with pytest.raises(SessionBundleError, match="symlink"):
            export_session_bundle(sid, tmp_path / "x.zip", root=root, capsule_base=caps)

    def test_refuses_non_ulid_run_id(self, tmp_path: Path) -> None:
        sid, root, caps = build_session(tmp_path, (RUN_1,))
        manifest = load_session(sid, root=root)
        weird = make_capsule(caps, "../escape")
        add_member(manifest, weird, root=root)
        save_session(manifest, root=root)
        with pytest.raises(SessionBundleError, match="not a ULID"):
            export_session_bundle(sid, tmp_path / "x.zip", root=root, capsule_base=caps)

    def test_byte_and_entry_ceilings(self, tmp_path: Path) -> None:
        sid, root, caps = build_session(tmp_path)
        with pytest.raises(SessionBundleError, match="byte ceiling"):
            export_session_bundle(
                sid,
                tmp_path / "x.zip",
                root=root,
                capsule_base=caps,
                limits=BundleLimits(max_total_bytes=10),
            )
        with pytest.raises(SessionBundleError, match="archive entries"):
            export_session_bundle(
                sid,
                tmp_path / "x.zip",
                root=root,
                capsule_base=caps,
                limits=BundleLimits(max_entries=3),
            )
        assert not (tmp_path / "x.zip").exists()
        assert not list(tmp_path.glob(".x.zip.*"))  # temp file cleaned

    def test_member_changed_during_export_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sid, root, caps = build_session(tmp_path)
        real = bundle_mod._write_entry

        def racing(zf, arcname, source, budget):  # type: ignore[no-untyped-def]
            if arcname.endswith(f"{RUN_2}/capsule.yaml"):
                source.write_text("run_id: changed-mid-export\n")
            return real(zf, arcname, source, budget)

        monkeypatch.setattr(bundle_mod, "_write_entry", racing)
        with pytest.raises(SessionBundleError, match="changed during export"):
            export_session_bundle(sid, tmp_path / "x.zip", root=root, capsule_base=caps)


class TestVerify:
    def test_clean_bundle_verifies(self, exported: tuple[str, Path, Path]) -> None:
        sid, _root, out = exported
        report = verify_session_bundle(out)
        assert report.ok, report.problems
        assert report.session_id == sid
        assert report.members == 2
        assert report.files_checked > 4

    def test_every_single_byte_modification_is_named(
        self, exported: tuple[str, Path, Path], tmp_path: Path
    ) -> None:
        _sid, _root, out = exported
        target = f"capsules/{RUN_2}/outputs/answer.txt"
        bad = tmp_path / "bad.zip"
        _rezip(out, bad, {target: b"answer from someone else\n"})
        report = verify_session_bundle(bad)
        assert not report.ok
        assert f"modified: {target}" in report.problems
        assert any("capsule_hash mismatch" in p for p in report.problems)

    def test_missing_unlisted_and_index_edits(
        self, exported: tuple[str, Path, Path], tmp_path: Path
    ) -> None:
        _sid, _root, out = exported
        bad = tmp_path / "bad.zip"
        with zipfile.ZipFile(out) as zf:
            index = json.loads(zf.read("session-bundle.json"))
        index["created_by"]["version"] = "forged"
        _rezip(
            out,
            bad,
            {
                f"capsules/{RUN_1}/model-calls.jsonl": None,
                "session-bundle.json": json.dumps(index).encode(),
            },
            extra={"smuggled.sh": b"rm -rf /"},
        )
        problems = verify_session_bundle(bad).problems
        assert f"missing: capsules/{RUN_1}/model-calls.jsonl" in problems
        assert "not in index: smuggled.sh" in problems
        assert any("manifest_hash" in p for p in problems)

    def test_swapped_session_manifest(
        self, exported: tuple[str, Path, Path], tmp_path: Path
    ) -> None:
        _sid, root, out = exported
        with zipfile.ZipFile(out) as zf:
            manifest = json.loads(zf.read("session.json"))
        manifest["member_runs"] = manifest["member_runs"][:1]
        bad = tmp_path / "bad.zip"
        _rezip(out, bad, {"session.json": json.dumps(manifest).encode()})
        problems = verify_session_bundle(bad).problems
        assert any("session.json digest" in p for p in problems)
        assert any(f"index lists {RUN_2}" in p for p in problems)

    def test_member_capsule_yaml_not_matching_ref(
        self, exported: tuple[str, Path, Path], tmp_path: Path
    ) -> None:
        _sid, _root, out = exported
        # Rewrite capsule.yaml AND its artifact digest + manifest_hash, so only
        # the capsule_ref cross-check can catch it.
        with zipfile.ZipFile(out) as zf:
            index = json.loads(zf.read("session-bundle.json"))
        forged = b"run_id: forged\n"
        rel = f"capsules/{RUN_1}/capsule.yaml"
        for art in index["artifacts"]:
            if art["path"] == rel:
                art["sha256"] = "sha256:" + hashlib.sha256(forged).hexdigest()
        index["members"][0]["sequence"] = 7
        del index["manifest_hash"]
        from novafabric.evidence.bundle import _canonical_manifest_hash

        index["manifest_hash"] = _canonical_manifest_hash(index)
        bad = tmp_path / "bad.zip"
        _rezip(out, bad, {rel: forged, "session-bundle.json": json.dumps(index).encode()})
        problems = verify_session_bundle(bad).problems
        assert any("does not match its capsule_ref" in p for p in problems)
        assert any("sequence mismatch" in p for p in problems)

    def test_member_not_carried(self, exported: tuple[str, Path, Path], tmp_path: Path) -> None:
        _sid, _root, out = exported
        with zipfile.ZipFile(out) as zf:
            index = json.loads(zf.read("session-bundle.json"))
        index["members"] = index["members"][:1]
        from novafabric.evidence.bundle import _canonical_manifest_hash

        del index["manifest_hash"]
        index["manifest_hash"] = _canonical_manifest_hash(index)
        bad = tmp_path / "bad.zip"
        _rezip(
            out,
            bad,
            {
                "session-bundle.json": json.dumps(index).encode(),
                f"capsules/{RUN_2}/capsule.yaml": None,
            },
        )
        problems = verify_session_bundle(bad).problems
        assert any(f"member {RUN_2} is not carried" in p for p in problems)

    def test_capsule_yaml_absent_for_indexed_member(
        self, exported: tuple[str, Path, Path], tmp_path: Path
    ) -> None:
        _sid, _root, out = exported
        bad = tmp_path / "bad.zip"
        _rezip(out, bad, {f"capsules/{RUN_1}/capsule.yaml": None})
        problems = verify_session_bundle(bad).problems
        assert any("capsule.yaml missing" in p for p in problems)

    @pytest.mark.parametrize(
        ("mutate", "expected"),
        [
            ({"session-bundle.json": None}, "not a NovaFabric session bundle"),
            ({"session-bundle.json": b"{not json"}, "not valid JSON"),
            ({"session-bundle.json": b'{"bundle_kind": "evidence"}'}, "not a session-bundle index"),
            ({"session.json": None}, "missing: session.json"),
            ({"session.json": b"{}"}, "session.json is invalid"),
        ],
    )
    def test_structural_problems(
        self,
        exported: tuple[str, Path, Path],
        tmp_path: Path,
        mutate: dict[str, bytes | None],
        expected: str,
    ) -> None:
        _sid, _root, out = exported
        bad = tmp_path / "bad.zip"
        _rezip(out, bad, mutate)
        report = verify_session_bundle(bad)
        assert not report.ok
        assert any(expected in p for p in report.problems), report.problems

    def test_index_session_id_mismatch(
        self, exported: tuple[str, Path, Path], tmp_path: Path
    ) -> None:
        _sid, _root, out = exported
        with zipfile.ZipFile(out) as zf:
            index = json.loads(zf.read("session-bundle.json"))
        index["session_id"] = "01AAAAAAAAAAAAAAAAAAAAAAAA"
        index["artifacts"].append({"path": "../escape", "sha256": "sha256:" + "0" * 64})
        bad = tmp_path / "bad.zip"
        _rezip(out, bad, {"session-bundle.json": json.dumps(index).encode()})
        problems = verify_session_bundle(bad).problems
        assert any("index session_id does not match" in p for p in problems)
        assert any("unsafe archive member name '../escape'" in p for p in problems)

    def test_not_a_zip(self, tmp_path: Path) -> None:
        junk = tmp_path / "junk.zip"
        junk.write_bytes(b"not a zip")
        report = verify_session_bundle(junk)
        assert not report.ok
        assert "not a readable ZIP" in report.problems[0]


class TestUnsafeArchives:
    @pytest.mark.parametrize(
        "name",
        [
            "../evil.txt",
            "capsules/../../evil",
            "/etc/evil",
            "C:/evil",
            "a\\..\\evil",
            "~/evil",
            "a//b",
            "a/./b",
        ],
    )
    def test_unsafe_names_rejected(self, name: str) -> None:
        with pytest.raises(UnsafeBundleMemberError):
            bundle_mod.safe_member_path(name)

    def test_safe_name_accepted(self) -> None:
        assert bundle_mod.safe_member_path("capsules/x/y.txt").parts == ("capsules", "x", "y.txt")

    def _write(self, path: Path, entries: list[tuple[zipfile.ZipInfo | str, bytes]]) -> Path:
        with zipfile.ZipFile(path, "w") as zf:
            for info, data in entries:
                zf.writestr(info, data)
        return path

    def test_traversal_member_refused_on_import(self, tmp_path: Path) -> None:
        bad = self._write(tmp_path / "evil.zip", [("../../outside.txt", b"x")])
        with pytest.raises(SessionBundleError, match="unsafe archive member"):
            import_session_bundle(bad, root=tmp_path / "sessions")
        assert not (tmp_path / "outside.txt").exists()
        assert [p.name for p in (tmp_path / "sessions").iterdir()] == []

    def test_unsafe_directory_entry_refused(self, tmp_path: Path) -> None:
        bad = self._write(tmp_path / "evil.zip", [("../dir/", b"")])
        assert not verify_session_bundle(bad).ok

    def test_symlink_entry_refused(self, tmp_path: Path) -> None:
        info = zipfile.ZipInfo("link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        bad = self._write(tmp_path / "evil.zip", [(info, b"/etc/passwd")])
        report = verify_session_bundle(bad)
        assert any("symlink" in p for p in report.problems)

    def test_duplicate_entry_refused(self, tmp_path: Path) -> None:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # zipfile warns on duplicate names
            bad = self._write(tmp_path / "evil.zip", [("a.txt", b"1"), ("a.txt", b"2")])
        assert any("appears twice" in p for p in verify_session_bundle(bad).problems)

    def test_entry_and_declared_byte_ceilings(self, tmp_path: Path) -> None:
        bad = self._write(tmp_path / "many.zip", [(f"f{i}", b"x") for i in range(5)])
        report = verify_session_bundle(bad, limits=BundleLimits(max_entries=3))
        assert any("entries" in p for p in report.problems)
        report = verify_session_bundle(bad, limits=BundleLimits(max_total_bytes=2))
        assert any("declares" in p for p in report.problems)

    def test_lying_header_bomb_is_stopped(self, tmp_path: Path) -> None:
        path = self._write(tmp_path / "bomb.zip", [("big.bin", b"\0" * 10_000)])
        # Forge the central-directory size down, so declared-size checks pass.
        with zipfile.ZipFile(path) as zf:
            info = zf.infolist()[0]
        raw = bytearray(path.read_bytes())
        cd = raw.rfind(b"PK\x01\x02")
        raw[cd + 24 : cd + 28] = (10).to_bytes(4, "little")
        path.write_bytes(bytes(raw))
        report = verify_session_bundle(path)
        assert not report.ok
        assert info.file_size == 10_000


class TestImport:
    def test_import_round_trip_resolves_members(
        self, exported: tuple[str, Path, Path], tmp_path: Path
    ) -> None:
        sid, _root, out = exported
        dest_root = tmp_path / "elsewhere" / "sessions"
        result = import_session_bundle(out, root=dest_root)
        assert result.session_id == sid
        assert result.members == 2
        session_dir = dest_root / sid
        assert (session_dir / "capsules" / RUN_1 / "inputs").is_dir()
        assert not (session_dir / "session-bundle.json").exists()
        assert not [p for p in dest_root.iterdir() if p.name.startswith(".import-")]
        # capsule_base deliberately absent: the bundled members resolve on their own
        resolved = resolve_members(load_session(sid, root=dest_root), root=dest_root)
        assert [r.status for r in resolved] == ["ok", "ok"]

    def test_import_never_overwrites(self, exported: tuple[str, Path, Path]) -> None:
        sid, root, out = exported
        before = (root / sid / "session.json").read_bytes()
        with pytest.raises(SessionBundleError, match="already exists"):
            import_session_bundle(out, root=root)
        assert (root / sid / "session.json").read_bytes() == before
        assert not [p for p in root.iterdir() if p.name.startswith(".import-")]

    def test_import_of_tampered_bundle_writes_nothing(
        self, exported: tuple[str, Path, Path], tmp_path: Path
    ) -> None:
        _sid, _root, out = exported
        bad = tmp_path / "bad.zip"
        _rezip(out, bad, {f"capsules/{RUN_1}/outputs/answer.txt": b"forged"})
        dest = tmp_path / "dest"
        with pytest.raises(SessionBundleError, match="modified"):
            import_session_bundle(bad, root=dest)
        assert list(dest.iterdir()) == []

    def test_import_refreshes_existing_index(
        self, exported: tuple[str, Path, Path], tmp_path: Path
    ) -> None:
        from novafabric.session import list_sessions_fast, rebuild_index

        sid, _root, out = exported
        dest = tmp_path / "dest"
        rebuild_index(root=dest)
        import_session_bundle(out, root=dest)
        listing = list_sessions_fast(root=dest)
        assert listing.source == "index"
        assert [m.session_id for m in listing.manifests] == [sid]


# ---------------------------------------------------------------------------
# Reviewer-verified defects (regressions)
# ---------------------------------------------------------------------------


def _reforge(src: Path, dst: Path, edit_manifest: Any) -> None:
    """Rewrite ``session.json`` via *edit_manifest* and re-seal the index.

    The result is a *self-consistent* bundle: every digest (session.json,
    artifacts[], manifest_hash) matches the edited bytes, so only semantic
    checks can reject it.
    """
    from novafabric.evidence.bundle import _canonical_manifest_hash

    with zipfile.ZipFile(src) as zf:
        manifest = json.loads(zf.read("session.json"))
        index = json.loads(zf.read("session-bundle.json"))
    edit_manifest(manifest)
    raw = json.dumps(manifest).encode()
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    index["session_id"] = manifest["session_id"]
    index["session_manifest"]["sha256"] = digest
    for art in index["artifacts"]:
        if art["path"] == "session.json":
            art["sha256"], art["size_bytes"] = digest, len(raw)
    index.pop("manifest_hash")
    index["manifest_hash"] = _canonical_manifest_hash(index)
    _rezip(
        src,
        dst,
        {"session.json": raw, "session-bundle.json": json.dumps(index).encode()},
    )


class TestSessionIdTraversal:
    @pytest.mark.parametrize("evil", ["../../PWNED", "/tmp/nova-PWNED-abs", "PWNED"])
    def test_non_ulid_session_id_fails_verification_and_import(
        self, exported: tuple[str, Path, Path], tmp_path: Path, evil: str
    ) -> None:
        _sid, _root, out = exported
        bad = tmp_path / "evil.zip"
        _reforge(out, bad, lambda m: m.update(session_id=evil))
        report = verify_session_bundle(bad)
        assert not report.ok
        assert any("is not a ULID" in p for p in report.problems), report.problems

        dest = tmp_path / "a" / "b" / "sessions"
        with pytest.raises(SessionBundleError, match="not a ULID"):
            import_session_bundle(bad, root=dest)
        assert not (tmp_path / "a" / "PWNED").exists()
        assert not Path("/tmp/nova-PWNED-abs").exists()
        assert [p.name for p in dest.iterdir()] == []

    def test_import_refuses_destination_outside_root_even_if_verified(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_verify(bundle_path, payload, limits):  # type: ignore[no-untyped-def]
            (payload / "session-bundle.json").write_text("{}")
            return bundle_mod.SessionBundleVerification(
                bundle=str(bundle_path), ok=True, session_id="../PWNED"
            )

        monkeypatch.setattr(bundle_mod, "_verify_into", fake_verify)
        root = tmp_path / "sessions"
        with pytest.raises(SessionBundleError, match="outside"):
            import_session_bundle(tmp_path / "any.zip", root=root)
        assert not (tmp_path / "PWNED").exists()
        assert list(root.iterdir()) == []


class TestVerifyNeverRaises:
    def test_malformed_capsule_ref_is_a_problem_not_an_exception(
        self, exported: tuple[str, Path, Path], tmp_path: Path
    ) -> None:
        _sid, _root, out = exported
        bad = tmp_path / "bad.zip"

        def break_ref(m: dict[str, Any]) -> None:
            m["member_runs"][0]["capsule_ref"] = "not-a-ref"

        _reforge(out, bad, break_ref)
        report = verify_session_bundle(bad)
        assert not report.ok
        assert any("malformed capsule_ref" in p for p in report.problems), report.problems


class TestImportCleanup:
    def test_failed_rename_releases_the_claimed_dir(
        self,
        exported: tuple[str, Path, Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sid, _root, out = exported
        dest = tmp_path / "dest"

        def boom(src: object, dst: object) -> None:
            raise OSError("simulated rename failure")

        monkeypatch.setattr(bundle_mod.os, "replace", boom)
        with pytest.raises(OSError, match="simulated"):
            import_session_bundle(out, root=dest)
        assert not (dest / sid).exists()
        assert list(dest.iterdir()) == []
        monkeypatch.undo()
        assert import_session_bundle(out, root=dest).session_id == sid


class TestZipBomb:
    def test_high_ratio_entry_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "bomb.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("zeros.bin", b"\0" * (2 * 1024**2))
        report = verify_session_bundle(path)
        assert not report.ok
        assert any("compression-ratio ceiling" in p for p in report.problems), report.problems
        # the ceiling is configurable
        relaxed = BundleLimits(max_compression_ratio=1e9)
        report = verify_session_bundle(path, limits=relaxed)
        assert not any("compression-ratio" in p for p in report.problems)

    def test_small_high_ratio_entries_are_allowed(self, tmp_path: Path) -> None:
        path = tmp_path / "small.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("zeros.bin", b"\0" * 100_000)
        report = verify_session_bundle(path)
        assert not any("compression-ratio" in p for p in report.problems)

    def test_extraction_counts_actual_bytes_not_declared(self, tmp_path: Path) -> None:
        import io

        info = zipfile.ZipInfo("a.bin")
        info.file_size = 4  # declared

        class LyingZip:
            def open(self, _info: zipfile.ZipInfo) -> io.BytesIO:
                return io.BytesIO(b"x" * 1000)  # a reader that ignores the header

        with pytest.raises(SessionBundleError, match="expands past"):
            bundle_mod._extract(
                LyingZip(),  # type: ignore[arg-type]
                [info],
                [],
                tmp_path,
                BundleLimits(),
            )


class TestExportOpenTimeSymlink:
    def test_file_swapped_for_symlink_after_planning_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sid, root, caps = build_session(tmp_path)
        secret = tmp_path / "secret.txt"
        secret.write_text("top secret\n")
        real = bundle_mod._plan_entries

        def racing(*args: Any, **kwargs: Any) -> Any:
            planned = real(*args, **kwargs)
            victim = caps / RUN_1 / "outputs" / "answer.txt"
            victim.unlink()
            victim.symlink_to(secret)
            return planned

        monkeypatch.setattr(bundle_mod, "_plan_entries", racing)
        out = tmp_path / "x.zip"
        with pytest.raises(SessionBundleError, match="symlink"):
            export_session_bundle(sid, out, root=root, capsule_base=caps)
        assert not out.exists()

    def test_non_regular_or_vanished_file_is_refused_at_open(self, tmp_path: Path) -> None:
        fifo = tmp_path / "fifo"
        os.mkfifo(fifo)
        with pytest.raises(SessionBundleError, match="not a regular file"):
            bundle_mod._open_regular_nofollow(fifo)  # must not block
        with pytest.raises(SessionBundleError, match="cannot be read"):
            bundle_mod._open_regular_nofollow(tmp_path / "gone")
