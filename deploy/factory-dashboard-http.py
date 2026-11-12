#!/usr/bin/env python3
"""Read-only static server for the pushed factory dashboard (pc-wsl-bots).

An example for the pc-wsl-bots WSL node, run by
deploy/factory-dashboard-http.service. Nothing in this repository installs
or starts it. Standard library only, so it runs from a single copied file.

It replaces `python3 -m http.server`, which serves any path under its
directory, follows symlinks out of it, lists directories, and serves the
working directory (for a user unit, the home directory) when --directory is
empty. This server:

- binds only TAILNET_IP, which must be an IPv4 address in Tailscale's
  100.64.0.0/10 range (never a wildcard, loopback or LAN address);
- serves DASHBOARD_DIR, which must be set, absolute, an existing directory,
  and neither / nor $HOME nor any directory that contains $HOME (it is
  resolved through symlinks before those checks);
- answers GET and HEAD only, and only for /, /status.json and the four
  pages (/index.html, /activity.html, /efficiency.html, /job.html), the
  files `orch dashboard push` writes. Every other path, including
  dot-files such as the push's temp files and anything with `..` or an
  encoded slash, is 404, and no directory is ever listed;
- opens the directory once at startup (O_NOFOLLOW) and keeps that
  descriptor, so swapping the directory or an ancestor for a symlink later
  changes nothing;
- opens each file relative to that descriptor with O_NOFOLLOW and serves it
  only if it is a regular file with one link, so neither a symlink nor a
  hard link to a file elsewhere is served.

Settings come from the environment (the unit's EnvironmentFile):
TAILNET_IP, DASHBOARD_DIR, and DASHBOARD_PORT (default 8787, 1024-65535).
`--check` validates them, prints one line and exits.
"""

from __future__ import annotations

import ipaddress
import os
import re
import stat
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

TAILNET = ipaddress.ip_network("100.64.0.0/10")
DEFAULT_PORT = 8787
MAX_BYTES = 8 * 1024 * 1024
PAGES = ("index.html", "activity.html", "efficiency.html", "job.html")
ROUTES = {
    "/": "index.html",
    "/status.json": "status.json",
    **{f"/{name}": name for name in PAGES},
}
CONTENT_TYPES = {
    "status.json": "application/json",
    **{name: "text/html; charset=utf-8" for name in PAGES},
}
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    # The snapshot pages are self-contained: inline style and script only.
    # Their script fetches this server's own status.json (connect-src 'self') to
    # update in place; nothing else is ever loaded.
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
}


class ConfigError(ValueError):
    pass


def check_address(value: str | None) -> str:
    text = value or ""
    try:
        ok = (
            re.fullmatch(r"[0-9.]{7,15}", text) is not None
            and ipaddress.IPv4Address(text) in TAILNET
        )
    except ValueError:
        ok = False
    if not ok:
        raise ConfigError(
            "TAILNET_IP must be this node's tailnet IPv4 address (100.64.0.0/10)"
        )
    return text


def check_root(value: str | None, home: str | None) -> Path:
    if not value:
        raise ConfigError("DASHBOARD_DIR must be set to the pushed snapshot directory")
    if not os.path.isabs(value):
        raise ConfigError("DASHBOARD_DIR must be an absolute path")
    try:
        root = Path(value).resolve(strict=True)
    except (OSError, RuntimeError):
        raise ConfigError(f"DASHBOARD_DIR does not exist: {value}") from None
    if not root.is_dir():
        raise ConfigError(f"DASHBOARD_DIR is not a directory: {value}")
    if root == Path(root.anchor):
        raise ConfigError("DASHBOARD_DIR must not be /")
    if home:
        home_path = Path(home).resolve()
        if root == home_path or root in home_path.parents:
            raise ConfigError("DASHBOARD_DIR must not be $HOME or a directory above it")
    return root


def check_port(value: str | None) -> int:
    if value is None:
        return DEFAULT_PORT
    if re.fullmatch(r"[0-9]{1,5}", value) is None or not 1024 <= int(value) <= 65535:
        raise ConfigError("DASHBOARD_PORT must be a number from 1024 to 65535")
    return int(value)


def load_config(environ: dict[str, str]) -> tuple[str, int, Path]:
    return (
        check_address(environ.get("TAILNET_IP")),
        check_port(environ.get("DASHBOARD_PORT")),
        check_root(environ.get("DASHBOARD_DIR"), environ.get("HOME")),
    )


NOFOLLOW = os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def open_root(root: Path) -> int:
    """Open the served directory once, without following a symlink at its
    last component, and return the descriptor. Files are then opened
    relative to it, so replacing the directory or any ancestor of it with a
    symlink after startup cannot change what is served."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW)
    try:
        if not os.path.samestat(os.fstat(fd), os.stat(root, follow_symlinks=False)):
            raise OSError(f"{root} changed while it was opened")
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_snapshot_file(root_fd: int, name: str) -> bytes | None:
    """The file's bytes if `name` is a regular, singly linked file directly in
    the pinned root directory, reached without following any symlink;
    otherwise None."""
    try:
        # O_NONBLOCK so a FIFO planted under the name cannot hang the open.
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | NOFOLLOW, dir_fd=root_fd)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return None
        if info.st_size > MAX_BYTES:
            return None
        return handle.read(MAX_BYTES + 1)[:MAX_BYTES]


class SnapshotHandler(BaseHTTPRequestHandler):
    server_version = "factory-dashboard"
    sys_version = ""
    timeout = 15  # a client that stalls mid-request is dropped

    def do_GET(self) -> None:
        self._serve(body=True)

    def do_HEAD(self) -> None:
        self._serve(body=False)

    def _serve(self, *, body: bool) -> None:
        name = ROUTES.get(urlsplit(self.path).path)
        data = read_snapshot_file(self.server.root_fd, name) if name else None  # type: ignore[attr-defined]
        if name is None or data is None:
            self._send(404, b"not found\n", "text/plain; charset=utf-8", body=body)
            return
        self._send(200, data, CONTENT_TYPES[name], body=body)

    def _send(self, code: int, data: bytes, content_type: str, *, body: bool) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(data)


class SnapshotServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], root: Path):
        self.root = root
        self.root_fd = open_root(root)
        try:
            super().__init__(address, SnapshotHandler)
        except BaseException:
            os.close(self.root_fd)
            raise

    def server_close(self) -> None:
        super().server_close()
        if self.root_fd >= 0:
            os.close(self.root_fd)
            self.root_fd = -1


def make_server(host: str, port: int, root: Path) -> SnapshotServer:
    """Serve `root` on host:port. Callers outside tests go through load_config."""
    return SnapshotServer((host, port), root)


def main(argv: list[str]) -> int:
    if argv not in ([], ["--check"]):
        print("usage: factory-dashboard-http.py [--check]", file=sys.stderr)
        return 2
    try:
        host, port, root = load_config(dict(os.environ))
    except ConfigError as exc:
        print(f"factory-dashboard-http: {exc}", file=sys.stderr)
        return 1
    if argv == ["--check"]:
        print(f"ok: would serve {root} on {host}:{port}")
        return 0
    server = make_server(host, port, root)
    print(f"factory-dashboard-http: serving {root} on {host}:{port}", file=sys.stderr)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
