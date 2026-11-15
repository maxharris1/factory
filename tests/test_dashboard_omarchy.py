"""Dashboard look (Omarchy style): the Tokyo Night palette, the vendored
Geist and JetBrains Mono fonts with their licenses, and the script that
registers those fonts from bytes without fetching anything.
"""

import base64
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import lib.dashboard_page as page
from lib.dashboard import collect_snapshot, render_pages
from tests.test_dashboard import NOW, seeded

ROOT = Path(__file__).resolve().parents[1]
FONT_DIR = ROOT / "lib" / "fonts"


def _pages(tmp_path: Path) -> dict[str, str]:
    return render_pages(collect_snapshot(*seeded(tmp_path), now=NOW))


def _snapshot(tmp_path: Path) -> dict:
    return collect_snapshot(*seeded(tmp_path), now=NOW)


def _css(html: str) -> str:
    (css,) = re.findall(r"<style>(.*?)</style>", html, flags=re.S)
    return css


def _script(html: str) -> str:
    (script,) = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    return script


def _faces(script: str) -> list[dict]:
    match = re.search(r"var FONTS = (\[.*?\]);", script, flags=re.S)
    assert match, "no FONTS in the page script"
    return json.loads(match.group(1))


# --- vendored fonts --------------------------------------------------------------------


def test_fonts_are_vendored_with_their_licenses() -> None:
    readme = (FONT_DIR / "README.md").read_text(encoding="utf-8")
    for family, name, _ in page.FONTS:
        data = (FONT_DIR / name).read_bytes()
        assert data[:4] == b"wOF2", name
        # The README records the checksum of the exact file in the repo.
        assert f"{hashlib.sha256(data).hexdigest()}  {name}" in readme, name
    for license_name, owner in (
        ("OFL-Geist.txt", "The Geist Project Authors"),
        ("OFL-JetBrainsMono.txt", "The JetBrains Mono Project Authors"),
    ):
        text = (FONT_DIR / license_name).read_text(encoding="utf-8")
        assert "SIL OPEN FONT LICENSE Version 1.1" in text, license_name
        assert owner in text, license_name


def test_every_page_carries_both_fonts_as_bytes(tmp_path: Path) -> None:
    files = {family: (FONT_DIR / name).read_bytes() for family, name, _ in page.FONTS}
    for name, html in _pages(tmp_path).items():
        faces = _faces(_script(html))
        assert [face["family"] for face in faces] == list(files), name
        for face in faces:
            assert base64.b64decode(face["data"]) == files[face["family"]], name
        # Registered from bytes, never from a URL.
        assert "new FontFace(font.family, bytes," in _script(html), name


def test_css_uses_the_omarchy_fonts_with_system_fallbacks(tmp_path: Path) -> None:
    css = _css(_pages(tmp_path)["index.html"])
    sans = re.search(r"--sans:([^;}]+)", css).group(1)
    mono = re.search(r"--mono:([^;}]+)", css).group(1)
    assert sans.startswith('"Geist Variable",') and "sans-serif" in sans
    assert mono.startswith('"JetBrains Mono Variable",') and "monospace" in mono
    assert "font:14px/1.5 var(--sans)" in css


def test_a_missing_font_is_left_out_and_the_pages_still_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    family, name, _ = page.FONTS[1]
    shutil.copyfile(FONT_DIR / name, fonts / name)
    (fonts / page.FONTS[0][1]).write_bytes(b"not a font")
    monkeypatch.setattr(page, "FONT_DIR", fonts)
    page.font_faces.cache_clear()
    snapshot = _snapshot(tmp_path)
    try:
        pages = render_pages(snapshot)
        assert [face["family"] for face in _faces(_script(pages["job.html"]))] == [family]
        (fonts / name).unlink()
        page.font_faces.cache_clear()
        pages = render_pages(snapshot)
        assert _faces(_script(pages["index.html"])) == []
        assert 'id="lanes"' in pages["index.html"]
    finally:
        page.font_faces.cache_clear()


# --- palette ---------------------------------------------------------------------------


def test_palette_is_tokyo_night(tmp_path: Path) -> None:
    css = _css(_pages(tmp_path)["index.html"])
    palette = dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6,8})", css))
    expected = {
        "bg": "#1a1b26", "dim": "#13141c", "panel": "#1f2230", "panel-hi": "#24283b",
        "line": "#24283b", "line-hi": "#414868", "field": "#0e0e14", "sel": "#292e42",
        "text": "#c0caf5", "text-2": "#a9b1d6", "muted": "#8b93b8",
        "brand": "#9ece6a", "brand-soft": "#9ece6a1f", "ink": "#0c0e10",
        # Status: ok = brand green, warn = orange, info = cyan, error = red.
        "ok": "#9ece6a", "warn": "#ff9e64", "accent": "#7dcfff", "bad": "#f7768e",
    }
    for name, value in expected.items():
        assert palette.get(name) == value, name


def test_corners_are_square_and_elevation_is_a_ring(tmp_path: Path) -> None:
    html = _pages(tmp_path)["index.html"]
    css = _css(html)
    assert "--elev:0 0 0 1px rgba(255,255,255,.07)" in css
    assert "--elev-hi:0 0 0 1px rgba(255,255,255,.13)" in css
    panel = re.search(r"\.panel\{[^}]*\}", css).group(0)
    assert "box-shadow:var(--elev)" in panel and "radius" not in panel
    # #144: the waffle replaced the round savings gauge, so every corner is square.
    assert "border-radius" not in css
    # #139: the brand caret is a PixelStrip canvas whose rate comes from the
    # link state; the engine stills it under reduced motion.
    assert "@keyframes caret" not in css
    assert re.search(r'<span class="caret" id="caret" aria-hidden="true"><canvas class="px" data-px="caret"', html)


# --- the font loader, in node ---------------------------------------------------------

LOADER_HARNESS = r"""
const fs = require('fs');
const script = fs.readFileSync(process.argv[2], 'utf8');
const added = [];
global.window = global;
global.FontFace = function (family, bytes, desc) {
  this.family = family; this.size = bytes.byteLength; this.desc = desc;
  this.load = () => Promise.resolve(this); };
global.document = {fonts: {add: (face) => { added.push(face); }}, body: {getAttribute: () => null},
  hidden: true, getElementById: () => null, createElement: () => ({}), addEventListener: () => {}};
global.addEventListener = () => {};
global.location = {hash: '', reload: () => {}};
global.fetch = () => new Promise(() => {});
global.setInterval = () => 0;
eval(script);
process.stdout.write(JSON.stringify(added.map(f => [f.family, f.size, f.desc.weight, f.desc.display])));
process.exit(0);
"""

NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_script_registers_each_font_from_its_bytes(tmp_path: Path) -> None:
    script = _script(_pages(tmp_path)["index.html"])
    (tmp_path / "page.js").write_text(script, encoding="utf-8")
    (tmp_path / "harness.js").write_text(LOADER_HARNESS, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(tmp_path / "harness.js"), str(tmp_path / "page.js")],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    expected = [
        [family, (FONT_DIR / name).stat().st_size, weight, "swap"]
        for family, name, weight in page.FONTS
    ]
    assert json.loads(result.stdout) == expected
