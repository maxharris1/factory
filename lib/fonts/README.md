# Dashboard fonts (vendored)

The dashboard pages (lib/dashboard_page.py) use these two variable fonts,
in the Omarchy (omarchy.org) style: Geist for UI text, JetBrains Mono for
numbers, tickets and labels.

| File | Family | Source | License |
|---|---|---|---|
| `geist-latin-wght-normal.woff2` | Geist Variable, wght 100-900 | npm `@fontsource-variable/geist@5.3.0`, `files/` | SIL OFL 1.1, `OFL-Geist.txt` |
| `jetbrains-mono-latin-wght-normal.woff2` | JetBrains Mono Variable, wght 100-800 | npm `@fontsource-variable/jetbrains-mono@5.3.0`, `files/` | SIL OFL 1.1, `OFL-JetBrainsMono.txt` |

The files are the upstream Fontsource builds, unmodified (Latin subset,
normal style only). Both licenses are the package's own `LICENSE` file,
copied verbatim. The OFL allows the fonts to be bundled and redistributed
with software, as long as the copyright notice and license go with them
and the fonts are not sold on their own. Neither font declares a Reserved
Font Name.

Checksums (sha256):

```
19f9c92546aa300c312235e3125af1b81394d8db9a4bc4a425cd5b641d2d54e1  geist-latin-wght-normal.woff2
18be452724bfdc236c074ca94a249a7f41a86752c7d04ab258ce9ed5651f6a7e  jetbrains-mono-latin-wght-normal.woff2
```

Package tarball integrity (as published on the npm registry and checked on
download):

- geist-5.3.0.tgz `sha512-j0m+vLQuG5XAYoHtGCVu0spvlGreR3EzpECUVzkFmI1mTVnAO38l/NEPDCFgZ177JxzYJCLSmTQibIiYPilGrA==`
- jetbrains-mono-5.3.0.tgz `sha512-F32xpS2NsGYoQi2ADSkKTgpJj7ozajsGgDJ8woTnqjmIB+dxDIqImjl4pXZVEExu8UFZ2ndhmX18EBS/hdz3Lw==`

## How the page loads them

Nothing is fetched. The static server's CSP is `default-src 'none'` with no
`font-src`, and every page must stay a single self-contained file (the push
sends the same five files as before). So the page script carries each font
as base64 and registers it with the `FontFace` API from those bytes, which
makes no request. Without script, or if a font is missing here, the pages
fall back to the system UI and monospace fonts.
