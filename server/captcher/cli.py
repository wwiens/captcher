"""The `captcher` command.

    captcher                      start Captcher and open it in your browser
    captcher serve [options]      the same, spelled out
        --port N                  listen on port N (default 8080)
        --host ADDRESS            listen on ADDRESS (default 127.0.0.1); anything
                                  but this computer needs CAPTCHER_ACCESS_TOKEN
        --no-browser              do not open a browser tab
        --dev                     reload on code changes, show tracebacks
    captcher extension            print the folder to load into Chrome
    captcher install-browser      download Chromium for the render and replay
                                  checks (needs the [verify] extra)
    captcher token [LABEL]        mint a capture token for scripted uploads
    captcher devices              list the browsers and tokens that can upload
    captcher revoke ID            revoke one of them
    captcher --version

Settings live in ~/.captcher/.env (CAPTCHER_HOME moves the whole folder).
"""
import argparse
import os
import sys

COMMANDS = ("serve", "extension", "install-browser", "token", "devices", "revoke")


def _version():
    try:
        from importlib.metadata import version
        return version("captcher")
    except Exception:
        return "unknown"


def _parser():
    p = argparse.ArgumentParser(
        prog="captcher", usage="captcher [serve|extension|install-browser|token|devices|revoke] [options]",
        add_help=False)
    p.add_argument("-h", "--help", action="store_true")
    p.add_argument("--version", action="store_true")
    p.add_argument("command", nargs="?", default="serve")
    p.add_argument("args", nargs="*")
    p.add_argument("--port", type=int)
    p.add_argument("--host")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--dev", action="store_true")
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # `captcher 9000` (a bare port) has worked since the first release.
    if argv and argv[0].isdigit():
        argv = ["serve", "--port", argv[0], *argv[1:]]
    opts = _parser().parse_args(argv)
    if opts.help:
        print(__doc__.strip())
        return 0
    if opts.version:
        print(f"captcher {_version()}")
        return 0
    cmd = opts.command
    if cmd not in COMMANDS:
        print(f"captcher: unknown command {cmd!r}\n\n{__doc__.strip()}", file=sys.stderr)
        return 2
    if cmd == "serve" and opts.args and opts.args[0].isdigit() and opts.port is None:
        opts.port = int(opts.args[0])          # `captcher serve 9000`

    # Everything below imports modules that read the data folder's .env at
    # import, so the folder and its .env are created first.
    from . import install
    notes = install.prepare()

    if cmd == "extension":
        print(install.extension_folder())
        return 0
    if cmd == "install-browser":
        return install.install_browser()
    if cmd in ("token", "devices", "revoke"):
        from . import auth
        return auth.cli([cmd, *opts.args])

    # serve. Both are read when server.py is imported, so set them first.
    if opts.host:
        os.environ["CAPTCHER_HOST"] = opts.host
    if opts.dev:
        os.environ["CAPTCHER_DEBUG"] = "1"
    for line in notes:
        print(line)
    from . import server
    server.main(port=opts.port or 8080, open_browser=not opts.no_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())
