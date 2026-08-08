"""Source readers. Each yields :class:`pastscape.model.Message` objects."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

from ..model import Message


def detect_source(path: Path) -> str:
    """Guess which reader handles ``path``.

    Content beats extension: a Takeout download is a zip whatever it is
    called, and a PST is a PST.
    """
    if path.is_dir():
        return "maildir" if (path / "cur").is_dir() and (path / "new").is_dir() else "eml"

    suffix = path.suffix.lower()
    if suffix in (".pst", ".ost"):
        return "pst"

    # Sniff: PST files start with "!BDN".
    try:
        with path.open("rb") as fh:
            head = fh.read(4)
        if head == b"!BDN":
            return "pst"
    except OSError:
        pass

    from .mbox import is_container, sniff_compression

    # zip/tar (Google Takeout) and gzip/bzip2/xz streams are all handled by the
    # mbox reader, which unwraps them.
    if is_container(path):
        return "mbox-archive"
    if sniff_compression(path) != "none":
        return "mbox-compressed"
    if suffix in (".mbox", ".mbx", ".mbs"):
        return "mbox"
    if suffix == ".eml":
        return "eml-file"
    return "mbox"


def read_source(path: Path, kind: str | None = None) -> Iterator[Message]:
    kind = kind or detect_source(path)
    if kind == "pst":
        from .pst import read_pst

        yield from read_pst(path)
    elif kind in ("eml", "maildir"):
        from .eml import read_eml_dir

        yield from read_eml_dir(path)
    elif kind == "eml-file":
        from .eml import read_eml_file

        msg = read_eml_file(path, folder="Inbox")
        if msg:
            yield msg
    elif kind in ("mbox", "mbox-compressed", "mbox-archive"):
        from .mbox import read_mbox

        yield from read_mbox(path)
    else:
        raise ValueError(f"unknown source kind: {kind!r}")


def source_fingerprint(path: Path) -> str:
    """Cheap change detector for the manifest: size + mtime of the input."""
    import hashlib

    h = hashlib.sha1()
    if path.is_dir():
        for root, dirs, files in os.walk(path):
            dirs.sort()
            for name in sorted(files):
                fp = Path(root) / name
                try:
                    st = fp.stat()
                except OSError:
                    continue
                h.update(str(fp.relative_to(path)).encode("utf-8", "replace"))
                h.update(f"{st.st_size}:{int(st.st_mtime)}".encode())
    else:
        try:
            st = path.stat()
            h.update(f"{path.name}:{st.st_size}:{int(st.st_mtime)}".encode())
        except OSError:
            h.update(b"missing")
    return h.hexdigest()[:16]
