"""``nova serve`` sends browser security headers on every response (review frontend-dashboard-01).

Defence in depth behind the ReportChart fix: no plugins or ``<base>`` hijack, no
framing (click-jacking), no MIME sniffing, and **no Referer** — the dashboard token
travels in ``?token=``, so any outbound link would otherwise hand it to the linked
site.

Issue #278 adds ``script-src``: ``'self'`` plus the SHA-256 hash of each inline
script in the HTML being served, never ``'unsafe-inline'``. The Swagger UI page
(``/api/docs``) loads its script from a CDN, so it keeps the base policy only.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve import csp  # noqa: E402
from novafabric.serve.app import SECURITY_HEADERS, create_app  # noqa: E402

TOKEN = "test-token-security-headers-0123"
HOST = {"host": "127.0.0.1:4321"}
CSP = "Content-Security-Policy"
SHIPPED_STATIC = Path(csp.__file__).parent / "static"

INLINE = "document.documentElement.dataset.ok = '1';"
PAGE = f"<!doctype html><html><head><script>{INLINE}</script></head><body></body></html>"


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    capsules = tmp_path / "runs"
    capsules.mkdir()
    return TestClient(create_app(token=TOKEN, capsule_dir=capsules, static_dir=None))


def _site(tmp_path: Path) -> Path:
    site = tmp_path / "site"
    (site / "dashboard").mkdir(parents=True)
    (site / "dashboard" / "index.html").write_text(PAGE, encoding="utf-8")
    return site


def _directives(policy: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for part in policy.split(";"):
        words = part.split()
        if words:
            out[words[0]] = words[1:]
    return out


def _assert_headers(headers: object) -> None:
    for name, value in SECURITY_HEADERS.items():
        if name == CSP:
            continue
        assert headers[name] == value, name  # type: ignore[index]
    policy = headers[CSP]  # type: ignore[index]
    for directive in csp.BASE_DIRECTIVES:
        assert directive in policy, directive
    assert "script-src 'self'" in policy
    assert "'unsafe-inline'" not in policy


def test_api_responses_carry_the_security_headers(client: TestClient) -> None:
    res = client.get(f"/api/runs?token={TOKEN}", headers=HOST)
    assert res.status_code == 200
    _assert_headers(res.headers)


def test_refusals_carry_them_too(client: TestClient) -> None:
    _assert_headers(client.get("/api/runs", headers=HOST).headers)  # 401
    _assert_headers(client.get("/api/runs", headers={"host": "evil.example"}).headers)  # host guard


def test_the_policy_closes_framing_plugins_base_and_referrer() -> None:
    base = SECURITY_HEADERS[CSP]
    for directive in ("object-src 'none'", "base-uri 'none'", "frame-ancestors 'none'"):
        assert directive in base
    assert SECURITY_HEADERS["Referrer-Policy"] == "no-referrer"
    assert SECURITY_HEADERS["X-Content-Type-Options"] == "nosniff"
    assert SECURITY_HEADERS["X-Frame-Options"] == "DENY"


def test_swagger_ui_keeps_the_base_policy(client: TestClient) -> None:
    # Swagger UI's CDN script and inline bootstrap would be blocked by script-src.
    res = client.get(f"/api/docs?token={TOKEN}", headers=HOST)
    assert res.headers[CSP] == SECURITY_HEADERS[CSP]
    assert "script-src" not in res.headers[CSP]
    assert res.headers["Referrer-Policy"] == "no-referrer"


def test_an_inline_script_is_allowed_by_its_hash_not_by_unsafe_inline(tmp_path: Path) -> None:
    capsules = tmp_path / "runs"
    capsules.mkdir()
    app = create_app(token=TOKEN, capsule_dir=capsules, static_dir=_site(tmp_path))
    res = TestClient(app).get("/dashboard/", headers=HOST)
    assert res.status_code == 200
    scripts = _directives(res.headers[CSP])["script-src"]
    assert f"'{csp.script_hash(INLINE)}'" in scripts
    assert "'unsafe-inline'" not in scripts
    assert "'self'" in scripts
    _assert_headers(res.headers)


def test_a_caller_mounted_site_is_hashed_through_csp_static_dir(tmp_path: Path) -> None:
    # `nova serve` mounts the site after create_app returns; the hashes must still be there.
    capsules = tmp_path / "runs"
    capsules.mkdir()
    site = _site(tmp_path)
    app = create_app(
        token=TOKEN,
        capsule_dir=capsules,
        static_dir=None,
        static_mounted_by_caller=True,
        csp_static_dir=site,
    )
    res = TestClient(app).get(f"/api/runs?token={TOKEN}", headers=HOST)
    assert f"'{csp.script_hash(INLINE)}'" in res.headers[CSP]


# ---------- the hashing itself ----------


def test_script_hash_matches_the_browser_definition() -> None:
    # sha256 over the UTF-8 text, base64; CRLF counts as LF (HTML parser normalisation).
    assert csp.script_hash("alert(1)") == "sha256-bhHHL3z2vDgxUt0W3dWQOrprscmda2Y5pLsLg4GF+pI="
    assert csp.script_hash("a;\r\nb;") == csp.script_hash("a;\nb;")


def test_scripts_with_src_and_empty_scripts_are_not_hashed() -> None:
    html = (
        '<script type="module" src="/_astro/x.js"></script>'
        "<script></script>"
        "<SCRIPT>one()</SCRIPT >"
        '<script type="module">two()</script>'
    )
    assert csp.inline_script_hashes_in_html(html) == {
        csp.script_hash("one()"),
        csp.script_hash("two()"),
    }


def test_no_static_dir_gives_self_only(tmp_path: Path) -> None:
    for missing in (None, tmp_path / "absent"):
        policy = csp.dashboard_content_security_policy(missing)
        assert "script-src 'self' 'unsafe-eval'; " in policy
        assert "sha256-" not in policy


def test_oversized_html_is_skipped_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = _site(tmp_path)
    monkeypatch.setattr(csp, "MAX_HTML_BYTES", 10)
    assert csp.inline_script_hashes(site) == ()


def test_every_inline_script_in_the_shipped_bundle_is_allowed() -> None:
    # The wheel's own dashboard: a policy that missed one of its inline scripts would
    # blank the page in the browser while every API test stays green.
    if not SHIPPED_STATIC.is_dir():
        pytest.skip("dashboard bundle not built in this checkout")
    policy = csp.dashboard_content_security_policy(SHIPPED_STATIC)
    allowed = set(_directives(policy)["script-src"])
    pages = sorted(SHIPPED_STATIC.rglob("*.html"))
    assert pages, "the shipped bundle has no HTML"
    for page in pages:
        html = page.read_text(encoding="utf-8")
        for match in re.finditer(r"<script\b([^>]*)>(.*?)</script\s*>", html, re.I | re.S):
            attrs, body = match.group(1), match.group(2)
            if "src=" in attrs or not body:
                continue
            assert f"'{csp.script_hash(body)}'" in allowed, page
    assert "'unsafe-inline'" not in allowed


def test_nova_serve_sends_the_hashes_of_the_site_it_mounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("uvicorn")
    from typer.testing import CliRunner  # noqa: PLC0415

    from novafabric.cli.main import app as nova_app  # noqa: PLC0415

    if not SHIPPED_STATIC.is_dir():
        pytest.skip("dashboard bundle not built in this checkout")
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("NOVAFABRIC_CAPSULE_DIR", raising=False)
    caps = tmp_path / "runs"
    caps.mkdir()
    with patch("uvicorn.run") as run:
        result = CliRunner().invoke(
            nova_app,
            ["serve", "--experimental", "--capsule-dir", str(caps), "--no-browser"],
        )
    assert result.exit_code == 0, result.output
    served = run.call_args.args[0]
    res = TestClient(served).get("/dashboard/", headers=HOST)
    assert res.status_code == 200
    assert res.headers[CSP] == csp.dashboard_content_security_policy(SHIPPED_STATIC)
    assert "sha256-" in res.headers[CSP]
