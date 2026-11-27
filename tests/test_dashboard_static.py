"""Static JS/CSS extraction and strict script-src 'self' CSP tests (E4-3)."""

from __future__ import annotations

import ast
import http.client
import importlib.util
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib.dashboard import STATIC_ASSETS, render_pages, render_static_assets, write_snapshot_pages
from lib.dashboard_flow import load_chart
from lib.dashboard_page import PAGES, app_css, page_script
from lib.dashboard_static import STATIC_DIR, auth_script, read_asset
from tests.test_dashboard import snapshot_for

ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = ROOT / "deploy" / "factory-dashboard-http.py"


def _load_server():
    spec = importlib.util.spec_from_file_location("factory_dashboard_http_static", SERVER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _serve(site: Path, actions=None):
    mod = _load_server()
    server = mod.make_server("127.0.0.1", 0, site, actions)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    def request(path: str, method: str = "GET", headers: dict[str, str] | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request(method, path, headers=headers or {})
            res = conn.getresponse()
            body = res.read()
            hdrs = {k.lower(): v for k, v in res.getheaders()}
            return res.status, hdrs, body
        finally:
            conn.close()

    return mod, server, thread, request


def test_rendered_pages_have_zero_inline_scripts_or_styles(tmp_path: Path) -> None:
    snap = snapshot_for(tmp_path)
    pages = render_pages(snap)
    assert set(pages) == set(PAGES)
    for name, html in pages.items():
        assert '<link rel="stylesheet" href="/app.css">' in html, name
        assert '<script src="/app.js" defer></script>' in html, name
        # Zero inline <script> blocks (only external src="/app.js")
        inline_scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", html, flags=re.I)
        assert inline_scripts == [], (name, inline_scripts)
        assert "<style" not in html.lower(), name


def test_auth_required_page_has_no_inline_script_and_auth_js_matches_source() -> None:
    mod = _load_server()
    html = mod._AUTH_REQUIRED_HTML.decode("utf-8")
    assert "<script src='/auth.js' defer></script>" in html
    assert re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", html, flags=re.I) == []
    assert mod._AUTH_JS.decode("utf-8") == auth_script()


def test_simple_http_server_serves_every_published_page_asset(tmp_path: Path) -> None:
    """Retained python3 -m http.server fallback must serve every referenced asset with 200."""
    from http.server import SimpleHTTPRequestHandler

    site = tmp_path / "site"
    write_snapshot_pages(site, snapshot_for(tmp_path))
    for page_name in PAGES:
        html = (site / page_name).read_text(encoding="utf-8")
        urls = re.findall(r'(?:href|src)="(/[^"]+\.(?:css|js))"', html)
        assert urls == ["/app.css", "/app.js"], (page_name, urls)
        for url in urls:
            handler = object.__new__(SimpleHTTPRequestHandler)
            handler.directory = str(site)
            handler.command = "GET"
            handler.request_version = "HTTP/1.1"
            handler.headers = {}
            codes: list[int] = []
            handler.send_response = lambda code, *args, _c=codes: _c.append(code)
            handler.send_error = lambda code, *args, _c=codes: _c.append(code)
            handler.send_header = lambda *args: None
            handler.end_headers = lambda: None
            handler.path = url
            resp = handler.send_head()
            if resp is not None:
                resp.close()
            assert codes == [200], (page_name, url, codes)


def test_strict_csp_drops_unsafe_inline_from_script_src() -> None:
    mod = _load_server()
    csp = mod.SECURITY_HEADERS["Content-Security-Policy"]
    directives = {
        part.strip().split()[0]: part.strip().split()[1:]
        for part in csp.split(";")
        if part.strip()
    }
    assert directives["default-src"] == ["'none'"]
    assert directives["script-src"] == ["'self'"]
    assert "'unsafe-inline'" not in directives["script-src"]
    assert "'self'" in directives["style-src"]
    # QA headers inherit the strict script-src 'self' policy too
    qa_csp = mod.QA_HEADERS["Content-Security-Policy"]
    assert "script-src 'self';" in qa_csp
    assert "script-src 'unsafe-inline'" not in qa_csp


def test_static_routes_serve_assets_and_block_traversals_and_links(tmp_path: Path) -> None:
    outside = tmp_path / "secret.txt"
    outside.write_text("TOP_SECRET_CANARY", encoding="utf-8")
    site = tmp_path / "site"
    snap = snapshot_for(tmp_path)
    write_snapshot_pages(site, snap)

    mod, server, thread, req = _serve(site)
    try:
        expected_types = {
            "app.css": "text/css; charset=utf-8",
            "app.js": "application/javascript; charset=utf-8",
            "auth.js": "application/javascript; charset=utf-8",
        }
        assets = render_static_assets(snap)
        for name, ctype in expected_types.items():
            for route in (f"/{name}", f"/static/{name}"):
                code, hdrs, body = req(route)
                assert code == 200, route
                assert hdrs["content-type"] == ctype
                assert hdrs["x-content-type-options"] == "nosniff"
                assert hdrs["cache-control"] == "no-store"
                assert "script-src 'self';" in hdrs["content-security-policy"]
                assert body.decode("utf-8") == assets[name]
                hcode, hhdrs, hbody = req(route, method="HEAD")
                assert (hcode, hbody, hhdrs["content-length"]) == (200, b"", str(len(body)))

        # Non-allowlisted paths, dot-temp files, traversals must 404
        for bad in (
            "/static/",
            "/static/index.html",
            "/static/status.json",
            "/static/tickets.json",
            "/static/.app.js.push-tmp",
            "/.app.js.push-tmp",
            "/static/../secret.txt",
            "/static/%2e%2e/secret.txt",
            "/static/app.js/extra",
            "/app.js/extra",
            "/static/unknown.js",
            "/unknown.js",
        ):
            code, _, body = req(bad)
            assert code == 404, bad
            assert b"TOP_SECRET_CANARY" not in body

        # Symlinks must be refused for app.js AND auth.js (no fallback masking a symlink!)
        for name in ("app.js", "auth.js"):
            (site / name).unlink()
            (site / name).symlink_to(outside)
            for route in (f"/{name}", f"/static/{name}"):
                code, _, body = req(route)
                assert code == 404, f"symlink for {route} must 404"
                assert b"TOP_SECRET_CANARY" not in body

        # Hardlink must be refused
        (site / "app.css").unlink()
        os.link(outside, site / "app.css")
        for route in ("/app.css", "/static/app.css"):
            code, _, body = req(route)
            assert code == 404
            assert b"TOP_SECRET_CANARY" not in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_require_auth_reads_allows_auth_js_only_before_signin(tmp_path: Path) -> None:
    mod = _load_server()
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<!doctype html>secret snapshot", encoding="utf-8")
    (site / "status.json").write_text("{}\n", encoding="utf-8")
    (site / "app.css").write_text("body{}\n", encoding="utf-8")
    (site / "app.js").write_text("console.log(1);\n", encoding="utf-8")
    # Note: auth.js intentionally absent on disk -> should fall back cleanly to _AUTH_JS

    handler = object.__new__(mod.SnapshotHandler)
    cfg = mod.ActionsConfig("synthetic", "human", frozenset(), require_auth_reads=True)
    handler.server = SimpleNamespace(
        actions=SimpleNamespace(config=cfg, current_with_renewal=lambda *_: (None, False)),
        root_fd=mod.open_root(site),
    )
    handler.client_address = ("127.0.0.1", 0)
    handler._action_client = lambda: "127.0.0.1"
    handler._cookie = lambda _: None
    out: list[tuple[int, str, bytes]] = []
    handler._send = lambda code, data, ctype, **kw: out.append((code, ctype, data))
    handler._json = lambda code, data: out.append((code, "application/json", json.dumps(data).encode()))
    try:
        for route in ("/", "/app.css", "/app.js", "/static/app.css", "/static/app.js", "/auth.js", "/static/auth.js"):
            handler.path = route
            handler._serve(body=True)
        assert [c for c, _, _ in out] == [401, 401, 401, 401, 401, 200, 200]
        assert b"/auth.js" in out[0][2]
        assert out[5][1] == "application/javascript; charset=utf-8"
        assert out[5][2] == mod._AUTH_JS
        assert out[6][2] == mod._AUTH_JS
    finally:
        os.close(handler.server.root_fd)


def test_read_asset_blocks_path_traversal() -> None:
    with pytest.raises(ValueError, match="escapes static root"):
        read_asset("../dashboard_page.py")


def test_python_dashboard_modules_contain_no_large_inline_css_or_js_literals() -> None:
    py_files = [
        *sorted((ROOT / "lib" / "dashboard_pages").glob("*.py")),
        ROOT / "lib" / "dashboard_telemetry.py",
        ROOT / "lib" / "dashboard_flow.py",
    ]
    offenders: list[tuple[str, str, int]] = []
    for fp in py_files:
        tree = ast.parse(fp.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and any(k in target.id for k in ("CSS", "SCRIPT")):
                        span = (node.end_lineno or node.lineno) - node.lineno + 1
                        if span > 15:
                            offenders.append((str(fp.relative_to(ROOT)), target.id, span))
    assert offenders == [], f"large inline CSS/JS literals remain in Python: {offenders}"


def test_assembled_js_and_standalone_scripts_pass_node_check(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    assembled = tmp_path / "app.js"
    assembled.write_text(page_script(load_chart()), encoding="utf-8")
    for js_file in (
        assembled,
        STATIC_DIR / "js" / "auth.js",
        STATIC_DIR / "js" / "pixelstrip.js",
    ):
        proc = subprocess.run([node, "--check", str(js_file)], capture_output=True, text=True, check=False)
        assert proc.returncode == 0, f"node --check failed on {js_file.name}: {proc.stderr}"


def test_app_css_and_static_assets_list_match_server_routes() -> None:
    mod = _load_server()
    assert tuple(STATIC_ASSETS) == tuple(mod.STATIC_ASSETS)
    assert app_css()
    for name in STATIC_ASSETS:
        assert mod.STATIC_ROUTES[f"/{name}"] == name
        assert mod.STATIC_ROUTES[f"/static/{name}"] == name
