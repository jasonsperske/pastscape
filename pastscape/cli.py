"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .builder import build_site
from .sources import detect_source
from .state import Manifest

EPILOG = """\
examples:
  pastscape build archive.pst -o site
  pastscape build mail/ -o site --title "Old Work Mail"
  pastscape build archive.pst extra-eml/ -o site        # merge several sources
  pastscape build archive.pst -o site --serve           # build, then preview
  pastscape info site                                   # what is published

Re-running build against the same output folder updates it in place: only new
or changed messages are rewritten. Delete manifest.json (or pass --force) for a
full rebuild.
"""


def _setup_logging(verbose: int, quiet: bool) -> None:
    level = logging.WARNING if quiet else (logging.DEBUG if verbose > 1 else logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(message)s" if verbose < 2 else "%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def cmd_build(args: argparse.Namespace) -> int:
    sources = [Path(s).expanduser() for s in args.source]
    missing = [str(s) for s in sources if not s.exists()]
    if missing:
        print(f"error: no such file or directory: {', '.join(missing)}", file=sys.stderr)
        return 2

    kinds = {}
    if args.type:
        for src in sources:
            kinds[src] = args.type

    site = Path(args.output).expanduser()
    stats = build_site(
        sources,
        site,
        title=args.title,
        kinds=kinds,
        limit=args.limit,
        folder_prefix=args.folder_prefix,
        block_remote=not args.allow_remote_images,
        news_host=args.news_host,
        prune=args.prune,
        force=args.force,
    )

    print(f"\nPastscape: {stats.summary()}")
    print(f"  site:  {site}")
    print(f"  open:  {site / 'index.html'}")
    if stats.attachments:
        print(f"  attachments written: {stats.attachments}")
    for err in stats.errors:
        print(f"  ! {err}", file=sys.stderr)

    if args.serve:
        _serve(site, args.port)
    return 1 if stats.errors and stats.total == 0 else 0


def cmd_info(args: argparse.Namespace) -> int:
    site = Path(args.site).expanduser()
    if not site.is_dir():
        print(f"error: {site} is not a directory", file=sys.stderr)
        return 2
    man = Manifest.load(site)
    if not man.messages:
        print(f"{site}: no published messages found")
        return 1

    folders: dict[str, int] = {}
    for rec in man.messages.values():
        folders[rec.folder] = folders.get(rec.folder, 0) + 1

    print(f"Pastscape archive: {site}")
    print(f"  title:    {man.title}")
    print(f"  built:    {man.built}")
    print(f"  messages: {len(man.messages)}")
    if man.sources:
        print("  sources:")
        for path, meta in man.sources.items():
            print(f"    {path}  (fingerprint {meta.get('fingerprint', '?')})")
    print("  folders:")
    for path in sorted(folders):
        print(f"    {folders[path]:>7}  {path}")
    return 0


def cmd_detect(args: argparse.Namespace) -> int:
    for src in args.source:
        path = Path(src).expanduser()
        if not path.exists():
            print(f"{src}: missing")
            continue
        print(f"{src}: {detect_source(path)}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    site = Path(args.site).expanduser()
    if not (site / "index.html").is_file():
        print(f"error: {site}/index.html not found -- build first", file=sys.stderr)
        return 2
    _serve(site, args.port)
    return 0


def _serve(site: Path, port: int) -> None:
    import functools
    import http.server
    import socketserver

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site))
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
        print(f"\nServing {site} at http://127.0.0.1:{port}/  (Ctrl-C to stop)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pastscape",
        description="Build a static, Netscape Communicator-styled archive from PST files or EML folders.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"pastscape {__version__}")

    # Accepted either before or after the subcommand; people type both. SUPPRESS
    # keeps the subparser from clobbering a value given ahead of the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="count", default=argparse.SUPPRESS)
    common.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("-v", "--verbose", action="count", default=0)
    parser.add_argument("-q", "--quiet", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", parents=[common], help="build or update an archive site")
    b.add_argument("source", nargs="+",
                   help="PST/OST file, mbox (plain, .gz/.bz2/.xz, or a Google "
                        "Takeout .zip/.tgz), or directory of .eml files (repeatable)")
    b.add_argument("-o", "--output", default="site", help="output directory (default: site)")
    b.add_argument("-t", "--title", default="Local Mail",
                   help="archive title, shown in the title bar and folder tree")
    b.add_argument("--type", choices=["pst", "eml", "mbox", "mbox-compressed",
                                      "mbox-archive", "maildir", "eml-file"],
                   help="force the source reader instead of sniffing")
    b.add_argument("--folder-prefix", default="",
                   help="nest every folder under this path, e.g. 'Archive 2004'")
    b.add_argument("--news-host", default="",
                   help="decorative news server entry in the folder tree, e.g. news.server.com")
    b.add_argument("--limit", type=int, default=0, help="stop after N messages (for testing)")
    b.add_argument("--prune", action="store_true",
                   help="delete pages for messages no longer present in the sources")
    b.add_argument("--force", action="store_true",
                   help="ignore the manifest and rewrite every page")
    b.add_argument("--allow-remote-images", action="store_true",
                   help="do not block remote images in message bodies")
    b.add_argument("--serve", action="store_true", help="serve the site after building")
    b.add_argument("--port", type=int, default=8000)
    b.set_defaults(func=cmd_build)

    i = sub.add_parser("info", parents=[common], help="summarise a published archive")
    i.add_argument("site", nargs="?", default="site")
    i.set_defaults(func=cmd_info)

    d = sub.add_parser("detect", parents=[common], help="report which reader would handle a source")
    d.add_argument("source", nargs="+")
    d.set_defaults(func=cmd_detect)

    s = sub.add_parser("serve", parents=[common], help="serve a built archive over HTTP")
    s.add_argument("site", nargs="?", default="site")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose, args.quiet)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
